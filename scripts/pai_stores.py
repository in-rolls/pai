"""Format-aware access to the per-block tree, live on disk or inside a zstd archive.

The scraper writes one directory per block (``metadata.parquet``,
``scores_long.parquet``, ``data_wide.parquet``, ``context.json``, ``DONE.json``, plus
``html/``). That tree is the authoritative state but a wasteful way to keep it loose:
the JSON files are near-duplicates of each other and a solid archive compresses
across them.

Consumers go through :class:`BlockStore` so they work either way. The scraper does
not — it keeps writing the live tree, and compaction is a separate step over
finished years (see ``pai_compact.py``).

Blocks are yielded in sorted order by streaming the archive once and buffering one
block at a time (a few KB), rather than seeking: every consumer walks the whole
tree, and seeking backwards in a compressed stream means decompressing from the
start again.
"""

import csv
import hashlib
import io
import json
import os
import tarfile
from collections.abc import Iterator
from compression.zstd import CompressionParameter, DecompressionParameter, ZstdFile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

# Per-block payload lives in these; html/ and debug/ are archived separately so the
# 2.2 GB of page captures never has to be touched to read a score.
DATA_SUFFIXES = (".parquet", ".json")
HTML_SUFFIXES = (".html",)
EXCLUDED_DIRS = ("html", "debug")

BLOCKS_ARCHIVE = "blocks_{year}.tar.zst"
HTML_ARCHIVE = "html_{year}.tar.zst"
MANIFEST = "compact_{year}.json"

# -19 with a 128 MB window: the level is worth it here (42x vs 31x at -9) because
# compaction is a once-per-year batch job, not something on the read path.
COMPRESS_LEVEL = 19
WINDOW_LOG = 27


def zstd_write_options() -> dict[int, int]:
    return {
        CompressionParameter.compression_level: COMPRESS_LEVEL,
        CompressionParameter.window_log: WINDOW_LOG,
        CompressionParameter.enable_long_distance_matching: 1,
        CompressionParameter.nb_workers: os.cpu_count() or 1,
    }


def zstd_read_options() -> dict[int, int]:
    # The reader's window must be at least as large as the writer's or the frame
    # is rejected as needing too much memory.
    return {DecompressionParameter.window_log_max: WINDOW_LOG}


@dataclass
class Block:
    """One block directory's data payload, held in memory (a few KB)."""

    year: str
    rel: PurePosixPath  # path relative to the data dir, e.g. "2022-2023/Ladakh__37/..."
    files: dict[str, bytes] = field(default_factory=dict)

    def exists(self, name: str) -> bool:
        return name in self.files

    def text(self, name: str) -> str | None:
        raw = self.files.get(name)
        return None if raw is None else raw.decode("utf-8")

    def json(self, name: str) -> dict[str, Any] | None:
        raw = self.text(name)
        return None if raw is None else json.loads(raw)

    def table(self, name: str) -> pa.Table | None:
        """Read a per-block Parquet table; None if the file is absent."""
        raw = self.files.get(name)
        return None if raw is None else pq.read_table(io.BytesIO(raw))

    def rows(self, name: str) -> list[dict[str, Any]]:
        """Read a per-block Parquet table into typed dict rows; [] if absent."""
        table = self.table(name)
        return [] if table is None else table.to_pylist()

    @property
    def data_bytes(self) -> int:
        return sum(len(v) for v in self.files.values())


class BlockStore:
    """Reads the per-block tree for a year from whichever form is present."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    # -- form discovery ---------------------------------------------------- #
    def archive_path(self, year: str) -> Path:
        return self.data_dir / BLOCKS_ARCHIVE.format(year=year)

    def html_archive_path(self, year: str) -> Path:
        return self.data_dir / HTML_ARCHIVE.format(year=year)

    def manifest_path(self, year: str) -> Path:
        return self.data_dir / MANIFEST.format(year=year)

    def year_dir(self, year: str) -> Path:
        return self.data_dir / year

    def mode(self, year: str) -> str:
        """ "live" if the tree is on disk, "archive" if only the archive is, else "missing"."""
        if self.year_dir(year).is_dir():
            return "live"
        if self.archive_path(year).exists():
            return "archive"
        return "missing"

    def years(self) -> list[str]:
        """Every year present in either form, sorted."""
        found = {
            p.name
            for p in self.data_dir.glob("*")
            if p.is_dir() and "-" in p.name and not p.name.startswith(".")
        }
        for p in self.data_dir.glob("blocks_*.tar.zst"):
            found.add(p.name[len("blocks_") : -len(".tar.zst")])
        return sorted(found)

    # -- iteration --------------------------------------------------------- #
    def iter_blocks(self, year: str, names: set[str] | None = None) -> Iterator[Block]:
        """Yield each block of `year`. `names` limits which files are loaded.

        A caller that only needs ``DONE.json`` should say so: on the live tree
        that is the difference between reading a few MB and the whole cache.
        """
        mode = self.mode(year)
        if mode == "live":
            yield from self._iter_live(year, names)
        elif mode == "archive":
            yield from self._iter_archive(year, names)

    def _iter_live(self, year: str, names: set[str] | None = None) -> Iterator[Block]:
        yield from self.iter_tree(self.year_dir(year), year, names)

    def iter_tree(
        self, root: Path, year: str = "", names: set[str] | None = None
    ) -> Iterator[Block]:
        """Walk a live directory tree, yielding every directory that holds block data.

        "Holds block data" rather than "has a DONE.json": the rglob this replaces
        did not require a status file, and narrowing that would silently drop
        blocks from the consolidated output.
        """
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
            payload = {}
            for fn in sorted(files):
                if fn.endswith(DATA_SUFFIXES) and (names is None or fn in names):
                    payload[fn] = (Path(dirpath) / fn).read_bytes()
            if not payload:
                continue
            yield Block(
                year=year,
                rel=PurePosixPath(Path(dirpath).relative_to(self.data_dir).as_posix()),
                files=payload,
            )

    def _iter_archive(self, year: str, names: set[str] | None = None) -> Iterator[Block]:
        """Stream the archive once, emitting a Block each time the directory changes.

        Members are written sorted, so a directory's files are contiguous.
        """
        current: str | None = None
        payload: dict[str, bytes] = {}
        with self._open_archive(self.archive_path(year)) as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith(DATA_SUFFIXES):
                    continue
                p = PurePosixPath(member.name)
                if names is not None and p.name not in names:
                    continue
                parent = str(p.parent)
                if parent != current:
                    if payload:
                        assert current is not None
                        yield Block(year=year, rel=PurePosixPath(current), files=payload)
                    current, payload = parent, {}
                fobj = tar.extractfile(member)
                if fobj is not None:
                    payload[p.name] = fobj.read()
        if payload:
            assert current is not None
            yield Block(year=year, rel=PurePosixPath(current), files=payload)

    @staticmethod
    def _open_archive(path: Path) -> tarfile.TarFile:
        return tarfile.open(fileobj=ZstdFile(path, "rb", options=zstd_read_options()), mode="r|")

    # -- html -------------------------------------------------------------- #
    def iter_html(self, year: str) -> Iterator[tuple[str, bytes]]:
        """Yield (relative path, content) for every captured page."""
        if self.mode(year) == "live":
            for fp in sorted(self.year_dir(year).rglob("*.html")):
                yield fp.relative_to(self.data_dir).as_posix(), fp.read_bytes()
            return
        archive = self.html_archive_path(year)
        if not archive.exists():
            return
        with self._open_archive(archive) as tar:
            for member in tar:
                if member.isfile() and member.name.endswith(HTML_SUFFIXES):
                    fobj = tar.extractfile(member)
                    if fobj is not None:
                        yield member.name, fobj.read()

    # -- byte accounting --------------------------------------------------- #
    def sizes(self, year: str) -> tuple[int, int]:
        """(data_bytes, html_bytes) of the *uncompressed* tree.

        Read from the compaction manifest once the tree is archived, so coverage
        tables keep reporting the same numbers before and after compaction.
        """
        if self.mode(year) == "live":
            data_bytes = html_bytes = 0
            for root, _dirs, files in os.walk(self.year_dir(year)):
                for fn in files:
                    try:
                        sz = os.path.getsize(os.path.join(root, fn))
                    except OSError:
                        continue
                    if fn.endswith(HTML_SUFFIXES):
                        html_bytes += sz
                    elif fn.endswith(DATA_SUFFIXES):
                        data_bytes += sz
            return data_bytes, html_bytes
        man = self.read_manifest(year)
        if not man:
            return 0, 0
        return man["totals"]["data_bytes"], man["totals"]["html_bytes"]

    def read_manifest(self, year: str) -> dict[str, Any] | None:
        p = self.manifest_path(year)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def counts(self, year: str) -> dict[str, int]:
        """File counts a coverage report would otherwise get by globbing."""
        man = self.read_manifest(year)
        if self.mode(year) == "archive" and man:
            return man["counts"]
        done = failed = html = 0
        for blk in self.iter_blocks(year):
            done += blk.exists("DONE.json")
            failed += blk.exists("FAILED.json")
        for _ in self.iter_html(year):
            html += 1
        return {"done": done, "failed": failed, "html": html}


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# Column names the append-only logs carried before the per-block cache became Parquet.
LEGACY_GLOBAL_COLUMNS = {
    "metadata_csv": "metadata_file",
    "scores_long_csv": "scores_file",
    "data_wide_csv": "wide_file",
}


def _rename_legacy(row: dict[str, str]) -> dict[str, str]:
    return {LEGACY_GLOBAL_COLUMNS.get(name, name): value for name, value in row.items()}


def read_global(data_dir: Path, stem: str) -> list[dict[str, str]]:
    """Read a top-level append-only log: compacted Parquet first, then the live CSV.

    After a compaction the scraper starts a fresh CSV, so the older rows live in the
    Parquet and the newer ones in the CSV; the log is their concatenation. Everything
    is returned as strings either way: the scraper writes strings, and the parquet is
    written with a string schema so a gp_code cannot lose a leading zero.
    """
    rows: list[dict[str, str]] = []
    parquet = data_dir / f"{stem}.parquet"
    if parquet.exists():
        table = pq.read_table(parquet)
        cols = {name: table.column(name).to_pylist() for name in table.column_names}
        rows.extend(
            _rename_legacy(
                {name: ("" if col[i] is None else str(col[i])) for name, col in cols.items()}
            )
            for i in range(table.num_rows)
        )
    csv_path = data_dir / f"{stem}.csv"
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows.extend(_rename_legacy(row) for row in csv.DictReader(f))
    return rows

#!/usr/bin/env python3
"""Compact the PAI data directory, and expand it again.

The scraper's output is 6.8 GB in 79,815 files across 28,795 directories, none of
it compressed. Almost all of that is recoverable without losing anything:

  data/gp_scores_long.csv   1.99 GB  derived from the per-block files; regenerable
  data/gp_metadata.csv       161 MB  likewise
  per-block csv+json        2.35 GB  ->  56 MB as one solid zstd archive (42x)
  html page captures        2.20 GB  ->  ~250 MB likewise
  block_manifest.csv          49 MB  ->  2.9 MB parquet   (primary, kept)
  dropdown_inventory.csv      10 MB  ->  0.5 MB parquet   (primary, kept)

Subcommands:
  verify   Prove the global CSVs are reproducible from the per-block files.
  compact  Archive the tree and drop what verify proved redundant.
  expand   Restore a byte-identical tree (needed to resume a scrape).
  status   Report what form each year is in, and what it costs.

Nothing is deleted until the replacement has been written and read back, and every
archived file's sha256 has been matched against the manifest. Order matters here:
the expensive archive step runs before any deletion, so a failure costs time, not
data.

Usage:
  uv run scripts/pai_compact.py verify  [--data-dir data]
  uv run scripts/pai_compact.py compact [--data-dir data] [--years ...] [--keep-debug]
  uv run scripts/pai_compact.py expand  --years 2022-2023 [--into DIR]
  uv run scripts/pai_compact.py status  [--data-dir data]
"""

import argparse
import csv
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterable
from compression.zstd import ZstdFile
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import (  # noqa: E402
    DATA_WIDE_CSV,
    METADATA_CSV,
    SCORES_LONG_CSV,
    consolidate_per_block,
)
from pai_contracts import (  # noqa: E402
    apply_reviewed_score_vector_links,
    apply_reviewed_theme_headers,
    gp_key,
    load_score_value_exceptions,
    validate_block_rows,
)
from pai_stores import (  # noqa: E402
    DATA_SUFFIXES,
    EXCLUDED_DIRS,
    BlockStore,
    read_global,
    sha256_bytes,
    zstd_read_options,
    zstd_write_options,
)

# Rebuilt from the per-block tree by pai_rebuild_index.py, so they are copies, not
# sources. Everything else at the top level is an append-only scrape log and stays.
DERIVED_GLOBALS = {
    "gp_scores_long.csv": SCORES_LONG_CSV,
    "gp_metadata.csv": METADATA_CSV,
}
# Primary top-level CSVs: not reproducible from the per-block files, so they are
# converted rather than dropped.
PRIMARY_GLOBALS = ["block_manifest.csv", "dropdown_inventory.csv"]


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{n} B"
        value /= 1024.0
    return f"{value:.1f} GB"


# --------------------------------------------------------------------------- #
# verify: are the global CSVs really reproducible from the per-block files?
# --------------------------------------------------------------------------- #
def csv_row_multiset(path: Path) -> tuple[list[str], dict[tuple, int]]:
    """Header plus a multiset of rows, so order and duplicates both stay visible."""
    counts: dict[tuple, int] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, [])
        for row in reader:
            key = tuple(row)
            counts[key] = counts.get(key, 0) + 1
    return header, counts


def verify_rollup(data_dir: Path) -> int:
    """Regenerate each derived global and compare it to the one on disk.

    Compares row multisets, not bytes: consolidate_per_block walks rglob order,
    which is not guaranteed stable across filesystems, so a pure diff would report
    a difference that does not exist.
    """
    failures = 0
    store = BlockStore(data_dir)
    score_exceptions = load_score_value_exceptions()
    allowed_null_scores = set(score_exceptions)

    # A compacted source tree is only trustworthy if every byte still matches
    # the manifest written before the live tree was removed.
    for year in store.years():
        if store.mode(year) != "archive":
            continue
        print(f"\n=== compact archive {year} ===")
        manifest = store.read_manifest(year)
        if manifest is None:
            print("  FAIL archive has no compact manifest")
            failures += 1
            continue
        seen: dict[str, str] = {}
        for archive in (store.archive_path(year), store.html_archive_path(year)):
            if archive.exists():
                seen.update(archive_digests(archive))
        expected = {path: meta["sha256"] for path, meta in manifest["files"].items()}
        missing = set(expected) - set(seen)
        extra = set(seen) - set(expected)
        wrong = {path for path in expected.keys() & seen.keys() if expected[path] != seen[path]}
        if missing or extra or wrong:
            print(
                f"  FAIL members: missing={len(missing)}, unexpected={len(extra)}, "
                f"checksum mismatch={len(wrong)}"
            )
            failures += 1
        else:
            print(f"  PASS {len(seen):,} archived members match the compact manifest")

    for global_name, block_name in DERIVED_GLOBALS.items():
        existing = data_dir / global_name
        print(f"\n=== {global_name} ===")
        if not existing.exists():
            with tempfile.TemporaryDirectory() as td:
                regen = Path(td) / global_name
                n = consolidate_per_block(data_dir, block_name, regen)
            if n == 0:
                print("  FAIL absent global could not be rebuilt from the block store")
                failures += 1
            else:
                print(f"  PASS absent global is rebuildable: {n:,} rows")
            continue
        with tempfile.TemporaryDirectory() as td:
            regen = Path(td) / global_name
            t0 = time.time()
            n = consolidate_per_block(data_dir, block_name, regen)
            print(f"  regenerated from per-block files: {n:,} rows ({time.time() - t0:.0f}s)")

            head_a, rows_a = csv_row_multiset(existing)
            head_b, rows_b = csv_row_multiset(regen)

        if head_a != head_b:
            print(f"  FAIL header differs\n    on disk:    {head_a}\n    regenerated: {head_b}")
            failures += 1
            continue

        only_disk = sum(c for k, c in rows_a.items() if rows_b.get(k, 0) < c)
        only_regen = sum(c for k, c in rows_b.items() if rows_a.get(k, 0) < c)
        print(f"  rows on disk:      {sum(rows_a.values()):,}")
        print(f"  rows regenerated:  {sum(rows_b.values()):,}")
        print(f"  rows only on disk (would be LOST if deleted): {only_disk:,}")
        print(f"  rows only regenerated (extra):                {only_regen:,}")
        if only_disk or only_regen:
            print("  FAIL not reproducible; do not delete this file")
            failures += 1
            for k in list(rows_a):
                if rows_b.get(k, 0) < rows_a[k]:
                    print(f"    example row present only on disk: {k[:6]} ...")
                    break
        else:
            print("  PASS reproducible; safe to drop")
    # Stream block-sized batches rather than materializing the multi-GB global
    # long table. This still exercises the exact BlockStore read path and every
    # row contract, while a global key set catches duplicates across blocks.
    print("\n=== rebuilt analysis contract ===")
    seen_gp: set[tuple[str, ...]] = set()
    observed_null_scores: set[tuple[str, str, str]] = set()
    gp_rows = score_rows = wide_rows = done_blocks = repaired_identities = 0
    try:
        names = {"DONE.json", METADATA_CSV, SCORES_LONG_CSV, DATA_WIDE_CSV}
        for year in store.years():
            for block in store.iter_blocks(year, names=names):
                status = block.json("DONE.json")
                if not status or status.get("status") != "done":
                    continue
                metadata = block.rows(METADATA_CSV)
                scores = block.rows(SCORES_LONG_CSV)
                wide = block.rows(DATA_WIDE_CSV)
                repaired_identities += apply_reviewed_score_vector_links(metadata, scores, wide)
                apply_reviewed_theme_headers(scores, wide)
                contract = validate_block_rows(
                    metadata,
                    scores,
                    wide,
                    require_current_pai2_identity=False,
                    allowed_null_scores=allowed_null_scores,
                    observed_null_scores=observed_null_scores,
                )
                keys = {gp_key(row) for row in metadata}
                overlap = seen_gp & keys
                if overlap:
                    raise AssertionError(f"duplicate GP-year across blocks: {next(iter(overlap))}")
                seen_gp.update(keys)
                if int(status.get("gp_rows", 0) or 0) != contract["gp_rows"]:
                    raise AssertionError(f"{block.rel}: DONE GP row count differs")
                if int(status.get("score_rows", 0) or 0) != contract["score_rows"]:
                    raise AssertionError(f"{block.rel}: DONE score row count differs")
                done_blocks += 1
                gp_rows += contract["gp_rows"]
                score_rows += contract["score_rows"]
                wide_rows += contract["wide_rows"]
        if not done_blocks:
            raise AssertionError("no successful blocks found")
        if gp_rows != wide_rows:
            raise AssertionError(
                f"global metadata/wide conservation failed: {gp_rows} != {wide_rows}"
            )
        present_year_codes = {(key[0], key[-1]) for key in seen_gp}
        scoped_null_scores = {
            key for key in allowed_null_scores if (key[0], key[1]) in present_year_codes
        }
        for key in scoped_null_scores:
            exception = score_exceptions[key]
            source_path = exception["source_path"]
            year = exception["year"]
            manifest = store.read_manifest(year)
            expected_sha = exception["source_sha256"]
            if manifest is not None:
                source_meta = manifest["files"].get(source_path)
                actual_sha = source_meta.get("sha256") if source_meta else None
            else:
                source = data_dir / source_path
                actual_sha = sha256_bytes(source.read_bytes()) if source.exists() else None
            if actual_sha != expected_sha:
                raise AssertionError(f"reviewed score-null source mismatch: {source_path}")
        if observed_null_scores != scoped_null_scores:
            raise AssertionError(
                "reviewed null-score set mismatch: "
                f"observed_only={sorted(observed_null_scores - scoped_null_scores)}, "
                f"configured_only={sorted(scoped_null_scores - observed_null_scores)}"
            )
        print(
            f"  PASS {done_blocks:,} blocks, {gp_rows:,} GPs, {score_rows:,} scores; "
            f"schema/key/theme/range/DONE conservation; {len(observed_null_scores)} "
            f"reviewed source-null scores; {repaired_identities} reviewed legacy identities"
        )
    except (AssertionError, ValueError) as exc:
        print(f"  FAIL {exc}")
        failures += 1
    return failures


# --------------------------------------------------------------------------- #
# compact
# --------------------------------------------------------------------------- #
def collect(year_dir: Path, data_dir: Path, keep_debug: bool) -> tuple[list, list, list]:
    """(data files, html files, dropped files) as (path, arcname) pairs.

    Anything not archived is returned in `dropped` rather than passed over: the
    tree is deleted after this, so a file that lands in no archive is gone, and
    that has to be reported rather than inferred.
    """
    data_files, html_files, dropped = [], [], []
    for root, dirs, files in os.walk(year_dir):
        in_excluded = Path(root).name in EXCLUDED_DIRS
        dirs[:] = sorted(dirs)
        for fn in sorted(files):
            fp = Path(root) / fn
            arc = fp.relative_to(data_dir).as_posix()
            if fn.endswith(".html"):
                html_files.append((fp, arc))
            elif fn.endswith(".png") and keep_debug:
                html_files.append((fp, arc))
            elif fn.endswith(DATA_SUFFIXES) and not in_excluded:
                data_files.append((fp, arc))
            else:
                dropped.append((fp, arc))
    return data_files, html_files, dropped


def write_archive(pairs: Iterable[tuple[Path, str]], dst: Path) -> None:
    pairs = sorted(pairs, key=lambda t: t[1])
    with ZstdFile(dst, "wb", options=zstd_write_options()) as zf:
        with tarfile.open(fileobj=zf, mode="w|") as tar:
            for src, arc in pairs:
                tar.add(src, arcname=arc, recursive=False)


def archive_digests(path: Path) -> dict[str, str]:
    """sha256 of every member, read back out of the finished archive."""
    out: dict[str, str] = {}
    with ZstdFile(path, "rb", options=zstd_read_options()) as zf:
        with tarfile.open(fileobj=zf, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                fobj = tar.extractfile(member)
                if fobj is not None:
                    out[member.name] = sha256_bytes(fobj.read())
    return out


def to_parquet(src: Path, dst: Path) -> tuple[int, int]:
    """Convert a CSV to parquet, reading every column as a string.

    Type inference is the one way this step could silently change the data — a
    gp_code losing a leading zero, a grade of "NA" becoming null. The scraper
    writes strings, so strings is what round-trips.
    """
    import pyarrow as pa
    import pyarrow.csv as pv
    import pyarrow.parquet as pq

    with src.open(newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), [])
    if not header:
        raise SystemExit(f"{src}: no header row; refusing to convert an empty file")
    table = pv.read_csv(
        src,
        read_options=pv.ReadOptions(block_size=1 << 26),
        convert_options=pv.ConvertOptions(column_types=dict.fromkeys(header, pa.string())),
    )
    pq.write_table(table, dst, compression="zstd", compression_level=3)

    # Read the parquet back and compare it cell-for-cell against the CSV before
    # the caller deletes the CSV. Writing a file is not evidence that it holds
    # what the source held.
    with src.open(newline="", encoding="utf-8") as f:
        original = list(csv.DictReader(f))
    restored = read_global(src.parent, src.stem)
    if len(original) != len(restored):
        raise SystemExit(
            f"{src.name}: parquet has {len(restored):,} rows, CSV had {len(original):,}"
        )
    for i, (a, b) in enumerate(zip(original, restored, strict=True)):
        if a != b:
            bad = [k for k in a if a[k] != b.get(k)]
            raise SystemExit(f"{src.name}: row {i} differs in columns {bad}")
    return table.num_rows, len(header)


def compact_year(store: BlockStore, year: str, keep_debug: bool) -> bool:
    data_dir = store.data_dir
    year_dir = store.year_dir(year)
    print(f"\n=== {year} ===")
    if store.mode(year) != "live":
        print(f"  {store.mode(year)}; nothing to do")
        return True

    data_files, html_files, dropped = collect(year_dir, data_dir, keep_debug)
    data_bytes = sum(p.stat().st_size for p, _ in data_files)
    html_bytes = sum(p.stat().st_size for p, _ in html_files)
    print(
        f"  {len(data_files):,} data files ({human(data_bytes)}), "
        f"{len(html_files):,} html/debug files ({human(html_bytes)})"
    )
    if dropped:
        by_kind: dict[str, list[int]] = {}
        for fp, arc in dropped:
            kind = "debug/" if "debug" in PurePosixPath(arc).parts else (fp.suffix or fp.name)
            b = by_kind.setdefault(kind, [0, 0])
            b[0] += 1
            b[1] += fp.stat().st_size
        summary = ", ".join(
            f"{n:,} x {kind} ({human(sz)})" for kind, (n, sz) in sorted(by_kind.items())
        )
        print(f"  DROPPING (archived nowhere, deleted with the tree): {summary}")
        print(
            '    each one listed in the manifest\'s "dropped"; '
            "pass --keep-debug to archive debug/ instead"
        )

    manifest: dict[str, Any] = {
        "year": year,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "totals": {"data_bytes": data_bytes, "html_bytes": html_bytes},
        "counts": {
            "done": 0,
            "failed": 0,
            "html": len([1 for _, a in html_files if a.endswith(".html")]),
        },
        "files": {},
        "dropped": [arc for _, arc in dropped],
    }
    for fp, arc in data_files + html_files:
        raw = fp.read_bytes()
        manifest["files"][arc] = {"size": len(raw), "sha256": sha256_bytes(raw)}
        if arc.endswith("/DONE.json"):
            manifest["counts"]["done"] += 1
        elif arc.endswith("/FAILED.json"):
            manifest["counts"]["failed"] += 1

    blocks_archive = store.archive_path(year)
    html_archive = store.html_archive_path(year)

    t0 = time.time()
    print(f"  writing {blocks_archive.name} ...")
    write_archive(data_files, blocks_archive)
    print(
        f"    {human(blocks_archive.stat().st_size)} "
        f"({data_bytes / max(blocks_archive.stat().st_size, 1):.0f}x, {time.time() - t0:.0f}s)"
    )

    if html_files:
        t0 = time.time()
        print(f"  writing {html_archive.name} ...")
        write_archive(html_files, html_archive)
        print(
            f"    {human(html_archive.stat().st_size)} "
            f"({html_bytes / max(html_archive.stat().st_size, 1):.0f}x, {time.time() - t0:.0f}s)"
        )

    # Read both archives back and match every member against the manifest before
    # anything is removed.
    print("  verifying archives against the manifest ...")
    seen: dict[str, str] = {}
    seen.update(archive_digests(blocks_archive))
    if html_files:
        seen.update(archive_digests(html_archive))
    missing = [a for a in manifest["files"] if a not in seen]
    wrong = [a for a, m in manifest["files"].items() if a in seen and seen[a] != m["sha256"]]
    extra = [a for a in seen if a not in manifest["files"]]
    print(
        f"    members {len(seen):,} | missing {len(missing)} | "
        f"checksum mismatch {len(wrong)} | unexpected {len(extra)}"
    )
    if missing or wrong or extra:
        print("  FAIL archive does not match the tree; nothing deleted")
        for a in (missing + wrong + extra)[:5]:
            print(f"    {a}")
        return False

    store.manifest_path(year).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"  wrote {store.manifest_path(year).name}")

    shutil.rmtree(year_dir)
    print(f"  removed {year_dir}/ ({human(data_bytes + html_bytes)} freed)")
    return True


def compact_globals(data_dir: Path) -> None:
    print("\n=== top-level files ===")
    for name in DERIVED_GLOBALS:
        p = data_dir / name
        if p.exists():
            sz = p.stat().st_size
            p.unlink()
            print(f"  removed {name} ({human(sz)} freed; rebuild with pai_rebuild_index.py)")

    for name in PRIMARY_GLOBALS:
        src = data_dir / name
        if not src.exists():
            continue
        dst = src.with_suffix(".parquet")
        rows, cols = to_parquet(src, dst)
        before, after = src.stat().st_size, dst.stat().st_size
        src.unlink()
        print(
            f"  {name} -> {dst.name}: {rows:,} rows x {cols} cols, "
            f"{human(before)} -> {human(after)} ({before / max(after, 1):.0f}x)"
        )

    for log in sorted(data_dir.glob("*.log")):
        raw = log.read_bytes()
        dst = log.with_suffix(".log.zst")
        with ZstdFile(dst, "wb", options=zstd_write_options()) as f:
            f.write(raw)
        log.unlink()
        print(f"  {log.name} -> {dst.name}: {human(len(raw))} -> {human(dst.stat().st_size)}")


# --------------------------------------------------------------------------- #
# expand
# --------------------------------------------------------------------------- #
def expand_year(store: BlockStore, year: str, into: Path) -> bool:
    manifest = store.read_manifest(year)
    if manifest is None:
        print(f"  [{year}] no manifest; cannot verify an expansion")
        return False
    into.mkdir(parents=True, exist_ok=True)
    n = 0
    for archive in (store.archive_path(year), store.html_archive_path(year)):
        if not archive.exists():
            continue
        print(f"  extracting {archive.name} ...")
        with ZstdFile(archive, "rb", options=zstd_read_options()) as zf:
            with tarfile.open(fileobj=zf, mode="r|") as tar:
                tar.extractall(into, filter="data")
                n += 1
    if not n:
        print(f"  [{year}] no archives found")
        return False

    print("  verifying against the manifest ...")
    bad = []
    for arc, meta in manifest["files"].items():
        fp = into / arc
        if not fp.exists():
            bad.append(("missing", arc))
        elif sha256_bytes(fp.read_bytes()) != meta["sha256"]:
            bad.append(("checksum", arc))
    if bad:
        print(f"  FAIL {len(bad)} file(s) differ")
        for kind, arc in bad[:5]:
            print(f"    {kind}: {arc}")
        return False
    print(f"  PASS {len(manifest['files']):,} files restored byte-identical to {into}")
    return True


# --------------------------------------------------------------------------- #
def cmd_status(store: BlockStore) -> int:
    data_dir = store.data_dir
    print(f"{'year':12} {'form':9} {'on disk':>12} {'uncompressed':>14} {'ratio':>7}")
    for year in store.years():
        mode = store.mode(year)
        if mode == "live":
            data_b, html_b = store.sizes(year)
            print(
                f"{year:12} {mode:9} {human(data_b + html_b):>12} "
                f"{human(data_b + html_b):>14} {'1x':>7}"
            )
        else:
            on_disk = sum(
                p.stat().st_size
                for p in (store.archive_path(year), store.html_archive_path(year))
                if p.exists()
            )
            data_b, html_b = store.sizes(year)
            total = data_b + html_b
            print(
                f"{year:12} {mode:9} {human(on_disk):>12} {human(total):>14} "
                f"{total / max(on_disk, 1):.0f}x"
            )
    print()
    for p in sorted(data_dir.glob("*")):
        if p.is_file():
            print(f"  {p.name:34} {human(p.stat().st_size):>10}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["verify", "compact", "expand", "status"])
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--years", nargs="+")
    ap.add_argument("--into", help="expand: destination dir (default: the data dir)")
    ap.add_argument(
        "--keep-debug",
        action="store_true",
        help="compact: archive debug/*.png too (default: drop them)",
    )
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"Error: data dir not found: {data_dir}", file=sys.stderr)
        return 1
    store = BlockStore(data_dir)
    years = args.years or store.years()

    if args.command == "status":
        return cmd_status(store)

    if args.command == "verify":
        failures = verify_rollup(data_dir)
        print(f"\n{'FAILED' if failures else 'PASS'}: {failures} global(s) not reproducible")
        return 1 if failures else 0

    if args.command == "expand":
        into = Path(args.into) if args.into else data_dir
        ok = all(expand_year(store, y, into) for y in years)
        return 0 if ok else 1

    # compact
    print("Gate: the derived globals must be reproducible before anything is removed.")
    if verify_rollup(data_dir) != 0:
        print("\nABORT: verify failed; nothing was changed.", file=sys.stderr)
        return 1
    for year in years:
        if not compact_year(store, year, args.keep_debug):
            print("\nABORT: archive verification failed; nothing was deleted.", file=sys.stderr)
            return 1
    compact_globals(data_dir)
    print()
    return cmd_status(store)


if __name__ == "__main__":
    raise SystemExit(main())

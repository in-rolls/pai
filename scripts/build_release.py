#!/usr/bin/env python3
"""
Build per-year release archives for the PAI data.

For each year, the default data archive contains only typed Parquet analysis
tables plus a compact provenance/control manifest and checksums. Raw rendered
HTML and the resumable per-block cache are separate recovery artifacts; pass
--include-cache only when intentionally archiving that cache.

Usage:
  python scripts/build_release.py [--data-dir data] [--out dist] \
      [--years 2022-2023 2023-2024] [--skip-html] [--skip-data]
"""

import argparse
import contextlib
import hashlib
import io
import os
import shutil
import sys
import tarfile
import time
from compression.zstd import ZstdFile
from pathlib import Path, PurePosixPath

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_rebuild_index import build, parse_expected  # noqa: E402
from pai_stores import BlockStore, zstd_read_options, zstd_write_options  # noqa: E402


@contextlib.contextmanager
def open_zst_tar_write(path: Path):
    """A tar stream compressed with zstd rather than gzip.

    Same container, ~5x smaller here: gzip's 32 KB window cannot match across
    files, and these per-block CSVs are near-duplicates of each other.
    """
    with ZstdFile(path, "wb", options=zstd_write_options()) as zf:
        with tarfile.open(fileobj=zf, mode="w|") as tar:
            yield tar


def add_bytes(tar: tarfile.TarFile, arcname: str, raw: bytes) -> None:
    """Add an in-memory file, so a source that is itself an archive needs no unpacking."""
    info = tarfile.TarInfo(arcname)
    info.size = len(raw)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(raw))


def verify_archive(archive: Path, expected: dict[str, str]) -> None:
    """Read the finished archive and verify every member checksum and path."""
    found: dict[str, str] = {}
    with ZstdFile(archive, "rb", options=zstd_read_options()) as zf:
        with tarfile.open(fileobj=zf, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                handle = tar.extractfile(member)
                if handle is not None:
                    found[member.name] = hashlib.sha256(handle.read()).hexdigest()
    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    wrong = sorted(name for name in expected.keys() & found.keys() if expected[name] != found[name])
    if missing or extra or wrong:
        raise AssertionError(
            f"{archive}: round-trip failed: missing={missing[:3]}, extra={extra[:3]}, "
            f"checksum_mismatch={wrong[:3]}"
        )


README_DATA = """PAI Gram Panchayat scores — {year}

Files:
  gp_scores_wide.parquet      canonical one-row-per-GP table
  gp_metadata.parquet         one row per GP, identity/geography/provenance
  gp_scores_long.parquet      one row per GP x overall/theme score
  gp_universe.parquet         official handler denominator and LGD/name bridge
  block_manifest.parquet      latest collection outcome per block
  dropdown_inventory.parquet  discovered State/District/Block universe
  collection_manifest.json    schemas, row contracts, official controls, sha256

IDs and labels are strings; scores and count/order fields are numeric. The
collection manifest records the unique GP-year key rule and all checks run.
Raw HTML and the resumable per-block cache are separate recovery artifacts and
are deliberately absent from this analysis-data archive unless the archive name
ends in _data_with_cache.

The matching raw HTML captures may be published as a separate archive.
Source: https://pai.gov.in  |  Scraper: scripts/pai_scraper_resumable.py
"""

DATAVERSE_MD = """# Uploading a release to Dataverse

Dataverse stores the large source and recovery archives. The required artifact for each
archived year is the compact typed-Parquet analysis bundle:

  dist/pai_<year>_data.tar.zst

Raw rendered source pages may be published separately as `pai_<year>_html.tar.zst`.
The much larger `pai_<year>_data_with_cache.tar.zst` is a recovery artifact, not analysis
data, and should be uploaded only when preserving the resumable per-block cache is intended.

Steps:
1. Go to your Dataverse collection and **Add Data -> New Dataset** (or open the existing dataset).
2. Fill metadata (title e.g. "PAI Gram Panchayat scores", author, description, subject).
   Describe the typed Parquet tables, the official-count contracts, and any separately uploaded
   raw HTML or recovery cache.
3. **Upload Files** -> add the data bundle and any intentional source/recovery archives.
4. **Publish** the dataset.
5. Record the dataset **DOI** and each file's numeric id in the README so the source-archive
   download commands resolve. The small canonical Parquet tables are shipped separately in
   `data/release/` and attached to each GitHub release tag.
"""


def requires_national_controls(year: str, allow_partial: bool) -> bool:
    """PAI 2.0 cannot be labeled as a release without the national controls."""
    return year == "2023-2024" and not allow_partial


def build_data_archive(
    data_dir: Path,
    out: Path,
    year: str,
    expected: dict[tuple[str, str], int],
    include_cache: bool = False,
    require_national: bool = False,
    universe_data_dir: Path | None = None,
) -> Path:
    stage = out / "_stage" / f"pai_{year}_data"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    year_expected = {key: value for key, value in expected.items() if key[0] == year}
    contract = build(
        data_dir,
        stage,
        year_expected,
        years=[year],
        require_national=require_national,
        universe_data_dir=universe_data_dir,
    )
    print(
        f"  [{year}] contracts passed: {contract['gp_rows']:,} GPs, "
        f"{contract['score_rows']:,} GP x score rows"
    )
    (stage / "README_data.txt").write_text(README_DATA.format(year=year), encoding="utf-8")

    suffix = "_data_with_cache" if include_cache else "_data"
    archive = out / f"pai_{year}{suffix}.tar.zst"
    print(f"  [{year}] writing {archive.name} ...")
    n_block_files = 0
    store = BlockStore(data_dir)
    expected_members: dict[str, str] = {}
    with open_zst_tar_write(archive) as tar:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                arc = str(Path(f"pai_{year}_data") / p.relative_to(stage))
                raw = p.read_bytes()
                add_bytes(tar, arc, raw)
                expected_members[arc] = hashlib.sha256(raw).hexdigest()
        if include_cache:
            for blk in store.iter_blocks(year):
                rel_to_year = PurePosixPath(*blk.rel.parts[1:])
                for name in sorted(blk.files):
                    cache_arc = PurePosixPath(f"pai_{year}_data") / "cache" / rel_to_year / name
                    raw = blk.files[name]
                    add_bytes(tar, str(cache_arc), raw)
                    expected_members[str(cache_arc)] = hashlib.sha256(raw).hexdigest()
                    n_block_files += 1
    verify_archive(archive, expected_members)
    if include_cache:
        print(f"    added {n_block_files:,} per-block recovery files")
    return archive


def build_html_archive(data_dir: Path, out: Path, year: str) -> Path:
    """Raw HTML page captures for Dataverse, as a single .tar.zst (Dataverse leaves
    a tarball as one opaque file rather than auto-extracting it like a .zip)."""
    archive = out / f"pai_{year}_html.tar.zst"
    print(f"  [{year}] writing {archive.name} (raw HTML page captures) ...")
    n = 0
    store = BlockStore(data_dir)
    with open_zst_tar_write(archive) as tar:
        for rel, raw in store.iter_html(year):
            rel_to_year = PurePosixPath(*PurePosixPath(rel).parts[1:])
            add_bytes(tar, str(PurePosixPath(f"pai_{year}_html") / rel_to_year), raw)
            n += 1
    print(f"    added {n:,} html pages")
    return archive


def main() -> int:
    ap = argparse.ArgumentParser(description="Build per-year PAI release archives")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--years", nargs="+", default=["2022-2023", "2023-2024"])
    ap.add_argument(
        "--skip-html", action="store_true", help="Skip the (large) HTML capture archives"
    )
    ap.add_argument("--skip-data", action="store_true", help="Skip the parsed data archives")
    ap.add_argument(
        "--include-cache",
        action="store_true",
        help="Include the resumable per-block cache in the data archive (off by default)",
    )
    ap.add_argument(
        "--expected-state-gps",
        action="append",
        default=[],
        metavar="[YEAR:]STATE=N",
        help="Hard official count control; repeat for multiple states",
    )
    ap.add_argument(
        "--allow-partial",
        action="store_true",
        help="Allow a non-national PAI 2.0 release; explicit state controls still apply",
    )
    ap.add_argument(
        "--universe-data-dir",
        help="Standalone nationwide universe crawl directory or Parquet path",
    )
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "DATAVERSE_UPLOAD.md").write_text(DATAVERSE_MD, encoding="utf-8")
    expected = parse_expected(args.expected_state_gps)

    store = BlockStore(data_dir)
    for year in args.years:
        if store.mode(year) == "missing":
            print(f"  [{year}] not found, skipping")
            continue
        print(f"=== {year} ===")
        if not args.skip_data:
            a = build_data_archive(
                data_dir,
                out,
                year,
                expected,
                args.include_cache,
                requires_national_controls(year, args.allow_partial),
                Path(args.universe_data_dir) if args.universe_data_dir else None,
            )
            print(f"  -> {a}  ({a.stat().st_size / 1048576:.1f} MB)")
        if not args.skip_html:
            h = build_html_archive(data_dir, out, year)
            print(f"  -> {h}  ({h.stat().st_size / 1048576:.1f} MB)")

    print(f"\nDone. Artifacts in {out}/ ; Dataverse steps in {out / 'DATAVERSE_UPLOAD.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

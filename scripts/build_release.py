#!/usr/bin/env python3
"""
Build per-year release archives for the PAI data.

For each year produces, under --out (default dist/):
  pai_<year>_data.tar.gz   GitHub Release asset (CSV/JSON only, NO html/debug):
      consolidated/  gp_metadata_<year>.csv, gp_scores_long_<year>.csv,
                     gp_scores_wide_<year>.csv, block_manifest_<year>.csv,
                     dropdown_inventory_<year>.csv
      blocks/        per-block data_wide/metadata/scores_long.csv + DONE.json tree
      SUMMARY.md, README_data.txt
  pai_<year>_html.tar.gz   Dataverse asset (raw rendered HTML page captures)

Stdlib only; consolidated CSVs are rebuilt from the per-block files.

Usage:
  python scripts/build_release.py [--data-dir test_data] [--out dist] \
      [--years 2022-2023 2023-2024] [--skip-html] [--skip-data]
"""

import argparse
import csv
import os
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_summary  # noqa: E402  (sibling script, reused for SUMMARY.md)
from pai_common import (  # noqa: E402
    DATA_WIDE_CSV,
    METADATA_CSV,
    SCORES_LONG_CSV,
    consolidate_per_block,
)

# The consolidated dataset (gp_metadata / gp_scores_long / gp_scores_wide) is rebuilt
# from the per-block files via pai_common.consolidate_per_block — the authoritative
# current state (overwritten on each scrape). The top-level global CSVs are append-only
# logs and can carry duplicate rows from multi-run history, so we do NOT use them.

README_DATA = """PAI Gram Panchayat scores — {year}

Files (consolidated/):
  gp_metadata_{year}.csv      one row per Gram Panchayat (identity + location):
      year, state, district, block, gp_name, gp_code, scorecard_url, ...
  gp_scores_long_{year}.csv   one row per GP x theme (tidy/long):
      ...identity..., theme_order, theme_header, theme_slug, score, grade, band, raw_value
      theme_slug 'overall_pai_score' is the overall PAI score; T1..T9 are the nine themes.
  gp_scores_wide_{year}.csv   one row per GP, one column per theme (<slug>_score/_grade/_band/_raw)
  block_manifest_{year}.csv   per-block scrape outcome log
  dropdown_inventory_{year}.csv  the State/District/Block option universe discovered

blocks/  raw per-block CSV/JSON exactly as scraped (data_wide/metadata/scores_long + DONE.json).

Raw rendered HTML page captures are published separately on Dataverse (see the repository README).
Source: https://pai.gov.in  |  Scraper: scripts/pai_scraper_resumable.py
"""

DATAVERSE_MD = """# Uploading the HTML captures to Dataverse

Two archives were built for Dataverse (raw rendered page HTML, one per year):

  dist/pai_2022-2023_html.tar.gz
  dist/pai_2023-2024_html.tar.gz

Steps:
1. Go to your Dataverse collection and **Add Data -> New Dataset** (or open the existing dataset).
2. Fill metadata (title e.g. "PAI Gram Panchayat score pages (raw HTML)", author, description,
   subject). Note in the description that the parsed CSV data lives in the GitHub release.
3. **Upload Files** -> add both `pai_<year>_html.tar.gz` files. (Dataverse stores .tar.gz as a
   single file, unlike .zip which it offers to auto-extract.)
4. **Publish** the dataset.
5. Copy the dataset **DOI** (e.g. doi:10.7910/DVN/XXXXXX) and paste it into the README "Data"
   section, replacing the `<DATAVERSE_DOI>` placeholder.
"""


def dedup_global(src: Path, dst: Path, year: str, key_cols: list[str]) -> int:
    """Stream an append-only global log, keep rows for `year`, de-dup on key_cols (last wins)."""
    if not src.exists():
        dst.write_text("", encoding="utf-8")
        return 0
    with src.open(newline="", encoding="utf-8") as fi:
        reader = csv.reader(fi)
        header = next(reader)
        iy = header.index("year")
        kidx = [header.index(c) for c in key_cols]
        rows: dict[tuple, list[str]] = {}
        for row in reader:
            if len(row) <= iy or row[iy] != year:
                continue
            key = tuple(row[i] if i < len(row) else "" for i in kidx)
            rows[key] = row
    with dst.open("w", newline="", encoding="utf-8") as fo:
        w = csv.writer(fo)
        w.writerow(header)
        w.writerows(rows.values())
    return len(rows)


def make_summary_md(data_dir: Path, year: str) -> str:
    year_rows, state_rows = data_summary.build(data_dir, [year])
    return (
        f"# PAI coverage — {year}\n\n## Year totals\n\n"
        + data_summary.markdown_table(year_rows)
        + "\n## By state\n\n"
        + data_summary.markdown_table(state_rows)
    )


def build_data_archive(data_dir: Path, out: Path, year: str) -> Path:
    year_dir = data_dir / year
    stage = out / "_stage" / f"pai_{year}_data"
    cons = stage / "consolidated"
    cons.mkdir(parents=True, exist_ok=True)

    print(f"  [{year}] consolidating per-block files (de-duplicated current state) ...")
    m = consolidate_per_block(year_dir, METADATA_CSV, cons / f"gp_metadata_{year}.csv")
    print(f"    gp_metadata_{year}.csv: {m:,} rows")
    s = consolidate_per_block(year_dir, SCORES_LONG_CSV, cons / f"gp_scores_long_{year}.csv")
    print(f"    gp_scores_long_{year}.csv: {s:,} rows")
    w = consolidate_per_block(year_dir, DATA_WIDE_CSV, cons / f"gp_scores_wide_{year}.csv")
    print(f"    gp_scores_wide_{year}.csv: {w:,} rows")
    bm = dedup_global(
        data_dir / "block_manifest.csv",
        cons / f"block_manifest_{year}.csv",
        year,
        ["block_dir"],
    )
    print(f"    block_manifest_{year}.csv: {bm:,} blocks (latest status)")
    di = dedup_global(
        data_dir / "dropdown_inventory.csv",
        cons / f"dropdown_inventory_{year}.csv",
        year,
        ["level", "state_value", "district_value", "option_value"],
    )
    print(f"    dropdown_inventory_{year}.csv: {di:,} options")

    (stage / "SUMMARY.md").write_text(make_summary_md(data_dir, year), encoding="utf-8")
    (stage / "README_data.txt").write_text(README_DATA.format(year=year), encoding="utf-8")

    archive = out / f"pai_{year}_data.tar.gz"
    print(f"  [{year}] writing {archive.name} (consolidated + blocks, excluding html/debug) ...")
    n_block_files = 0
    with tarfile.open(archive, "w:gz") as tar:
        # consolidated + docs
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                tar.add(p, arcname=str(Path(f"pai_{year}_data") / p.relative_to(stage)))
        # per-block CSV/JSON tree (skip html/ and debug/)
        for root, dirs, files in os.walk(year_dir):
            dirs[:] = [d for d in dirs if d not in ("html", "debug")]
            for fn in files:
                if fn.endswith((".csv", ".json")):
                    fp = Path(root) / fn
                    arc = Path(f"pai_{year}_data") / "blocks" / fp.relative_to(year_dir)
                    tar.add(fp, arcname=str(arc))
                    n_block_files += 1
    print(f"    added {n_block_files:,} per-block files")
    return archive


def build_html_archive(data_dir: Path, out: Path, year: str) -> Path:
    """Raw HTML page captures for Dataverse, as a single .tar.gz (Dataverse leaves
    .tar.gz as one opaque file rather than auto-extracting it like a .zip)."""
    year_dir = data_dir / year
    archive = out / f"pai_{year}_html.tar.gz"
    print(f"  [{year}] writing {archive.name} (raw HTML page captures) ...")
    n = 0
    with tarfile.open(archive, "w:gz") as tar:
        for fp in sorted(year_dir.rglob("*.html")):
            tar.add(fp, arcname=str(Path(f"pai_{year}_html") / fp.relative_to(year_dir)))
            n += 1
    print(f"    added {n:,} html pages")
    return archive


def main() -> int:
    ap = argparse.ArgumentParser(description="Build per-year PAI release archives")
    ap.add_argument("--data-dir", default="test_data")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--years", nargs="+", default=["2022-2023", "2023-2024"])
    ap.add_argument("--skip-html", action="store_true", help="Skip the (large) Dataverse HTML zips")
    ap.add_argument("--skip-data", action="store_true", help="Skip the GitHub data tar.gz")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "DATAVERSE_UPLOAD.md").write_text(DATAVERSE_MD, encoding="utf-8")

    for year in args.years:
        if not (data_dir / year).exists():
            print(f"  [{year}] not found, skipping")
            continue
        print(f"=== {year} ===")
        if not args.skip_data:
            a = build_data_archive(data_dir, out, year)
            print(f"  -> {a}  ({a.stat().st_size / 1048576:.1f} MB)")
        if not args.skip_html:
            h = build_html_archive(data_dir, out, year)
            print(f"  -> {h}  ({h.stat().st_size / 1048576:.1f} MB)")

    print(f"\nDone. Artifacts in {out}/ ; Dataverse steps in {out / 'DATAVERSE_UPLOAD.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

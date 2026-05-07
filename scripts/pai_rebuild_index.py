#!/usr/bin/env python3
"""
Rebuild global indexes from per-block outputs.

Usage:
  python pai_rebuild_index.py --out test_data
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List


GP_METADATA_FIELDS = [
    "run_id",
    "timestamp_utc",
    "year",
    "state",
    "state_value",
    "district",
    "district_value",
    "block",
    "block_value",
    "gp_name",
    "gp_code",
    "scorecard_url",
    "details_raw",
    "block_page",
    "block_dir",
    "block_data_wide_csv",
    "block_html_file",
    "source_url",
]

GP_SCORE_FIELDS = GP_METADATA_FIELDS + [
    "theme_order",
    "theme_header",
    "theme_slug",
    "score",
    "grade",
    "band",
    "raw_value",
]


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv_rows(path: Path, rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="test_data")
    args = parser.parse_args()

    out = Path(args.out)
    global_metadata = out / "gp_metadata.csv"
    global_scores = out / "gp_scores_long.csv"

    if global_metadata.exists():
        global_metadata.unlink()
    if global_scores.exists():
        global_scores.unlink()

    done_files = sorted(out.rglob("DONE.json"))
    print(f"Found {len(done_files)} DONE.json files")

    meta_count = 0
    score_count = 0

    for done in done_files:
        block_dir = done.parent
        meta_rows = read_csv(block_dir / "metadata.csv")
        score_rows = read_csv(block_dir / "scores_long.csv")

        append_csv_rows(global_metadata, meta_rows, GP_METADATA_FIELDS)
        append_csv_rows(global_scores, score_rows, GP_SCORE_FIELDS)

        meta_count += len(meta_rows)
        score_count += len(score_rows)

    print(f"Wrote {meta_count} metadata rows -> {global_metadata}")
    print(f"Wrote {score_count} score rows -> {global_scores}")


if __name__ == "__main__":
    main()

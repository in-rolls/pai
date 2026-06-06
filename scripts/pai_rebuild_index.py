#!/usr/bin/env python3
"""Rebuild the global indexes from the per-block outputs.

The per-block ``metadata.csv`` / ``scores_long.csv`` files are the authoritative
current state (overwritten on each scrape), so this rebuild is de-duplicated by
construction — unlike the scraper, it never appends, it overwrites. This is the
canonical way to (re)generate ``gp_metadata.csv`` and ``gp_scores_long.csv``.

Usage:
  uv run scripts/pai_rebuild_index.py --out test_data
"""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import METADATA_CSV, SCORES_LONG_CSV, consolidate_per_block  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild global indexes from per-block files")
    parser.add_argument("--out", default="test_data")
    args = parser.parse_args()

    out = Path(args.out)
    # rglob across all year dirs -> one de-duplicated global per file type.
    meta = consolidate_per_block(out, METADATA_CSV, out / "gp_metadata.csv")
    scores = consolidate_per_block(out, SCORES_LONG_CSV, out / "gp_scores_long.csv")

    print(f"Wrote {meta:,} metadata rows -> {out / 'gp_metadata.csv'}")
    print(f"Wrote {scores:,} score rows -> {out / 'gp_scores_long.csv'}")


if __name__ == "__main__":
    main()

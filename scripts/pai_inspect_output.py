#!/usr/bin/env python3
"""
Inspect scraper progress.

Usage:
  python pai_inspect_output.py --out test_data
"""

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import read_csv  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="test_data")
    args = parser.parse_args()

    out = Path(args.out)
    manifest = read_csv(out / "block_manifest.csv")
    metadata = read_csv(out / "gp_metadata.csv")
    scores = read_csv(out / "gp_scores_long.csv")
    inventory = read_csv(out / "dropdown_inventory.csv")

    print(f"Output dir: {out.resolve()}")
    print(f"DONE files: {len(list(out.rglob('DONE.json')))}")
    print(f"FAILED files: {len(list(out.rglob('FAILED.json')))}")
    print(f"HTML pages: {len(list(out.rglob('page_*.html')))}")
    print(f"Block manifest rows: {len(manifest)}")
    print(f"Dropdown inventory rows: {len(inventory)}")
    print(f"GP metadata rows: {len(metadata)}")
    print(f"GP score rows: {len(scores)}")

    if manifest:
        print("\nManifest status counts:")
        for k, v in Counter(row.get("status", "") for row in manifest).most_common():
            print(f"  {k}: {v}")

    if metadata:
        print("\nGP metadata rows by year:")
        for k, v in Counter(row.get("year", "") for row in metadata).most_common():
            print(f"  {k}: {v}")

        print("\nTop states by GP rows:")
        for k, v in Counter(row.get("state", "") for row in metadata).most_common(15):
            print(f"  {k}: {v}")

    if inventory:
        print("\nDropdown inventory by year/level:")
        c = Counter((row.get("year", ""), row.get("level", "")) for row in inventory)
        for (year, level), count in sorted(c.items()):
            print(f"  {year} / {level}: {count}")


if __name__ == "__main__":
    main()

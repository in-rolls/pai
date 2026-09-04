#!/usr/bin/env python3
"""
Inspect scraper progress.

Usage:
  python pai_inspect_output.py --out data
"""

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_stores import BlockStore, read_global  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    out = Path(args.out)
    store = BlockStore(out)
    manifest = read_global(out, "block_manifest")
    # The rebuild writes the analysis tables under derived/; the logs stay at the root.
    metadata = read_global(out / "derived", "gp_metadata")
    scores = read_global(out / "derived", "gp_scores_long")
    inventory = read_global(out, "dropdown_inventory")

    print(f"Output dir: {out.resolve()}")
    for year in store.years():
        counts = store.counts(year)
        print(
            f"{year} [{store.mode(year)}]: DONE {counts['done']:,}  "
            f"FAILED {counts['failed']:,}  HTML {counts['html']:,}"
        )
    print(f"Block manifest rows: {len(manifest)}")
    print(f"Dropdown inventory rows: {len(inventory)}")
    if metadata or scores:
        print(f"GP metadata rows: {len(metadata)}")
        print(f"GP score rows: {len(scores)}")
    else:
        print(
            "GP metadata / score rows: not materialized (rebuild with scripts/pai_rebuild_index.py)"
        )

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

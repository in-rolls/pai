#!/usr/bin/env python3
"""
Analyze PAI scraping progress from the block manifest (CSV or parquet).

Usage:
    python scripts/scrape_progress.py [--data-dir DIR] [--year YEAR] [--detailed]
"""

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import (  # noqa: E402
    BLOCK_TABLES,
    MANIFEST_FAILED,
    MANIFEST_NO_DATA,
    MANIFEST_SUCCESS,
)
from pai_stores import BlockStore, read_global  # noqa: E402


def analyze_progress(rows: list[dict], year: str | None = None) -> dict:
    """Analyze scraping progress"""
    if year:
        rows = [r for r in rows if r["year"] == year]

    # One logical block per hierarchy id (most recent status wins): the same block
    # may appear under different output-root spellings across compacted history
    # and the live log tail.
    block_status = {}
    for row in rows:
        if not row.get("block_dir"):
            continue
        key = tuple(
            row.get(field, "") for field in ("year", "state_value", "district_value", "block_value")
        )
        block_status[key] = row

    # Categorize statuses (shared buckets; "skipped_*" mark resumable re-runs).
    success_statuses = MANIFEST_SUCCESS
    no_data_statuses = MANIFEST_NO_DATA
    failed_statuses = MANIFEST_FAILED

    results: dict[str, list[dict]] = {
        "successful": [],
        "no_data": [],
        "failed": [],
        "other": [],
    }

    for row in block_status.values():
        status = row["status"]
        if status in success_statuses:
            results["successful"].append(row)
        elif status in no_data_statuses:
            results["no_data"].append(row)
        elif status in failed_statuses:
            results["failed"].append(row)
        else:
            results["other"].append(row)

    return results


def get_state_summary(rows: list[dict]) -> dict:
    """Group results by state"""
    by_state = defaultdict(list)
    for row in rows:
        by_state[row["state"]].append(row)
    return dict(by_state)


def print_summary(results: dict, year: str):
    """Print summary table"""
    total = sum(len(v) for k, v in results.items() if k != "skipped")

    print(f"\n{'=' * 60}")
    print(f"PAI Scraping Progress: {year}")
    print(f"{'=' * 60}\n")

    print(f"{'Status':<30} {'Blocks':>10}")
    print("-" * 42)
    print(f"{'✓ Successful with data':<30} {len(results['successful']):>10}")
    print(f"{'○ No data on site':<30} {len(results['no_data']):>10}")
    print(f"{'✗ Failed (need retry)':<30} {len(results['failed']):>10}")
    if results["other"]:
        print(f"{'? Other':<30} {len(results['other']):>10}")
    print("-" * 42)
    print(f"{'Total unique blocks':<30} {total:>10}")
    print()


def print_failed_by_state(results: dict):
    """Print failed blocks grouped by state"""
    failed = results["failed"]
    if not failed:
        print("No failed blocks!\n")
        return

    by_state = get_state_summary(failed)

    print(f"\n{'Failed Blocks by State':}")
    print("-" * 50)
    for state in sorted(by_state.keys(), key=lambda s: -len(by_state[s])):
        blocks = by_state[state]
        print(f"  {state}: {len(blocks)}")
        for b in blocks[:3]:
            print(f"    - {b['district']} / {b['block']}")
        if len(blocks) > 3:
            print(f"    ... and {len(blocks) - 3} more")
    print()


def print_state_coverage(results: dict):
    """Print coverage by state"""
    all_blocks = results["successful"] + results["no_data"] + results["failed"]
    by_state = get_state_summary(all_blocks)

    success_by_state = get_state_summary(results["successful"])
    failed_by_state = get_state_summary(results["failed"])

    print(f"\n{'State Coverage':}")
    print("-" * 70)
    print(f"{'State':<35} {'Success':>8} {'No Data':>8} {'Failed':>8} {'Total':>8}")
    print("-" * 70)

    for state in sorted(by_state.keys()):
        total = len(by_state[state])
        success = len(success_by_state.get(state, []))
        failed = len(failed_by_state.get(state, []))
        no_data = total - success - failed
        print(f"{state:<35} {success:>8} {no_data:>8} {failed:>8} {total:>8}")
    print()


def check_tables_on_disk(data_dir: Path, year: str) -> int:
    """Count blocks that actually hold a wide score table, live or archived."""
    store = BlockStore(data_dir)
    if store.mode(year) == "missing":
        return 0
    return sum(1 for blk in store.iter_blocks(year, names={BLOCK_TABLES["wide"]}))


def main(argv: list[str] | None = None) -> int:
    default_data = Path(__file__).parent.parent / "data"
    parser = argparse.ArgumentParser(description="Analyze PAI scraping progress")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=default_data,
        help=f"Collection root (default: {default_data})",
    )
    parser.add_argument("--year", default="2022-2023", help="Year to analyze")
    parser.add_argument("--detailed", action="store_true", help="Show detailed state coverage")
    parser.add_argument("--failed", action="store_true", help="Show failed blocks by state")
    args = parser.parse_args(argv)
    data_dir = args.data_dir

    rows = read_global(data_dir, "block_manifest")
    if not rows:
        print(f"Error: no block_manifest.csv or .parquet under {data_dir}")
        return 1
    results = analyze_progress(rows, args.year)

    print_summary(results, args.year)

    table_count = check_tables_on_disk(data_dir, args.year)
    print(f"Score tables on disk: {table_count}")
    if table_count != len(results["successful"]):
        print(
            f"  Note: Manifest shows {len(results['successful'])} successful, "
            f"disk has {table_count} score tables"
        )
    print()

    if args.failed or args.detailed:
        print_failed_by_state(results)

    if args.detailed:
        print_state_coverage(results)

    return 0


if __name__ == "__main__":
    exit(main())

#!/usr/bin/env python3
"""
Analyze PAI scraping progress from block_manifest.csv

Usage:
    python scripts/scrape_progress.py [--year YEAR] [--detailed]
"""

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import (  # noqa: E402
    MANIFEST_FAILED,
    MANIFEST_NO_DATA,
    MANIFEST_SUCCESS,
    read_csv,
)


def analyze_progress(rows: list[dict], year: str | None = None) -> dict:
    """Analyze scraping progress"""
    if year:
        rows = [r for r in rows if r["year"] == year]

    # Track unique blocks by their directory path (most recent status wins)
    block_status = {}
    for row in rows:
        block_dir = row["block_dir"]
        if block_dir:
            block_status[block_dir] = row

    # Categorize statuses (shared buckets; "skipped_*" mark resumable re-runs).
    success_statuses = MANIFEST_SUCCESS
    no_data_statuses = MANIFEST_NO_DATA
    failed_statuses = MANIFEST_FAILED

    results = {
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


def check_csvs_on_disk(data_dir: Path, year: str) -> int:
    """Count actual CSV files on disk"""
    year_dir = data_dir / year
    if not year_dir.exists():
        return 0
    return len(list(year_dir.rglob("data_wide.csv")))


def main():
    parser = argparse.ArgumentParser(description="Analyze PAI scraping progress")
    parser.add_argument("--year", default="2022-2023", help="Year to analyze")
    parser.add_argument("--detailed", action="store_true", help="Show detailed state coverage")
    parser.add_argument("--failed", action="store_true", help="Show failed blocks by state")
    args = parser.parse_args()

    base_dir = Path(__file__).parent.parent
    manifest_path = base_dir / "test_data" / "block_manifest.csv"
    data_dir = base_dir / "test_data"

    if not manifest_path.exists():
        print(f"Error: Manifest not found at {manifest_path}")
        return 1

    rows = read_csv(manifest_path)
    results = analyze_progress(rows, args.year)

    print_summary(results, args.year)

    csv_count = check_csvs_on_disk(data_dir, args.year)
    print(f"CSVs on disk: {csv_count}")
    if csv_count != len(results["successful"]):
        print(
            f"  Note: Manifest shows {len(results['successful'])} successful, "
            f"disk has {csv_count} CSVs"
        )
    print()

    if args.failed or args.detailed:
        print_failed_by_state(results)

    if args.detailed:
        print_state_coverage(results)

    return 0


if __name__ == "__main__":
    exit(main())

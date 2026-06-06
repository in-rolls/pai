#!/usr/bin/env python3
"""Tabulate PAI scrape coverage: data volume + year-wise and state-wise distribution.

Computes everything from the on-disk per-block files (DONE.json / FAILED.json and each
block's data_wide.csv), which are the authoritative current state. This matches exactly
what scripts/build_release.py packages (both rebuild the dataset from per-block files,
not from the append-only global CSVs, which can carry duplicate multi-run rows).

Outputs (also printed as GitHub-flavored Markdown):
    <out>/pai_summary.csv           one row per year (totals + on-disk sizes)
    <out>/pai_summary_by_state.csv  one row per (year, state)

Usage:
    uv run scripts/data_summary.py [--data-dir data] [--out docs] [--years 2022-2023 2023-2024]
"""

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import (  # noqa: E402
    DATA_WIDE_CSV,
    DONE_JSON,
    FAILED_JSON,
    MANIFEST_FAILED,
    OVERALL_COL,
    STATUS_EMPTY,
    STATUS_NO_DATA,
    STATUS_WITH_DATA,
    YEARS,
    read_block_status,
    write_csv_rows,
)


def human_mb(num_bytes: int) -> float:
    return round(num_bytes / 1_048_576, 1)


def _new_state() -> dict[str, float]:
    return {
        "with_data": 0,
        "no_data": 0,
        "empty": 0,
        "failed": 0,
        "gp_count": 0,
        "score_rows": 0,
        "overall_sum": 0.0,
        "overall_n": 0,
    }


def sum_overall(data_wide: Path) -> tuple[float, int]:
    """Return (sum, n) of the overall PAI score column in a per-block data_wide.csv."""
    total, n = 0.0, 0
    try:
        with data_wide.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                v = row.get(OVERALL_COL, "")
                if v:
                    try:
                        total += float(v)
                        n += 1
                    except ValueError:
                        pass
    except OSError as e:
        print(f"warning: could not read {data_wide}: {e}", file=sys.stderr)
    return total, n


def walk_year(year_dir: Path) -> tuple[dict[str, dict], set, int, int]:
    """Per-state coverage + GP/score counts + overall-score stats + on-disk bytes."""
    per_state: dict[str, dict] = defaultdict(_new_state)
    districts: set[tuple[str, str]] = set()
    data_bytes = 0
    html_bytes = 0

    for root, _dirs, files in os.walk(year_dir):
        for fn in files:
            try:
                sz = os.path.getsize(os.path.join(root, fn))
            except OSError:
                continue
            if fn.endswith(".html"):
                html_bytes += sz
            elif fn.endswith((".csv", ".json")):
                data_bytes += sz

        if DONE_JSON not in files and FAILED_JSON not in files:
            continue
        d = read_block_status(Path(root)) or {}
        state = d.get("state") or "(unknown)"
        status = d.get("status", "?")
        ps = per_state[state]
        if status in STATUS_WITH_DATA:
            ps["with_data"] += 1
            ps["gp_count"] += int(d.get("gp_rows", 0) or 0)
            ps["score_rows"] += int(d.get("score_rows", 0) or 0)
            s, n = sum_overall(Path(root) / DATA_WIDE_CSV)
            ps["overall_sum"] += s
            ps["overall_n"] += n
        elif status in STATUS_NO_DATA:
            ps["no_data"] += 1
        elif status in STATUS_EMPTY:
            ps["empty"] += 1
        elif status in MANIFEST_FAILED:
            ps["failed"] += 1
        if d.get("district"):
            districts.add((state, d["district"]))

    return per_state, districts, data_bytes, html_bytes


def build(data_dir: Path, years: list[str]) -> tuple[list[dict], list[dict]]:
    year_rows: list[dict] = []
    state_rows: list[dict] = []

    for year in years:
        year_dir = data_dir / year
        if not year_dir.exists():
            continue
        per_state, districts, data_bytes, html_bytes = walk_year(year_dir)

        states = sorted(s for s in per_state if s != "(unknown)")
        tot = _new_state()
        states_with_data = 0
        for state in states:
            ps = per_state[state]
            mean_overall = round(ps["overall_sum"] / ps["overall_n"], 2) if ps["overall_n"] else ""
            state_rows.append(
                {
                    "year": year,
                    "state": state,
                    "blocks_with_data": ps["with_data"],
                    "blocks_no_data": ps["no_data"] + ps["empty"],
                    "gp_count": ps["gp_count"],
                    "score_rows": ps["score_rows"],
                    "mean_overall_pai": mean_overall,
                }
            )
            for k in ("with_data", "no_data", "empty", "failed", "gp_count", "score_rows"):
                tot[k] += ps[k]
            if ps["with_data"] > 0:
                states_with_data += 1

        year_rows.append(
            {
                "year": year,
                "states_total": len(states),
                "states_with_data": states_with_data,
                "districts": len(districts),
                "blocks_with_data": tot["with_data"],
                "blocks_no_data": tot["no_data"] + tot["empty"],
                "blocks_failed": tot["failed"],
                "gp_count": tot["gp_count"],
                "score_rows": tot["score_rows"],
                "data_mb": human_mb(data_bytes),
                "html_mb": human_mb(html_bytes),
            }
        )

    return year_rows, state_rows


def markdown_table(rows: list[dict]) -> str:
    if not rows:
        return "(no data)\n"
    cols = list(rows[0].keys())
    out = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join("---" for _ in cols) + " |",
    ]
    for r in rows:
        out.append(
            "| "
            + " | ".join(f"{r[c]:,}" if isinstance(r[c], int) else str(r[c]) for c in cols)
            + " |"
        )
    return "\n".join(out) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Tabulate PAI scrape coverage")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--out", default="docs")
    p.add_argument("--years", nargs="+", default=YEARS)
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"Error: data dir not found: {data_dir}", file=sys.stderr)
        return 1

    year_rows, state_rows = build(data_dir, args.years)

    out = Path(args.out)
    write_csv_rows(out / "pai_summary.csv", year_rows)
    write_csv_rows(out / "pai_summary_by_state.csv", state_rows)

    print("## PAI coverage — year totals\n")
    print(markdown_table(year_rows))
    print("\n## PAI coverage — by state and year\n")
    print(markdown_table(state_rows))
    print(f"\nWrote {out / 'pai_summary.csv'} and {out / 'pai_summary_by_state.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

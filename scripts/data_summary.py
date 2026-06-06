#!/usr/bin/env python3
"""
Tabulate PAI scrape coverage: data volume + year-wise and state-wise distribution.

Computes everything from the on-disk per-block files (DONE.json / FAILED.json and each
block's data_wide.csv), which are the authoritative current state. This matches exactly
what scripts/build_release.py packages (both rebuild the dataset from per-block files,
not from the append-only global CSVs, which can carry duplicate multi-run rows).

Outputs (also printed as GitHub-flavored Markdown):
    <out>/pai_summary.csv           one row per year (totals + on-disk sizes)
    <out>/pai_summary_by_state.csv  one row per (year, state)

Usage:
    python scripts/data_summary.py [--data-dir test_data] [--out docs] [--years 2022-2023 2023-2024]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(10_000_000)

# Block statuses (mirrors scripts/scrape_progress.py classification).
STATUS_WITH_DATA = {"done"}
STATUS_NO_DATA = {"done_no_data_available"}
STATUS_EMPTY = {"done_no_rows"}

OVERALL_COL = "overall_pai_score_score"  # wide-CSV column holding the overall PAI score


def human_mb(num_bytes: int) -> float:
    return round(num_bytes / 1_048_576, 1)


def _new_state():
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


def sum_overall(data_wide: Path):
    """Return (sum, n) of the overall PAI score column in a per-block data_wide.csv."""
    total, n = 0.0, 0
    try:
        with data_wide.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                v = row.get(OVERALL_COL, "")
                if v not in ("", None):
                    try:
                        total += float(v)
                        n += 1
                    except ValueError:
                        pass
    except Exception:
        pass
    return total, n


def walk_year(year_dir: Path):
    """Per-state coverage + GP/score counts + overall-score stats + on-disk bytes."""
    per_state = defaultdict(_new_state)
    districts = set()
    data_bytes = 0
    html_bytes = 0

    for root, _, files in os.walk(year_dir):
        for fn in files:
            try:
                sz = os.path.getsize(os.path.join(root, fn))
            except OSError:
                continue
            if fn.endswith(".html"):
                html_bytes += sz
            elif fn.endswith((".csv", ".json")):
                data_bytes += sz

        if "DONE.json" in files:
            try:
                d = json.loads((Path(root) / "DONE.json").read_text(encoding="utf-8"))
            except Exception:
                d = {}
            state = d.get("state") or "(unknown)"
            status = d.get("status", "?")
            ps = per_state[state]
            if status in STATUS_WITH_DATA:
                ps["with_data"] += 1
                ps["gp_count"] += int(d.get("gp_rows", 0) or 0)
                ps["score_rows"] += int(d.get("score_rows", 0) or 0)
                s, n = sum_overall(Path(root) / "data_wide.csv")
                ps["overall_sum"] += s
                ps["overall_n"] += n
            elif status in STATUS_NO_DATA:
                ps["no_data"] += 1
            elif status in STATUS_EMPTY:
                ps["empty"] += 1
            if d.get("district"):
                districts.add((state, d.get("district")))
        elif "FAILED.json" in files:
            try:
                d = json.loads((Path(root) / "FAILED.json").read_text(encoding="utf-8"))
            except Exception:
                d = {}
            per_state[d.get("state") or "(unknown)"]["failed"] += 1

    return per_state, districts, data_bytes, html_bytes


def build(data_dir: Path, years: list[str]):
    year_rows = []
    state_rows = []

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
            mean_overall = (
                round(ps["overall_sum"] / ps["overall_n"], 2) if ps["overall_n"] else ""
            )
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
            for k in (
                "with_data",
                "no_data",
                "empty",
                "failed",
                "gp_count",
                "score_rows",
            ):
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


def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


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
            + " | ".join(
                f"{r[c]:,}" if isinstance(r[c], int) else str(r[c]) for c in cols
            )
            + " |"
        )
    return "\n".join(out) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description="Tabulate PAI scrape coverage")
    p.add_argument("--data-dir", default="test_data")
    p.add_argument("--out", default="docs")
    p.add_argument("--years", nargs="+", default=["2022-2023", "2023-2024"])
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"Error: data dir not found: {data_dir}", file=sys.stderr)
        return 1

    year_rows, state_rows = build(data_dir, args.years)

    out = Path(args.out)
    write_csv(out / "pai_summary.csv", year_rows)
    write_csv(out / "pai_summary_by_state.csv", state_rows)

    print("## PAI coverage — year totals\n")
    print(markdown_table(year_rows))
    print("\n## PAI coverage — by state and year\n")
    print(markdown_table(state_rows))
    print(f"\nWrote {out / 'pai_summary.csv'} and {out / 'pai_summary_by_state.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

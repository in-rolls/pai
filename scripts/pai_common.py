"""Shared constants and helpers for the PAI scraper and data tools.

Single source of truth for CSV schemas, per-year page config, block-status
buckets, theme column names, and the small CSV/JSON + per-block-tree utilities
that the scraper, index rebuilder, summariser, and packager all need.
"""

import csv
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Per-year page configuration
# --------------------------------------------------------------------------- #
# The two PAI editions use different result pages. 2022-2023 (PAI 1.0) renders a
# GP-anchor-first table (#GVdataT, "legacy"); 2023-2024 (PAI 2.0) renders a flat
# State/District/Block/GP/Overall + per-theme table (#GVdata, "flat").
YEAR_CONFIGS: dict[str, dict[str, str]] = {
    "2022-2023": {
        "url": "https://pai.gov.in/PS/Public/TW-GP.aspx?s=1",
        "expected_fy_value": "1",
        "result_table": "#GVdataT",
        "layout": "legacy",
    },
    "2023-2024": {
        "url": "https://pai.gov.in/PS/Public/TW-GP-New.aspx?s=2",
        "expected_fy_value": "2",
        "result_table": "#GVdata",
        "layout": "flat",
    },
}
YEARS: list[str] = list(YEAR_CONFIGS)

# Results paginate 100 GPs per "Next 100 >>" click. The flat page never marks
# #btnNext disabled, so a short page (< 100 rows) signals the last page.
FLAT_PAGE_SIZE = 100

# Theme columns: the overall PAI score has slug "overall_pai_score", which the
# wide CSV exposes as the "<slug>_score" column below.
OVERALL_SLUG = "overall_pai_score"
OVERALL_COL = f"{OVERALL_SLUG}_score"

# Browser timeouts (milliseconds) — named so they are not scattered magic numbers.
NAV_TIMEOUT_MS = 120_000
FORM_TIMEOUT_MS = 120_000
RESULT_TIMEOUT_MS = 60_000
CLICK_TIMEOUT_MS = 30_000
COMMIT_TIMEOUT_MS = 15_000
ALERT_CLICK_TIMEOUT_MS = 5_000

# --------------------------------------------------------------------------- #
# Block-status buckets
# --------------------------------------------------------------------------- #
# Statuses written to a block's DONE.json by the scraper:
STATUS_WITH_DATA = {"done"}
STATUS_NO_DATA = {"done_no_data_available"}
STATUS_EMPTY = {"done_no_rows"}

# Statuses that appear in the append-only block_manifest.csv (a superset of the
# DONE.json statuses, plus skip markers and page/year load failures).
MANIFEST_SUCCESS = {"done", "skipped_done"}
MANIFEST_NO_DATA = {"done_no_data_available", "done_no_rows", "skipped_no_data"}
MANIFEST_FAILED = {"failed", "state_page_load_failed", "year_page_load_failed", "year_crashed"}

# --------------------------------------------------------------------------- #
# CSV schemas (header/field order for the consolidated outputs)
# --------------------------------------------------------------------------- #
BLOCK_MANIFEST_FIELDS = [
    "run_id",
    "timestamp_utc",
    "year",
    "status",
    "state",
    "state_value",
    "district",
    "district_value",
    "block",
    "block_value",
    "block_dir",
    "data_wide_csv",
    "metadata_csv",
    "scores_long_csv",
    "html_pages",
    "gp_rows",
    "score_rows",
    "page_url",
    "page_title",
    "page_heading",
    "actual_fy_value",
    "expected_fy_value",
    "error",
]

DROPDOWN_INVENTORY_FIELDS = [
    "run_id",
    "timestamp_utc",
    "year",
    "level",
    "state",
    "state_value",
    "district",
    "district_value",
    "option_text",
    "option_value",
]

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

# Per-block file names.
DONE_JSON = "DONE.json"
FAILED_JSON = "FAILED.json"
DATA_WIDE_CSV = "data_wide.csv"
METADATA_CSV = "metadata.csv"
SCORES_LONG_CSV = "scores_long.csv"

csv.field_size_limit(10_000_000)


# --------------------------------------------------------------------------- #
# CSV / JSON helpers
# --------------------------------------------------------------------------- #
def read_csv(path: Path) -> list[dict[str, str]]:
    """Read a CSV into a list of dict rows; return [] if the file is missing."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    """Append rows to a CSV, writing the header first if the file is new."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def write_csv_rows(
    path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None
) -> None:
    """Write rows to a CSV, deriving the header from the row keys if not given."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for k in row:
                if k not in fieldnames:
                    fieldnames.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        if rows:
            writer.writerows(rows)


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: dict[str, Any]) -> None:
    """Write a JSON object to disk (UTF-8, indented, non-ASCII preserved)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Per-block tree helpers
# --------------------------------------------------------------------------- #
def iter_block_dirs(year_dir: Path) -> Iterator[Path]:
    """Yield each per-block directory under year_dir (one with DONE.json/FAILED.json)."""
    for root, _dirs, files in os.walk(year_dir):
        if DONE_JSON in files or FAILED_JSON in files:
            yield Path(root)


def read_block_status(block_dir: Path) -> dict[str, Any] | None:
    """Return the block's DONE.json (preferred) or FAILED.json as a dict, or None."""
    for name in (DONE_JSON, FAILED_JSON):
        p = block_dir / name
        if p.exists():
            try:
                return read_json(p)
            except Exception:
                return {"status": "done_unreadable_json"}
    return None


def consolidate_per_block(year_dir: Path, fname: str, dst: Path) -> int:
    """Union-concat every per-block ``fname`` (e.g. data_wide.csv) under year_dir.

    The per-block files are the authoritative current state (one file per block,
    overwritten on each scrape), so this de-duplicates by construction. Returns
    the number of data rows written.
    """
    files = sorted(year_dir.rglob(fname))
    fields: list[str] = []
    seen: set[str] = set()
    for fp in files:
        try:
            with fp.open(newline="", encoding="utf-8") as f:
                header = next(csv.reader(f), [])
        except OSError:
            continue
        for c in header:
            if c not in seen:
                seen.add(c)
                fields.append(c)
    if not fields:
        dst.write_text("", encoding="utf-8")
        return 0
    n = 0
    with dst.open("w", newline="", encoding="utf-8") as fo:
        writer = csv.DictWriter(fo, fieldnames=fields, extrasaction="ignore", restval="")
        writer.writeheader()
        for fp in files:
            try:
                with fp.open(newline="", encoding="utf-8") as f:
                    for row in csv.DictReader(f):
                        writer.writerow(row)
                        n += 1
            except OSError:
                continue
    return n

"""Shared constants and helpers for the PAI scraper and data tools.

Single source of truth for table schemas, per-year page config, block-status
buckets, theme column names, and the small CSV/JSON + per-block-tree utilities
that the scraper, index rebuilder, summariser, and packager all need.
"""

import csv
import json
import os
from pathlib import Path
from typing import Any

from filelock import FileLock

# --------------------------------------------------------------------------- #
# Per-year page configuration
# --------------------------------------------------------------------------- #
# The current unified page renders both editions in the GP-anchor-first legacy
# table. PAI 2.0 originally had a separate flat page, TW-GP-New.aspx?s=2, but
# that route is now incomplete and omits the GP LGD code. Do not restore it.
YEAR_CONFIGS: dict[str, dict[str, str]] = {
    "2022-2023": {
        "url": "https://pai.gov.in/PS/Public/TW-GP.aspx?s=1",
        "expected_fy_value": "1",
        "result_table": "#GVdataT",
        "layout": "legacy",
    },
    "2023-2024": {
        "url": "https://pai.gov.in/PS/Public/TW-GP.aspx",
        "expected_fy_value": "2",
        "result_table": "#GVdataT",
        "layout": "legacy",
    },
}
YEARS: list[str] = list(YEAR_CONFIGS)

EXPECTED_SCORE_ROWS_PER_GP = 10
# Official scored-GP totals per vintage, transcribed from the Ministry's published
# state tables (sources below). PAI 1.0: PIB 2120320 (9 Apr 2025), "No of GPs
# Submitted Data" column; five States/UTs were excluded pending validation.
OFFICIAL_FINAL_GP_COUNTS: dict[str, dict[str, int]] = {
    "2022-2023": {
        "Andaman And Nicobar Islands": 70,
        "Andhra Pradesh": 13_310,
        "Arunachal Pradesh": 2_108,
        "Assam": 2_183,
        "Bihar": 8_053,
        "Chhattisgarh": 11_643,
        "Gujarat": 14_618,
        "Haryana": 6_223,
        "Himachal Pradesh": 3_328,
        "Jammu And Kashmir": 4_291,
        "Jharkhand": 4_281,
        "Karnataka": 5_907,
        "Kerala": 941,
        "Ladakh": 193,
        "Lakshadweep": 10,
        "Madhya Pradesh": 23_011,
        "Maharashtra": 27_655,
        "Manipur": 1_976,
        "Mizoram": 834,
        "Odisha": 6_794,
        "Punjab": 10_514,
        "Rajasthan": 10_634,
        "Sikkim": 199,
        "Tamil Nadu": 12_525,
        "Telangana": 12_768,
        "The Dadra And Nagar Haveli And Daman And Diu": 38,
        "Tripura": 1_176,
        "Uttar Pradesh": 23_207,
        "Uttarakhand": 7_795,
        "__india__": 216_285,
    },
    "2023-2024": {
        "Andaman And Nicobar Islands": 70,
        "Andhra Pradesh": 13_310,
        "Arunachal Pradesh": 2_108,
        "Assam": 2_192,
        "Bihar": 8_053,
        "Chhattisgarh": 11_643,
        "Goa": 188,
        "Gujarat": 14_534,
        "Haryana": 6_225,
        "Himachal Pradesh": 3_615,
        "Jammu And Kashmir": 4_291,
        "Jharkhand": 4_345,
        "Karnataka": 5_946,
        "Kerala": 941,
        "Ladakh": 193,
        "Lakshadweep": 10,
        "Madhya Pradesh": 23_011,
        "Maharashtra": 27_894,
        "Manipur": 3_041,
        "Meghalaya": 3_069,
        "Mizoram": 840,
        "Nagaland": 1_277,
        "Odisha": 6_794,
        "Puducherry": 108,
        "Punjab": 13_233,
        "Uttar Pradesh": 57_678,
        "Rajasthan": 11_037,
        "Sikkim": 199,
        "Tamil Nadu": 12_482,
        "Telangana": 12_556,
        "The Dadra And Nagar Haveli And Daman And Diu": 42,
        "Tripura": 1_176,
        "Uttarakhand": 7_766,
        "__india__": 259_867,
    },
}
OFFICIAL_FINAL_GP_COUNTS_SOURCE: dict[str, str] = {
    "2022-2023": "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2120320",
    "2023-2024": "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2294288&lang=1&reg=6",
}

# Results paginate 100 GPs per "Next 100 >>" click. The flat page never marks
# #btnNext disabled, so a short page (< 100 rows) signals the last page.
FLAT_PAGE_SIZE = 100

# Theme columns: the overall PAI score has slug "overall_pai_score", which the
# wide CSV exposes as the "<slug>_score" column below.
OVERALL_SLUG = "overall_pai_score"
OVERALL_COL = f"{OVERALL_SLUG}_score"
CANONICAL_THEME_SLUGS = (
    OVERALL_SLUG,
    "t1_poverty_free_and_enhanced_livelihoods_panchayat",
    "t2_healthy_panchayat",
    "t3_child_friendly_panchayat",
    "t4_water_sufficient_panchayat",
    "t5_clean_and_green_panchayat",
    "t6_self_sufficient_infrastructure_in_panchayat",
    "t7_socially_just_and_socially_secured_panchayat",
    "t8_panchayat_with_good_governance",
    "t9_women_friendly_panchayat",
)
WIDE_THEME_FIELDS = tuple(
    f"{slug}_{suffix}"
    for slug in CANONICAL_THEME_SLUGS
    for suffix in ("score", "grade", "band", "raw")
)

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
# Table schemas (field order for the per-block Parquet tables and the logs)
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
    "metadata_file",
    "scores_file",
    "wide_file",
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

GP_UNIVERSE_FIELDS = [
    "year",
    "state",
    "state_value",
    "district",
    "district_value",
    "block",
    "block_value",
    "gp_code",
    "gp_name",
    "source_url",
    "retrieved_utc",
    "source_sha256",
]

# Per-block file names. The three tables are typed Parquet written by
# pai_contracts.write_block_tables; they are the parsed cache that the rendered
# HTML cannot replace without a browser.
DONE_JSON = "DONE.json"
FAILED_JSON = "FAILED.json"
BLOCK_TABLES = {
    "metadata": "metadata.parquet",
    "scores": "scores_long.parquet",
    "wide": "data_wide.parquet",
}
BLOCK_TABLE_FIELDS = {
    "metadata": GP_METADATA_FIELDS,
    "scores": GP_SCORE_FIELDS,
    "wide": [*GP_METADATA_FIELDS, *WIDE_THEME_FIELDS],
}

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
    # Several scraper workers (split by state) append to the same provenance logs.
    # The file is opened only once the lock is held, so the header check sees
    # whatever the previous holder wrote.
    with FileLock(f"{path}.lock"), path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if f.tell() == 0:
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
    """Write a JSON object atomically (UTF-8, indented, non-ASCII preserved).

    DONE.json is the resumability checkpoint; a process killed mid-write must
    leave the previous file, not a truncated one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Per-block tree helpers
# --------------------------------------------------------------------------- #
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

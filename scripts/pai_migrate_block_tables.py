#!/usr/bin/env python3
"""One-shot: convert per-block CSV caches to typed Parquet through the block contract.

Every block with a DONE.json and the three legacy CSVs is read, validated with
validate_block_rows, written with write_block_tables (typed schema, read-back
check), and its DONE.json keys are moved to the Parquet names. The CSVs are
deleted only after the Parquet row counts match DONE.json. A block whose CSVs
were touched in the last two minutes is skipped as in flight.

Usage:
  uv run scripts/pai_migrate_block_tables.py --data-dir data [--years 2023-2024]
"""

import argparse
import copy
import csv
import os
import sys
import time
from pathlib import Path

import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filelock import FileLock
from pai_common import (  # noqa: E402
    BLOCK_MANIFEST_FIELDS,
    BLOCK_TABLES,
    DONE_JSON,
    read_json,
    write_json,
)
from pai_contracts import (  # noqa: E402
    apply_reviewed_score_vector_links,
    apply_reviewed_theme_headers,
    load_score_value_exceptions,
    validate_block_rows,
    write_block_tables,
)

LEGACY_CSVS = {
    "metadata": "metadata.csv",
    "scores": "scores_long.csv",
    "wide": "data_wide.csv",
}
LEGACY_DONE_KEYS = {
    "metadata_csv": "metadata_file",
    "scores_long_csv": "scores_file",
    "data_wide_csv": "wide_file",
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row.pop("block_data_wide_csv", None)
    return rows


def migrate_block(
    block_dir: Path, allowed_null_scores: set, min_age_seconds: float
) -> dict[str, int] | None:
    done_path = block_dir / DONE_JSON
    csvs = {kind: block_dir / name for kind, name in LEGACY_CSVS.items()}
    if not done_path.exists() or not all(p.exists() for p in csvs.values()):
        return None
    newest = max(p.stat().st_mtime for p in csvs.values())
    if time.time() - newest < min_age_seconds:
        return None
    done = read_json(done_path)
    tables = {kind: read_rows(path) for kind, path in csvs.items()}
    # Validate what the rebuild will see: reviewed identity repairs, then the reviewed
    # theme-header dictionary (which the scraper now applies at parse time).
    checked = copy.deepcopy(tables)
    apply_reviewed_score_vector_links(checked["metadata"], checked["scores"], checked["wide"])
    apply_reviewed_theme_headers(checked["scores"], checked["wide"])
    validate_block_rows(
        checked["metadata"],
        checked["scores"],
        checked["wide"],
        require_current_pai2_identity=False,
        allowed_null_scores=allowed_null_scores,
    )
    # Persist canonical theme columns but the parser's own identities: identity
    # repairs stay a reviewed step of the rebuild, where they are counted.
    for kind, rows in tables.items():
        for raw_row, checked_row in zip(rows, checked[kind], strict=True):
            checked_row["gp_code"] = raw_row.get("gp_code", "")
            checked_row["scorecard_url"] = raw_row.get("scorecard_url", "")
    written = write_block_tables(block_dir, checked["metadata"], checked["scores"], checked["wide"])
    counts = {kind: pq.read_metadata(path).num_rows for kind, path in written.items()}
    if counts["metadata"] != int(done.get("gp_rows", 0) or 0):
        raise AssertionError(f"{block_dir}: GP rows {counts['metadata']} != DONE.json")
    if counts["scores"] != int(done.get("score_rows", 0) or 0):
        raise AssertionError(f"{block_dir}: score rows {counts['scores']} != DONE.json")
    for old_key, new_key in LEGACY_DONE_KEYS.items():
        done.pop(old_key, None)
        done[new_key] = str(block_dir / BLOCK_TABLES[new_key.split("_")[0]])
    write_json(done_path, done)
    freed = sum(p.stat().st_size for p in csvs.values())
    for path in csvs.values():
        path.unlink()
    return {
        "blocks": 1,
        "gp_rows": counts["metadata"],
        "csv_bytes": freed,
        "parquet_bytes": sum(p.stat().st_size for p in written.values()),
    }


def rewrite_manifest_header(data_dir: Path) -> bool:
    """Rename the manifest's legacy path columns; values keep pointing at the block."""
    path = data_dir / "block_manifest.csv"
    if not path.exists():
        return False
    # Same lock as append_csv_rows: a worker appending between our read and the
    # replace would otherwise lose its row.
    with FileLock(f"{path}.lock"):
        return _rewrite_manifest_header_locked(path)


def _rewrite_manifest_header_locked(path: Path) -> bool:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames == BLOCK_MANIFEST_FIELDS:
            return False
        rows = list(reader)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=BLOCK_MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            for old_key, new_key in LEGACY_DONE_KEYS.items():
                value = row.pop(old_key, "")
                row[new_key] = value.replace(".csv", ".parquet") if value else ""
            writer.writerow(row)
    tmp.replace(path)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--years", nargs="+", default=None)
    parser.add_argument(
        "--min-age-seconds",
        type=float,
        default=120,
        help="Skip blocks whose CSVs changed more recently than this (a scraper may be writing)",
    )
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    allowed_null_scores = set(load_score_value_exceptions())
    years = args.years or sorted(p.name for p in data_dir.iterdir() if p.is_dir() and "-" in p.name)
    for year in years:
        totals = {"blocks": 0, "gp_rows": 0, "csv_bytes": 0, "parquet_bytes": 0}
        skipped = 0
        for done_path in sorted((data_dir / year).glob("*/*/*/DONE.json")):
            result = migrate_block(done_path.parent, allowed_null_scores, args.min_age_seconds)
            if result is None:
                skipped += 1
                continue
            for key, value in result.items():
                totals[key] += value
        print(
            f"{year}: migrated {totals['blocks']:,} blocks, {totals['gp_rows']:,} GP rows; "
            f"csv {totals['csv_bytes'] / 1e6:.1f} MB -> "
            f"parquet {totals['parquet_bytes'] / 1e6:.1f} MB; "
            f"skipped {skipped:,} (no CSVs or in flight)"
        )
    if rewrite_manifest_header(data_dir):
        print("block_manifest.csv: renamed legacy path columns")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Verify the committed PAI data package against its manifest and key contract."""

import argparse
import json
import tomllib
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from build_data_package import (
    PUBLIC_FIELDS,
    PUBLIC_SCHEMA,
    ROOT,
    SCORE_FIELDS,
    coverage_audit,
    file_entry,
    state_coverage_audit,
    validate_git_sizes,
    validate_release_coverage,
    validate_score_values,
)


def package_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def keys(table: pa.Table) -> set[tuple[str, str]]:
    return set(
        zip(
            table.column("year").to_pylist(),
            table.column("gp_code").to_pylist(),
            strict=True,
        )
    )


def verify(data_dir: Path) -> dict[str, Any]:
    manifest_path = data_dir / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != package_version():
        raise AssertionError("data manifest version differs from pyproject.toml")
    expected_names = {"pai_gp.parquet"}
    actual_names = {path.name for path in data_dir.iterdir() if path.name != "MANIFEST.json"}
    if actual_names != expected_names or set(manifest.get("files", {})) != expected_names:
        raise AssertionError(
            f"data package members differ: expected={sorted(expected_names)}, "
            f"actual={sorted(actual_names)}"
        )
    actual_files = {filename: file_entry(data_dir / filename) for filename in expected_names}
    for filename in sorted(expected_names):
        if actual_files[filename] != manifest["files"][filename]:
            raise AssertionError(f"{filename}: manifest metadata or checksum differs")
    if manifest.get("package_bytes") != validate_git_sizes(actual_files):
        raise AssertionError("manifest package size differs from packaged data")

    table = pq.read_table(data_dir / "pai_gp.parquet")
    if table.column_names != PUBLIC_FIELDS or table.schema != PUBLIC_SCHEMA:
        raise AssertionError("public data schema differs from the declared package schema")
    for field in PUBLIC_FIELDS[:12]:
        if any(not str(value or "").strip() for value in table.column(field).to_pylist()):
            raise AssertionError(f"public hierarchy field {field} contains blanks")
    public_keys = keys(table)
    if len(public_keys) != table.num_rows:
        raise AssertionError("public data contain duplicate (year, gp_code) keys")

    rows = table.select(["score_available", "scorecard_url", *SCORE_FIELDS]).to_pylist()
    for row in rows:
        if row["score_available"]:
            if not str(row["scorecard_url"] or "").strip():
                raise AssertionError("score-available row has a blank scorecard URL")
        elif row["scorecard_url"] is not None or any(
            row[field] is not None for field in SCORE_FIELDS
        ):
            raise AssertionError("unscored hierarchy row contains score values")

    scores = table.filter(table.column("score_available"))
    official_counts_checked = validate_release_coverage(scores)
    if manifest.get("official_counts_checked") != official_counts_checked:
        raise AssertionError("manifest official-count checks differ from packaged data")
    if manifest.get("score_quality") != validate_score_values(scores):
        raise AssertionError("manifest score-quality checks differ from packaged data")
    observed_years = {str(value) for value in table.column("year").to_pylist()}
    manifest_years = manifest.get("years", [])
    if len(manifest_years) != len(set(manifest_years)) or set(manifest_years) != observed_years:
        raise AssertionError("manifest vintages differ from packaged data")

    if manifest.get("coverage") != coverage_audit(table):
        raise AssertionError("manifest coverage differs from packaged data")
    if manifest.get("coverage_by_state") != state_coverage_audit(table):
        raise AssertionError("manifest state coverage differs from packaged data")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "release")
    args = parser.parse_args()
    manifest = verify(args.data_dir)
    print(
        f"PASS version {manifest['version']}: "
        f"{manifest['files']['pai_gp.parquet']['rows']:,} GP-year rows"
    )


if __name__ == "__main__":
    main()

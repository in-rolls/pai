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
    PACKAGE_SCORE_FIELDS,
    ROOT,
    SCORE_FIELDS,
    SCORE_ID_FIELDS,
    file_entry,
    validate_git_sizes,
    validate_release_coverage,
    validate_score_values,
)
from pai_common import GP_UNIVERSE_FIELDS
from pai_contracts import typed_schema


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
    expected_names = {"pai_gp_scores.parquet", "pai_gp_universe.parquet"}
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

    scores = pq.read_table(data_dir / "pai_gp_scores.parquet")
    expected_score_schema = pa.schema(
        [
            *[pa.field(field, pa.string()) for field in SCORE_ID_FIELDS],
            *[pa.field(field, pa.float64()) for field in SCORE_FIELDS],
        ]
    )
    if scores.column_names != PACKAGE_SCORE_FIELDS or scores.schema != expected_score_schema:
        raise AssertionError("public score schema differs from the declared package schema")
    for field in SCORE_ID_FIELDS:
        if any(not str(value or "").strip() for value in scores.column(field).to_pylist()):
            raise AssertionError(f"public score identity field {field} contains blanks")
    official_counts_checked = validate_release_coverage(scores)
    if manifest.get("official_counts_checked") != official_counts_checked:
        raise AssertionError("manifest official-count checks differ from packaged data")
    if manifest.get("score_quality") != validate_score_values(scores):
        raise AssertionError("manifest score-quality checks differ from packaged data")
    observed_years = {str(value) for value in scores.column("year").to_pylist()}
    manifest_years = manifest.get("years", [])
    if len(manifest_years) != len(set(manifest_years)) or set(manifest_years) != observed_years:
        raise AssertionError("manifest vintages differ from packaged data")

    universe = pq.read_table(data_dir / "pai_gp_universe.parquet")
    if universe.schema != typed_schema(GP_UNIVERSE_FIELDS, "universe"):
        raise AssertionError("public universe schema differs from the declared package schema")
    for field in GP_UNIVERSE_FIELDS:
        if any(not str(value or "").strip() for value in universe.column(field).to_pylist()):
            raise AssertionError(f"public universe field {field} contains blanks")
    score_keys = keys(scores)
    universe_keys = keys(universe)
    if len(score_keys) != scores.num_rows or len(universe_keys) != universe.num_rows:
        raise AssertionError("public data contain duplicate (year, gp_code) keys")
    if score_keys != universe_keys:
        raise AssertionError("public scores and universe keys differ")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "release")
    args = parser.parse_args()
    manifest = verify(args.data_dir)
    print(
        f"PASS version {manifest['version']}: "
        f"{manifest['files']['pai_gp_scores.parquet']['rows']:,} GP-year rows"
    )


if __name__ == "__main__":
    main()

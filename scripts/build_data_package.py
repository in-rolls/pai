#!/usr/bin/env python3
"""Build the small, versioned PAI data package from a validated derived bundle."""

import argparse
import hashlib
import json
import math
import shutil
import tempfile
import tomllib
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pai_common import (
    CANONICAL_THEME_SLUGS,
    GP_UNIVERSE_FIELDS,
    OFFICIAL_FINAL_GP_COUNTS,
    YEAR_CONFIGS,
)
from pai_contracts import load_score_value_exceptions, typed_schema

ROOT = Path(__file__).parent.parent
SCORE_ID_FIELDS = [
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
]
SCORE_FIELDS = [f"{slug}_score" for slug in CANONICAL_THEME_SLUGS]
PACKAGE_SCORE_FIELDS = [*SCORE_ID_FIELDS, *SCORE_FIELDS]
REQUIRED_RELEASE_YEARS = tuple(YEAR_CONFIGS)
MAX_GIT_FILE_BYTES = 50 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_scores(source: Path, destination: Path) -> pa.Table:
    """Project the validated wide table to the stable public score schema."""
    table = pq.read_table(source, columns=PACKAGE_SCORE_FIELDS)
    expected = pa.schema(
        [
            *[pa.field(field, pa.string()) for field in SCORE_ID_FIELDS],
            *[pa.field(field, pa.float64()) for field in SCORE_FIELDS],
        ]
    )
    if table.schema != expected:
        raise AssertionError(f"{source}: public score schema changed: {table.schema}")
    rows = table.to_pylist()
    for field in SCORE_ID_FIELDS:
        if any(not str(row[field] or "").strip() for row in rows):
            raise AssertionError(f"public score identity field {field} contains blanks")
    keys = [(str(row["year"]), str(row["gp_code"])) for row in rows]
    if len(keys) != len(set(keys)):
        raise AssertionError("public score data contain duplicate (year, gp_code) keys")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination, compression="zstd", compression_level=7)
    return table


def copy_universe(source: Path, destination: Path) -> pa.Table:
    """Copy the official denominator using its exact declared schema."""
    table = pq.read_table(source)
    expected = typed_schema(GP_UNIVERSE_FIELDS, "universe")
    if table.schema != expected:
        raise AssertionError(f"{source}: public universe schema changed: {table.schema}")
    rows = table.to_pylist()
    for field in GP_UNIVERSE_FIELDS:
        if any(not str(row[field] or "").strip() for row in rows):
            raise AssertionError(f"public universe field {field} contains blanks")
    keys = list(
        zip(
            table.column("year").to_pylist(),
            table.column("gp_code").to_pylist(),
            strict=True,
        )
    )
    if len(keys) != len(set(keys)):
        raise AssertionError("public universe contains duplicate (year, gp_code) keys")
    pq.write_table(table, destination, compression="zstd", compression_level=7)
    return table


def validate_release_coverage(table: pa.Table) -> dict[str, dict[str, int]]:
    """Require every published vintage and the official PAI 2.0 totals."""
    years = {str(value) for value in table.column("year").to_pylist()}
    required = set(REQUIRED_RELEASE_YEARS)
    if years != required:
        raise AssertionError(
            f"release vintages differ: missing={sorted(required - years)}, "
            f"unexpected={sorted(years - required)}"
        )
    counts = Counter(
        zip(
            table.column("year").to_pylist(),
            table.column("state").to_pylist(),
            strict=True,
        )
    )
    checked: dict[str, dict[str, int]] = {}
    for year, controls in OFFICIAL_FINAL_GP_COUNTS.items():
        expected_states = {state for state in controls if not state.startswith("__")}
        actual_states = {state for observed_year, state in counts if observed_year == year}
        if actual_states != expected_states:
            raise AssertionError(
                f"official {year} state coverage differs: "
                f"missing={sorted(expected_states - actual_states)}, "
                f"unexpected={sorted(actual_states - expected_states)}"
            )
        for state in sorted(expected_states):
            actual = counts[(year, state)]
            expected = controls[state]
            if actual != expected:
                raise AssertionError(
                    f"official GP count failed for {state} {year}: {actual} != {expected}"
                )
            checked[f"{year}:{state}"] = {"actual": actual, "expected": expected}
        actual_india = sum(
            count for (observed_year, _), count in counts.items() if observed_year == year
        )
        expected_india = controls["__india__"]
        if actual_india != expected_india:
            raise AssertionError(
                f"official India GP count failed for {year}: {actual_india} != {expected_india}"
            )
        checked[f"{year}:__india__"] = {
            "actual": actual_india,
            "expected": expected_india,
        }
    return checked


def validate_score_values(table: pa.Table) -> dict[str, Any]:
    """Reject invalid values and require the exact reviewed source-null set."""
    years = {str(value) for value in table.column("year").to_pylist()}
    expected_nulls = {key for key in load_score_value_exceptions() if key[0] in years}
    actual_nulls: set[tuple[str, str, str]] = set()
    year_values = table.column("year").to_pylist()
    code_values = table.column("gp_code").to_pylist()
    for slug, field in zip(CANONICAL_THEME_SLUGS, SCORE_FIELDS, strict=True):
        for year, gp_code, value in zip(
            year_values, code_values, table.column(field).to_pylist(), strict=True
        ):
            key = (str(year), str(gp_code), slug)
            if value is None:
                actual_nulls.add(key)
            elif not math.isfinite(value) or not 0 <= value <= 100:
                raise AssertionError(f"invalid public score for {key}: {value!r}")
    if actual_nulls != expected_nulls:
        raise AssertionError(
            "reviewed public score-null set differs: "
            f"observed_only={sorted(actual_nulls - expected_nulls)}, "
            f"configured_only={sorted(expected_nulls - actual_nulls)}"
        )
    return {
        "reviewed_null_scores": len(actual_nulls),
        "score_quality_flag": ("source_blank_preserved_as_null" if actual_nulls else "complete"),
    }


def validate_source_manifest(derived_dir: Path) -> dict[str, Any]:
    """Bind the package inputs to the audited derived collection manifest."""
    path = derived_dir / "collection_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    declared = manifest.get("derived", {})
    for filename in ("gp_scores_wide.parquet", "gp_universe.parquet"):
        source = derived_dir / filename
        actual = {
            "rows": pq.read_metadata(source).num_rows,
            "sha256": sha256_file(source),
            "schema": str(pq.read_schema(source)),
        }
        if declared.get(filename) != actual:
            raise AssertionError(f"{filename}: derived collection manifest differs")
    return manifest


def package_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


def file_entry(path: Path) -> dict[str, Any]:
    parquet = pq.ParquetFile(path)
    return {
        "rows": parquet.metadata.num_rows,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "schema": str(parquet.schema_arrow),
    }


def validate_git_sizes(files: dict[str, dict[str, Any]]) -> int:
    """Keep every committed artifact below GitHub's large-file warning threshold."""
    oversized = {
        name: int(entry["bytes"])
        for name, entry in files.items()
        if int(entry["bytes"]) >= MAX_GIT_FILE_BYTES
    }
    if oversized:
        raise AssertionError(f"data files reached the 50 MiB Git warning threshold: {oversized}")
    return sum(int(entry["bytes"]) for entry in files.values())


def build(derived_dir: Path, out: Path) -> dict[str, Any]:
    """Create and atomically promote a checked two-table public data package."""
    source_manifest = derived_dir / "collection_manifest.json"
    scores_source = derived_dir / "gp_scores_wide.parquet"
    universe_source = derived_dir / "gp_universe.parquet"
    for required in (source_manifest, scores_source, universe_source):
        if not required.exists():
            raise FileNotFoundError(required)
    validate_source_manifest(derived_dir)

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out.name}.staging-", dir=out.parent) as name:
        stage = Path(name)
        scores_path = stage / "pai_gp_scores.parquet"
        universe_path = stage / "pai_gp_universe.parquet"
        scores = project_scores(scores_source, scores_path)
        universe = copy_universe(universe_source, universe_path)
        official_counts_checked = validate_release_coverage(scores)
        score_quality = validate_score_values(scores)
        score_keys = set(
            zip(
                scores.column("year").to_pylist(),
                scores.column("gp_code").to_pylist(),
                strict=True,
            )
        )
        universe_keys = set(
            zip(
                universe.column("year").to_pylist(),
                universe.column("gp_code").to_pylist(),
                strict=True,
            )
        )
        if score_keys != universe_keys:
            raise AssertionError(
                "public scores and universe differ: "
                f"unscored={len(universe_keys - score_keys)}, "
                f"unexpected={len(score_keys - universe_keys)}"
            )
        files = {
            scores_path.name: file_entry(scores_path),
            universe_path.name: file_entry(universe_path),
        }
        manifest = {
            "version": package_version(),
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "unit": "one Gram Panchayat in one PAI fiscal-year vintage",
            "key": ["year", "gp_code"],
            "years": list(REQUIRED_RELEASE_YEARS),
            "official_counts_checked": official_counts_checked,
            "score_quality": score_quality,
            "package_bytes": validate_git_sizes(files),
            "source_collection_manifest_sha256": sha256_file(source_manifest),
            "files": files,
        }
        (stage / "MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        restored = json.loads((stage / "MANIFEST.json").read_text(encoding="utf-8"))
        for filename, entry in restored["files"].items():
            path = stage / filename
            if sha256_file(path) != entry["sha256"]:
                raise AssertionError(f"{filename}: package checksum round trip failed")

        backup = out.parent / f".{out.name}.backup-{uuid.uuid4().hex}"
        if out.exists():
            out.replace(backup)
        try:
            stage.replace(out)
        except Exception:
            if backup.exists():
                backup.replace(out)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derived-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "release")
    args = parser.parse_args()
    manifest = build(args.derived_dir, args.out)
    print(
        f"Wrote {sum(item['rows'] for item in manifest['files'].values()):,} table rows "
        f"to {args.out}"
    )


if __name__ == "__main__":
    main()

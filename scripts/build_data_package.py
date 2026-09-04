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
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pai_collect_universe import HIERARCHY_EXCLUSIONS, load_hierarchy_exclusions
from pai_common import (
    CANONICAL_THEME_SLUGS,
    GP_UNIVERSE_FIELDS,
    OFFICIAL_FINAL_GP_COUNTS,
    YEAR_CONFIGS,
)
from pai_contracts import (
    expected_state_count,
    load_official_count_exceptions,
    load_score_value_exceptions,
    typed_schema,
)

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
PUBLIC_UNIVERSE_FIELDS = [
    "year",
    "state",
    "state_value",
    "district",
    "district_value",
    "block",
    "block_value",
    "gp_code",
    "gp_name",
    "hierarchy_source_url",
    "hierarchy_retrieved_utc",
    "hierarchy_source_sha256",
]
PUBLIC_FIELDS = [
    *PUBLIC_UNIVERSE_FIELDS,
    "score_available",
    "scorecard_url",
    *SCORE_FIELDS,
]
PUBLIC_SCHEMA = pa.schema(
    [
        *[pa.field(field, pa.string(), nullable=False) for field in PUBLIC_UNIVERSE_FIELDS],
        pa.field("score_available", pa.bool_(), nullable=False),
        pa.field("scorecard_url", pa.string()),
        *[pa.field(field, pa.float64()) for field in SCORE_FIELDS],
    ]
)
REQUIRED_RELEASE_YEARS = tuple(YEAR_CONFIGS)
MAX_GIT_FILE_BYTES = 50 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_scores(source: Path) -> pa.Table:
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
    return table


def read_universe(source: Path) -> pa.Table:
    """Read the full hierarchy denominator using its exact declared schema."""
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
    return table


def build_public_table(
    scores: pa.Table,
    universe: pa.Table,
    destination: Path,
    hierarchy_exclusions: dict[tuple[str, str, str], dict[str, str]] | None = None,
) -> tuple[pa.Table, list[dict[str, str]]]:
    """Left-join scores onto the full GP universe without imputing missing outcomes."""
    hierarchy_exclusions = hierarchy_exclusions or {}
    score_rows = {(str(row["year"]), str(row["gp_code"])): row for row in scores.to_pylist()}
    universe_rows = universe.to_pylist()
    universe_keys = {(str(row["year"]), str(row["gp_code"])) for row in universe_rows}
    unexpected = sorted(set(score_rows) - universe_keys)
    if unexpected:
        raise AssertionError(f"public scores contain GPs outside the universe: {unexpected[:10]}")

    rows: list[dict[str, Any]] = []
    corrections: list[dict[str, str]] = []
    for source in universe_rows:
        key = (str(source["year"]), str(source["gp_code"]))
        score = score_rows.get(key)
        if score is not None:
            mismatches = [
                field
                for field in ("state_value", "district_value", "block_value")
                if str(score[field]) != str(source[field])
            ]
            if mismatches:
                exclusion_key = (key[0], str(score["state_value"]), str(score["district_value"]))
                exclusion = hierarchy_exclusions.get(exclusion_key)
                reviewed = (
                    mismatches == ["state_value"]
                    and exclusion is not None
                    and exclusion["correct_state_value"] == str(source["state_value"])
                )
                if not reviewed:
                    raise AssertionError(
                        f"score/universe hierarchy differs for {key}: {sorted(mismatches)}"
                    )
                corrections.append(
                    {
                        "year": key[0],
                        "gp_code": key[1],
                        "district_value": str(source["district_value"]),
                        "score_state_value": str(score["state_value"]),
                        "hierarchy_state_value": str(source["state_value"]),
                    }
                )
        rows.append(
            {
                "year": source["year"],
                "state": source["state"],
                "state_value": source["state_value"],
                "district": source["district"],
                "district_value": source["district_value"],
                "block": source["block"],
                "block_value": source["block_value"],
                "gp_code": source["gp_code"],
                "gp_name": source["gp_name"],
                "hierarchy_source_url": source["source_url"],
                "hierarchy_retrieved_utc": source["retrieved_utc"],
                "hierarchy_source_sha256": source["source_sha256"],
                "score_available": score is not None,
                "scorecard_url": score["scorecard_url"] if score is not None else None,
                **{field: score[field] if score is not None else None for field in SCORE_FIELDS},
            }
        )
    rows.sort(
        key=lambda row: (
            row["year"],
            row["state_value"],
            row["district_value"],
            row["block_value"],
            row["gp_code"],
        )
    )
    table = pa.Table.from_pylist(rows, schema=PUBLIC_SCHEMA)
    if table.column_names != PUBLIC_FIELDS:
        raise AssertionError("public table field order changed")
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination, compression="zstd", compression_level=7)
    return table, corrections


def coverage_audit(table: pa.Table) -> dict[str, dict[str, int]]:
    """Count hierarchy, scored, and unscored rows by vintage."""
    rows = table.select(["year", "score_available"]).to_pylist()
    audit: dict[str, dict[str, int]] = {}
    for year in sorted({str(row["year"]) for row in rows}):
        selected = [row for row in rows if row["year"] == year]
        scored = sum(bool(row["score_available"]) for row in selected)
        audit[year] = {
            "universe_rows": len(selected),
            "scored_rows": scored,
            "unscored_rows": len(selected) - scored,
        }
    return audit


def state_coverage_audit(table: pa.Table) -> dict[str, dict[str, int | str]]:
    """Expose score availability by state so selection is visible to analysts."""
    rows = table.select(["year", "state", "state_value", "score_available"]).to_pylist()
    counts: dict[str, dict[str, int | str]] = {}
    for row in rows:
        key = f"{row['year']}:{row['state_value']}"
        entry = counts.setdefault(
            key,
            {
                "state": str(row["state"]),
                "universe_rows": 0,
                "scored_rows": 0,
                "unscored_rows": 0,
            },
        )
        entry["universe_rows"] = int(entry["universe_rows"]) + 1
        field = "scored_rows" if row["score_available"] else "unscored_rows"
        entry[field] = int(entry[field]) + 1
    return dict(sorted(counts.items()))


def drop_unvalidated_state_rows(scores: pa.Table) -> tuple[pa.Table, dict[str, int]]:
    """Remove score rows from states the Ministry did not validate for that vintage.

    The portal can display scores for such a state (Meghalaya in 2022-23); they
    were never part of the official index, so the release treats those GPs as
    unscored and records how many rows it set aside.
    """
    years = scores.column("year").to_pylist()
    states = scores.column("state").to_pylist()
    keep = []
    dropped: dict[str, int] = {}
    for year, state in zip(years, states, strict=True):
        controls = OFFICIAL_FINAL_GP_COUNTS.get(year)
        unvalidated = bool(controls) and state not in controls
        keep.append(not unvalidated)
        if unvalidated:
            dropped[f"{year}:{state}"] = dropped.get(f"{year}:{state}", 0) + 1
    return scores.filter(pa.array(keep)), dropped


def validate_release_coverage(
    table: pa.Table,
    hierarchy_corrections: list[dict[str, str]] | None = None,
    state_names: dict[str, str] | None = None,
) -> dict[str, dict[str, int]]:
    """Require every published vintage and the official totals of each controlled vintage.

    The Ministry counts a GP under the state the portal's score table shows. The
    release table carries the LGD hierarchy's state instead, so when verifying the
    release the reviewed corrections recorded in its manifest are folded back.
    """
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
    if hierarchy_corrections:
        if state_names is None:
            state_names = dict(
                zip(
                    table.column("state_value").to_pylist(),
                    table.column("state").to_pylist(),
                    strict=True,
                )
            )
        for correction in hierarchy_corrections:
            year = str(correction["year"])
            counts[(year, state_names[str(correction["hierarchy_state_value"])])] -= 1
            counts[(year, state_names[str(correction["score_state_value"])])] += 1
    checked: dict[str, dict[str, Any]] = {}
    count_exceptions = load_official_count_exceptions()
    for year, controls in OFFICIAL_FINAL_GP_COUNTS.items():
        expected_states = {state for state in controls if not state.startswith("__")}
        actual_states = {state for observed_year, state in counts if observed_year == year}
        if actual_states != expected_states:
            raise AssertionError(
                f"official {year} state coverage differs: "
                f"missing={sorted(expected_states - actual_states)}, "
                f"unexpected={sorted(actual_states - expected_states)}"
            )
        shortfall = 0
        for state in sorted(expected_states):
            actual = counts[(year, state)]
            expected, exception = expected_state_count(year, state, controls, count_exceptions)
            if actual != expected:
                raise AssertionError(
                    f"official GP count failed for {state} {year}: {actual} != {expected}"
                )
            entry: dict[str, Any] = {"actual": actual, "expected": controls[state]}
            if exception is not None:
                entry["reviewed_portal_count"] = expected
                entry["evidence"] = exception["evidence"]
                shortfall += controls[state] - expected
            checked[f"{year}:{state}"] = entry
        actual_india = sum(
            count for (observed_year, _), count in counts.items() if observed_year == year
        )
        expected_india = controls["__india__"] - shortfall
        if actual_india != expected_india:
            raise AssertionError(
                f"official India GP count failed for {year}: {actual_india} != {expected_india}"
            )
        checked[f"{year}:__india__"] = {
            "actual": actual_india,
            "expected": controls["__india__"],
            "reviewed_portal_shortfall": shortfall,
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


SUCCESSFUL_BLOCK_STATUSES = {
    "done",
    "done_no_data_available",
    "done_no_rows",
    "skipped_done",
    "skipped_no_data",
}


def validate_block_coverage(universe: pa.Table, manifest: pa.Table) -> dict[str, int]:
    """Every hierarchy block must have a successful collection outcome.

    Without this, a block that failed or was never scraped has no score rows and
    its GPs would be published as portal nonpublication rather than as a gap.
    """
    key_fields = ["year", "state_value", "district_value", "block_value"]
    universe_blocks = {tuple(row.values()) for row in universe.select(key_fields).to_pylist()}
    latest = {
        tuple(str(row[field]) for field in key_fields): str(row["status"])
        for row in manifest.select([*key_fields, "status"]).to_pylist()
    }
    uncollected = sorted(
        block for block in universe_blocks if latest.get(block) not in SUCCESSFUL_BLOCK_STATUSES
    )
    if uncollected:
        raise AssertionError(
            f"{len(uncollected)} hierarchy block(s) lack a successful collection outcome; "
            f"first: {uncollected[:5]}"
        )
    return {"hierarchy_blocks": len(universe_blocks)}


def validate_universe_provenance(derived_dir: Path, universe_dir: Path) -> dict[str, Any]:
    """The release denominator must be the independent nationwide hierarchy crawl.

    A derived bundle whose universe came from cached blocks can silently omit
    whole unscored branches, and no score-count control would notice. So the
    derived manifest must carry the crawl's checksum, that checksum must match
    the crawl's own manifest, and the derived universe must hold exactly the
    crawl's per-vintage row counts.
    """
    derived_manifest = json.loads((derived_dir / "collection_manifest.json").read_text("utf-8"))
    declared_sha = derived_manifest.get("contracts", {}).get("universe_source_sha256")
    if not declared_sha:
        raise AssertionError(
            "derived bundle was not built from the independent universe crawl "
            "(no universe_source_sha256 in its collection manifest)"
        )
    crawl_manifest = json.loads((universe_dir / "collection_manifest.json").read_text("utf-8"))
    crawl_sha = crawl_manifest["parquet"]["sha256"]
    if declared_sha != crawl_sha:
        raise AssertionError("derived bundle was built from a different universe crawl")
    universe = pq.read_table(derived_dir / "gp_universe.parquet", columns=["year", "state"])
    per_year = {
        str(item["values"]): int(item["counts"])
        for item in pc.value_counts(universe.column("year")).to_pylist()
    }
    states_by_year: dict[str, set[str]] = {}
    for year, state in zip(
        universe.column("year").to_pylist(), universe.column("state").to_pylist(), strict=True
    ):
        states_by_year.setdefault(str(year), set()).add(str(state))
    checked: dict[str, int] = {}
    for year in REQUIRED_RELEASE_YEARS:
        if year not in crawl_manifest.get("counts_by_year", {}):
            raise AssertionError(f"release vintages differ: the universe crawl has no {year}")
        expected = int(crawl_manifest["counts_by_year"][year]["parquet_rows"])
        if per_year.get(year, 0) != expected:
            raise AssertionError(
                f"derived universe holds {per_year.get(year, 0)} rows for {year}; "
                f"the crawl recorded {expected}"
            )
        # Nationwide scope: a crawl restricted to some states is self-consistent, so
        # require every controlled state and every officially unscored state.
        controls = OFFICIAL_FINAL_GP_COUNTS.get(year, {})
        required = {state for state in controls if not state.startswith("__")}
        required |= {
            str(entry["name"])
            for entry in crawl_manifest.get("states_without_published_score_control", {}).get(
                year, []
            )
        }
        missing = sorted(required - states_by_year.get(year, set()))
        if missing:
            raise AssertionError(f"universe crawl for {year} is not nationwide: missing {missing}")
        inventory = int(crawl_manifest["counts_by_year"][year].get("states", len(required)))
        if inventory != len(required):
            raise AssertionError(
                f"universe crawl for {year} lists {inventory} states; "
                f"controls and unscored states account for {len(required)}"
            )
        checked[year] = expected
    return {"universe_source_sha256": crawl_sha, "universe_rows_by_year": checked}


def validate_source_manifest(derived_dir: Path) -> dict[str, Any]:
    """Bind the package inputs to the audited derived collection manifest."""
    path = derived_dir / "collection_manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    declared = manifest.get("derived", {})
    for filename in ("gp_scores_wide.parquet", "gp_universe.parquet", "block_manifest.parquet"):
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


def build(derived_dir: Path, out: Path, universe_dir: Path | None = None) -> dict[str, Any]:
    """Create and atomically promote a checked universe-left public data package."""
    universe_dir = universe_dir if universe_dir is not None else ROOT / "runs" / "pai_universe"
    source_manifest = derived_dir / "collection_manifest.json"
    scores_source = derived_dir / "gp_scores_wide.parquet"
    universe_source = derived_dir / "gp_universe.parquet"
    blocks_source = derived_dir / "block_manifest.parquet"
    for required in (source_manifest, scores_source, universe_source, blocks_source):
        if not required.exists():
            raise FileNotFoundError(required)
    validate_source_manifest(derived_dir)
    universe_provenance = validate_universe_provenance(derived_dir, universe_dir)

    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out.name}.staging-", dir=out.parent) as name:
        stage = Path(name)
        public_path = stage / "pai_gp.parquet"
        scores, unvalidated_state_rows = drop_unvalidated_state_rows(project_scores(scores_source))
        universe = read_universe(universe_source)
        block_coverage = validate_block_coverage(universe, pq.read_table(blocks_source))
        official_counts_checked = validate_release_coverage(scores)
        score_quality = validate_score_values(scores)
        public, hierarchy_corrections = build_public_table(
            scores,
            universe,
            public_path,
            load_hierarchy_exclusions(HIERARCHY_EXCLUSIONS),
        )
        files = {public_path.name: file_entry(public_path)}
        manifest = {
            "version": package_version(),
            "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "unit": "one hierarchy Gram Panchayat in one PAI fiscal-year vintage",
            "key": ["year", "gp_code"],
            "years": list(REQUIRED_RELEASE_YEARS),
            "official_counts_checked": official_counts_checked,
            "block_coverage": block_coverage,
            "universe_provenance": universe_provenance,
            "unvalidated_state_rows_excluded": unvalidated_state_rows,
            "score_quality": score_quality,
            "coverage": coverage_audit(public),
            "coverage_by_state": state_coverage_audit(public),
            "score_hierarchy_corrections": hierarchy_corrections,
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
    parser.add_argument(
        "--universe-dir",
        type=Path,
        default=ROOT / "runs" / "pai_universe",
        help="Independent hierarchy crawl the derived bundle must have been built from",
    )
    args = parser.parse_args()
    manifest = build(args.derived_dir, args.out, args.universe_dir)
    print(
        f"Wrote {sum(item['rows'] for item in manifest['files'].values()):,} table rows "
        f"to {args.out}"
    )


if __name__ == "__main__":
    main()

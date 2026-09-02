#!/usr/bin/env python3
"""Build typed global Parquet tables and enforce the PAI data contracts.

Per-block compatibility CSVs are resumable intermediate state. The Parquet
tables written here are the canonical derived data products. Raw rendered HTML
and context/status JSON remain under the block tree and are never folded into a
derived table.

Usage:
  uv run scripts/pai_rebuild_index.py --data-dir runs/pai2_up \
    --expected-state-gps "Uttar Pradesh=57678"
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import (  # noqa: E402
    BLOCK_MANIFEST_FIELDS,
    DATA_WIDE_CSV,
    DONE_JSON,
    DROPDOWN_INVENTORY_FIELDS,
    GP_METADATA_FIELDS,
    GP_SCORE_FIELDS,
    GP_UNIVERSE_FIELDS,
    METADATA_CSV,
    SCORES_LONG_CSV,
    WIDE_THEME_FIELDS,
)
from pai_contracts import (  # noqa: E402
    apply_reviewed_score_vector_links,
    apply_reviewed_theme_headers,
    csv_to_typed_parquet,
    rows_to_typed_parquet,
    typed_schema,
    validate_global_tables,
    validate_universe_parquet,
    write_collection_manifest,
)
from pai_stores import BlockStore, read_global  # noqa: E402

ANALYSIS_BLOCK_FILES = {
    "metadata": METADATA_CSV,
    "scores": SCORES_LONG_CSV,
    "wide": DATA_WIDE_CSV,
}


def parse_expected(items: list[str]) -> dict[tuple[str, str], int]:
    """Parse YEAR:STATE=N or STATE=N (defaults to PAI 2.0)."""
    parsed: dict[tuple[str, str], int] = {}
    for item in items:
        label, sep, count = item.rpartition("=")
        if not sep or not label or not count:
            raise argparse.ArgumentTypeError(f"invalid expected count: {item!r}")
        if ":" in label:
            year, state = label.split(":", 1)
        else:
            year, state = "2023-2024", label
        parsed[(year.strip(), state.strip())] = int(count)
    return parsed


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def read_source_rows(data_dir: Path, stem: str) -> list[dict[str, str]]:
    """Prefer the live append log, falling back to compacted Parquet."""
    csv_path = data_dir / f"{stem}.csv"
    return read_csv_rows(csv_path) if csv_path.exists() else read_global(data_dir, stem)


def consolidate_analysis_tables(
    data_dir: Path, destinations: dict[str, Path], years: list[str] | None = None
) -> dict[str, int]:
    """Stream all three block tables together, applying reviewed identity repairs."""
    store = BlockStore(data_dir)
    selected_years = years if years is not None else store.years()
    names = set(ANALYSIS_BLOCK_FILES.values())
    fields = {
        "metadata": list(GP_METADATA_FIELDS),
        "scores": list(GP_SCORE_FIELDS),
        "wide": [*GP_METADATA_FIELDS, *WIDE_THEME_FIELDS],
    }

    unknown_destinations = set(destinations) - set(ANALYSIS_BLOCK_FILES)
    if unknown_destinations:
        raise ValueError(f"unknown analysis destinations: {sorted(unknown_destinations)}")
    counts = {name: 0 for name in destinations}
    with ExitStack() as stack:
        writers: dict[str, csv.DictWriter] = {}
        for name, destination in destinations.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            handle = stack.enter_context(destination.open("w", newline="", encoding="utf-8"))
            writer = csv.DictWriter(
                handle, fieldnames=fields[name], extrasaction="ignore", restval=""
            )
            writer.writeheader()
            writers[name] = writer

        for year in selected_years:
            for block in store.iter_blocks(year, names=names):
                tables = {
                    name: block.rows(filename) for name, filename in ANALYSIS_BLOCK_FILES.items()
                }
                apply_reviewed_score_vector_links(
                    tables["metadata"], tables["scores"], tables["wide"]
                )
                apply_reviewed_theme_headers(tables["scores"], tables["wide"])
                block_rel = block.rel.as_posix()
                for name, rows in tables.items():
                    if name not in writers:
                        continue
                    for row in rows:
                        if "block_dir" in row:
                            row["block_dir"] = block_rel
                        if "block_data_wide_csv" in row:
                            row["block_data_wide_csv"] = f"{block_rel}/{DATA_WIDE_CSV}"
                        if "block_html_file" in row and row.get("block_page"):
                            page = int(row["block_page"])
                            row["block_html_file"] = f"{block_rel}/html/page_{page:03d}.html"
                        writers[name].writerow(row)
                        counts[name] += 1
    return counts


def promote_bundle(stage: Path, out: Path) -> None:
    """Promote a fully checked directory without exposing a partial replacement."""
    backup = out.parent / f".{out.name}.backup-{uuid.uuid4().hex}"
    had_existing = out.exists()
    if had_existing:
        out.replace(backup)
    try:
        stage.replace(out)
    except Exception:
        if had_existing and backup.exists():
            backup.replace(out)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def latest_manifest_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """One current row per block, retaining non-block year failures separately."""
    latest: dict[tuple[str, ...], dict[str, str]] = {}
    extra: list[dict[str, str]] = []
    for row in rows:
        block_dir = row.get("block_dir", "")
        if block_dir:
            key = tuple(
                row.get(field, "")
                for field in ("year", "state_value", "district_value", "block_value")
            )
            latest[key] = row
        else:
            extra.append(row)
    return extra + list(latest.values())


def unique_inventory_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    latest: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = tuple(
            row.get(field, "")
            for field in (
                "year",
                "level",
                "state_value",
                "district_value",
                "option_value",
            )
        )
        latest[key] = row
    return list(latest.values())


def status_row_conservation(data_dir: Path, years: list[str] | None = None) -> dict[str, int]:
    expected_gp = expected_scores = done_blocks = 0
    store = BlockStore(data_dir)
    for year in years or store.years():
        for block in store.iter_blocks(year, names={DONE_JSON}):
            status = block.json(DONE_JSON)
            if not status or status.get("status") != "done":
                continue
            done_blocks += 1
            expected_gp += int(status.get("gp_rows", 0) or 0)
            expected_scores += int(status.get("score_rows", 0) or 0)
    return {
        "done_blocks": done_blocks,
        "done_gp_rows": expected_gp,
        "done_score_rows": expected_scores,
    }


def _clean_handler_name(code: str, raw_name: str) -> str:
    match = re.match(r"^(.*?)\s*\[(\d+)\]\s*$", raw_name)
    if not match:
        return raw_name.strip()
    if match.group(2) != code:
        raise AssertionError(f"handler GP name/code mismatch: {raw_name!r} versus {code}")
    return match.group(1).strip()


def write_universe_from_store(data_dir: Path, dst: Path, years: list[str]) -> int:
    """Stream cached official handler responses into canonical typed Parquet."""
    schema = typed_schema(GP_UNIVERSE_FIELDS, "universe")
    writer = pq.ParquetWriter(dst, schema, compression="zstd", compression_level=7)
    buffered: list[dict[str, str]] = []
    total = 0

    def flush() -> None:
        nonlocal total
        if not buffered:
            return
        batch = pa.RecordBatch.from_pylist(buffered, schema=schema)
        writer.write_batch(batch)
        total += batch.num_rows
        buffered.clear()

    try:
        store = BlockStore(data_dir)
        for year in years:
            for block in store.iter_blocks(
                year, names={"gp_universe.json", "gp_universe_provenance.json"}
            ):
                raw_payload = block.files.get("gp_universe.json")
                payload = block.json("gp_universe.json")
                provenance = block.json("gp_universe_provenance.json")
                if raw_payload is None or payload is None or provenance is None:
                    continue
                if payload.get("columns") != ["gp_code", "nm"]:
                    raise AssertionError(f"{block.rel}: unexpected GP-universe schema")
                actual_sha = hashlib.sha256(raw_payload).hexdigest()
                if provenance.get("sha256") != actual_sha:
                    raise AssertionError(f"{block.rel}: GP-universe source checksum mismatch")
                if int(provenance.get("http_status", 0)) != 200:
                    raise AssertionError(f"{block.rel}: GP-universe source was not HTTP 200")
                if int(provenance.get("gp_rows", -1)) != len(payload.get("rows", [])):
                    raise AssertionError(f"{block.rel}: GP-universe provenance row count mismatch")
                for raw in payload.get("rows", []):
                    code, raw_name = str(raw[0]), str(raw[1])
                    buffered.append(
                        {
                            "year": year,
                            "state": str(provenance["state"]),
                            "state_value": str(provenance["state_value"]),
                            "district": str(provenance["district"]),
                            "district_value": str(provenance["district_value"]),
                            "block": str(provenance["block"]),
                            "block_value": str(provenance["block_value"]),
                            "gp_code": code,
                            "gp_name": _clean_handler_name(code, raw_name),
                            "source_url": str(provenance["url"]),
                            "retrieved_utc": str(provenance["retrieved_utc"]),
                            "source_sha256": str(provenance["sha256"]),
                        }
                    )
                    if len(buffered) >= 10_000:
                        flush()
        flush()
    finally:
        writer.close()
    if total == 0:
        raise AssertionError(f"no cached official GP-universe rows found under {data_dir}")
    return total


def copy_universe_years(src: Path, dst: Path, years: list[str]) -> int:
    """Stream selected years from a standalone universe crawl into a release build."""
    source = pq.ParquetFile(src)
    schema = typed_schema(GP_UNIVERSE_FIELDS, "universe")
    if source.schema_arrow != schema:
        raise AssertionError(f"{src}: unexpected GP-universe schema")
    wanted = pa.array(years, type=pa.string())
    total = 0
    with pq.ParquetWriter(dst, schema, compression="zstd", compression_level=7) as writer:
        for batch in source.iter_batches(batch_size=65_536):
            year_column = batch.column(batch.schema.get_field_index("year"))
            selected = batch.filter(pc.is_in(year_column, value_set=wanted))
            if selected.num_rows:
                writer.write_batch(selected)
                total += selected.num_rows
    if total == 0:
        raise AssertionError(f"{src}: no GP-universe rows for years {years}")
    return total


def build(
    data_dir: Path,
    out: Path,
    expected: dict[tuple[str, str], int],
    years: list[str] | None = None,
    require_national: bool = False,
    universe_data_dir: Path | None = None,
) -> dict[str, Any]:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{out.name}.staging-", dir=out.parent) as stage_name:
        stage = Path(stage_name)
        outputs = {
            "metadata": stage / "gp_metadata.parquet",
            "scores": stage / "gp_scores_long.parquet",
            "wide": stage / "gp_scores_wide.parquet",
            "block_manifest": stage / "block_manifest.parquet",
            "inventory": stage / "dropdown_inventory.parquet",
            "universe": stage / "gp_universe.parquet",
        }
        tmp = stage / "csv"
        tmp.mkdir()
        temp_csvs = {
            "metadata": tmp / "gp_metadata.csv",
            "scores": tmp / "gp_scores_long.csv",
            "wide": tmp / "gp_scores_wide.csv",
        }
        consolidated = consolidate_analysis_tables(data_dir, temp_csvs, years)
        if any(rows == 0 for rows in consolidated.values()):
            raise AssertionError(f"no analysis rows found under {data_dir}")

        csv_to_typed_parquet(temp_csvs["metadata"], outputs["metadata"], "metadata")
        csv_to_typed_parquet(temp_csvs["scores"], outputs["scores"], "scores")
        csv_to_typed_parquet(temp_csvs["wide"], outputs["wide"], "wide")
        shutil.rmtree(tmp)

        raw_manifest = read_source_rows(data_dir, "block_manifest")
        raw_inventory = read_source_rows(data_dir, "dropdown_inventory")
        if years is not None:
            year_set = set(years)
            raw_manifest = [row for row in raw_manifest if row.get("year") in year_set]
            raw_inventory = [row for row in raw_inventory if row.get("year") in year_set]
        manifest_rows = latest_manifest_rows(raw_manifest)
        inventory_rows = unique_inventory_rows(raw_inventory)
        rows_to_typed_parquet(
            manifest_rows, BLOCK_MANIFEST_FIELDS, outputs["block_manifest"], "manifest"
        )
        rows_to_typed_parquet(
            inventory_rows, DROPDOWN_INVENTORY_FIELDS, outputs["inventory"], "inventory"
        )

        selected_years = years or BlockStore(data_dir).years()
        if universe_data_dir is None:
            write_universe_from_store(data_dir, outputs["universe"], selected_years)
        else:
            universe_source = universe_data_dir
            if universe_source.is_dir():
                universe_source = universe_source / "gp_universe.parquet"
            copy_universe_years(universe_source, outputs["universe"], selected_years)

        contract = validate_global_tables(
            outputs["metadata"], outputs["scores"], outputs["wide"], expected, require_national
        )
        contract.update(validate_universe_parquet(outputs["universe"], outputs["metadata"]))
        status_counts = status_row_conservation(data_dir, years)
        if status_counts["done_gp_rows"] != contract["gp_rows"]:
            raise AssertionError(
                "DONE.json/global metadata row conservation failed: "
                f"{status_counts['done_gp_rows']} != {contract['gp_rows']}"
            )
        if status_counts["done_score_rows"] != contract["score_rows"]:
            raise AssertionError(
                "DONE.json/global score row conservation failed: "
                f"{status_counts['done_score_rows']} != {contract['score_rows']}"
            )
        contract.update(status_counts)
        contract["manifest_rows"] = len(manifest_rows)
        contract["inventory_rows"] = len(inventory_rows)
        write_collection_manifest(
            stage / "collection_manifest.json", list(outputs.values()), contract, data_dir
        )
        promote_bundle(stage, out)
    return contract


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default="data", help="Where the block store lives")
    parser.add_argument("--out", help="Where to write Parquet (default: --data-dir/derived)")
    parser.add_argument(
        "--expected-state-gps",
        action="append",
        default=[],
        metavar="[YEAR:]STATE=N",
        help="Hard official count control; repeat for multiple states",
    )
    parser.add_argument(
        "--national-official-controls",
        action="store_true",
        help="Require all 33 official PAI 2.0 state totals and the 259,867 India total",
    )
    parser.add_argument(
        "--universe-data-dir",
        help="Standalone universe crawl directory or gp_universe.parquet path",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out = Path(args.out) if args.out else data_dir / "derived"
    expected = parse_expected(args.expected_state_gps)
    contract = build(
        data_dir,
        out,
        expected,
        require_national=args.national_official_controls,
        universe_data_dir=Path(args.universe_data_dir) if args.universe_data_dir else None,
    )
    print(json.dumps(contract, indent=2))
    print(f"Wrote typed derived data -> {out.resolve()}")


if __name__ == "__main__":
    main()

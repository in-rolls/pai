#!/usr/bin/env python3
"""Build typed global Parquet tables and enforce the PAI data contracts.

The per-block typed Parquet tables are the resumable parsed cache. The global
Parquet tables written here are the canonical derived data products, streamed
block by block through the reviewed identity repairs. Raw rendered HTML and
context/status JSON remain under the block tree and are never folded into a
derived table.

Usage:
  uv run scripts/pai_rebuild_index.py --data-dir runs/pai2_up \
    --expected-state-gps "Uttar Pradesh=57678"
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_collect_universe import request_url  # noqa: E402
from pai_common import (  # noqa: E402
    BLOCK_MANIFEST_FIELDS,
    BLOCK_TABLE_FIELDS,
    BLOCK_TABLES,
    DONE_JSON,
    DROPDOWN_INVENTORY_FIELDS,
    GP_UNIVERSE_FIELDS,
    YEAR_CONFIGS,
)
from pai_contracts import (  # noqa: E402
    apply_reviewed_score_vector_links,
    apply_reviewed_theme_headers,
    canonicalize_score_gp_codes,
    rows_to_table,
    rows_to_typed_parquet,
    typed_schema,
    validate_global_tables,
    validate_universe_parquet,
    write_collection_manifest,
)
from pai_scraper_resumable import universe_rows  # noqa: E402
from pai_stores import BlockStore, read_global  # noqa: E402


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


def read_source_rows(data_dir: Path, stem: str) -> list[dict[str, str]]:
    """The whole append log: compacted Parquet history plus the live CSV tail."""
    return read_global(data_dir, stem)


def consolidate_analysis_tables(
    data_dir: Path, destinations: dict[str, Path], years: list[str] | None = None
) -> dict[str, int]:
    """Stream every block's three tables into typed global Parquet, block by block.

    The reviewed identity repairs run on each block's rows before they are typed, so
    memory stays bounded by one block rather than the national long table.
    """
    store = BlockStore(data_dir)
    selected_years = years if years is not None else store.years()
    unknown_destinations = set(destinations) - set(BLOCK_TABLES)
    if unknown_destinations:
        raise ValueError(f"unknown analysis destinations: {sorted(unknown_destinations)}")
    counts = {name: 0 for name in destinations}
    writers: dict[str, pq.ParquetWriter] = {}
    try:
        for name, destination in destinations.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            schema = typed_schema(list(BLOCK_TABLE_FIELDS[name]), name)
            writers[name] = pq.ParquetWriter(
                destination, schema, compression="zstd", compression_level=7
            )
        for year in selected_years:
            for block in store.iter_blocks(year, names=set(BLOCK_TABLES.values())):
                tables = {name: block.rows(filename) for name, filename in BLOCK_TABLES.items()}
                apply_reviewed_score_vector_links(
                    tables["metadata"], tables["scores"], tables["wide"]
                )
                canonicalize_score_gp_codes(tables["metadata"], tables["scores"], tables["wide"])
                apply_reviewed_theme_headers(tables["scores"], tables["wide"])
                block_rel = block.rel.as_posix()
                for name, rows in tables.items():
                    if name not in writers or not rows:
                        continue
                    for row in rows:
                        # Cache paths are stored relative to the collection root so a
                        # moved or renamed staging run leaves no stale paths behind.
                        row["block_dir"] = block_rel
                        if row.get("block_page"):
                            page = int(row["block_page"])
                            row["block_html_file"] = f"{block_rel}/html/page_{page:03d}.html"
                    table = rows_to_table(rows, list(BLOCK_TABLE_FIELDS[name]), name)
                    writers[name].write_table(table)
                    counts[name] += table.num_rows
    finally:
        for writer in writers.values():
            writer.close()
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
            finished: set[str] = set()
            cached: set[str] = set()
            for block in store.iter_blocks(
                year, names={DONE_JSON, "gp_universe.json", "gp_universe_provenance.json"}
            ):
                if block.exists(DONE_JSON):
                    finished.add(block.rel.as_posix())
                raw_payload = block.files.get("gp_universe.json")
                payload = block.json("gp_universe.json")
                provenance = block.json("gp_universe_provenance.json")
                if raw_payload is None or payload is None or provenance is None:
                    continue
                # The cache sits in the block's source/ subdirectory.
                cached.add(block.rel.parent.as_posix())
                if payload.get("columns") != ["gp_code", "nm"]:
                    raise AssertionError(f"{block.rel}: unexpected GP-universe schema")
                actual_sha = hashlib.sha256(raw_payload).hexdigest()
                if provenance.get("sha256") != actual_sha:
                    raise AssertionError(f"{block.rel}: GP-universe source checksum mismatch")
                if int(provenance.get("http_status", 0)) != 200:
                    raise AssertionError(f"{block.rel}: GP-universe source was not HTTP 200")
                if int(provenance.get("gp_rows", -1)) != len(universe_rows(payload)):
                    raise AssertionError(f"{block.rel}: GP-universe provenance row count mismatch")
                for code, raw_name in universe_rows(payload):
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
                            "source_url": request_url(
                                str(provenance["url"]), provenance.get("params") or {}
                            ),
                            "retrieved_utc": str(provenance["retrieved_utc"]),
                            "source_sha256": str(provenance["sha256"]),
                        }
                    )
                    if len(buffered) >= 10_000:
                        flush()
            # A finished block without its handler response would silently drop
            # every GP of that block from the universe.
            uncached = sorted(finished - cached)
            if uncached:
                raise AssertionError(
                    f"{len(uncached)} finished block(s) lack their universe cache; "
                    f"first: {uncached[0]}"
                )
        flush()
    finally:
        writer.close()
    if total == 0:
        raise AssertionError(f"no cached official GP-universe rows found under {data_dir}")
    return total


def verify_universe_source(universe_dir: Path, years: list[str]) -> dict[str, Any]:
    """Bind a standalone universe crawl to its own collection manifest.

    A truncated or hand-edited gp_universe.parquet would shrink the denominator
    and every downstream coverage check would still pass, so the Parquet must
    match the checksum and row counts its collector recorded.
    """
    manifest_path = universe_dir / "collection_manifest.json"
    parquet_path = universe_dir / "gp_universe.parquet"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual_sha = hashlib.sha256(parquet_path.read_bytes()).hexdigest()
    if manifest["parquet"]["sha256"] != actual_sha:
        raise AssertionError(f"{parquet_path}: checksum differs from its collection manifest")
    table = pq.read_table(parquet_path, columns=["year"])
    if table.num_rows != int(manifest["row_count"]):
        raise AssertionError(f"{parquet_path}: {table.num_rows} rows != manifest row_count")
    per_year = pc.value_counts(table.column("year")).to_pylist()
    counts = {str(item["values"]): int(item["counts"]) for item in per_year}
    for year in years:
        declared = manifest["counts_by_year"].get(year, {}).get("parquet_rows")
        if declared is None:
            raise AssertionError(f"{manifest_path}: no universe collection for {year}")
        if counts.get(year, 0) != int(declared):
            raise AssertionError(
                f"{parquet_path}: {counts.get(year, 0)} rows for {year} != declared {declared}"
            )
    return {"universe_source_sha256": actual_sha, "universe_source_rows": table.num_rows}


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
    if require_national and universe_data_dir is None:
        # A hierarchy branch that failed before its blocks were enumerated leaves
        # no cache, so a store-derived denominator can be silently short; the
        # release denominator must come from the standalone crawl.
        raise AssertionError(
            "--national-official-controls requires --universe-data-dir "
            "(the independent hierarchy crawl)"
        )
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
        consolidated = consolidate_analysis_tables(
            data_dir, {k: outputs[k] for k in ("metadata", "scores", "wide")}, years
        )
        if any(rows == 0 for rows in consolidated.values()):
            raise AssertionError(f"no analysis rows found under {data_dir}")

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
        universe_provenance: dict[str, Any] = {}
        if universe_data_dir is None:
            write_universe_from_store(data_dir, outputs["universe"], selected_years)
        else:
            universe_source = universe_data_dir
            if universe_source.is_dir():
                universe_provenance = verify_universe_source(universe_source, selected_years)
                universe_source = universe_source / "gp_universe.parquet"
            elif require_national:
                raise AssertionError(
                    "--national-official-controls needs the universe crawl directory "
                    "(with its collection_manifest.json), not a bare Parquet file"
                )
            copy_universe_years(universe_source, outputs["universe"], selected_years)

        contract = validate_global_tables(
            outputs["metadata"], outputs["scores"], outputs["wide"], expected, require_national
        )
        contract.update(validate_universe_parquet(outputs["universe"], outputs["metadata"]))
        contract.update(universe_provenance)
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
        "--years", nargs="+", choices=list(YEAR_CONFIGS), help="limit the derived bundle"
    )
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
        years=args.years,
        require_national=args.national_official_controls,
        universe_data_dir=Path(args.universe_data_dir) if args.universe_data_dir else None,
    )
    print(json.dumps(contract, indent=2))
    print(f"Wrote typed derived data -> {out.resolve()}")


if __name__ == "__main__":
    main()

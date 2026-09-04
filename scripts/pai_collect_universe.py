#!/usr/bin/env python3
"""Collect the official PAI Gram Panchayat universe for every configured year.

The portal exposes a year-specific LGD hierarchy as three JSON handlers.  This
collector preserves every raw response and its request provenance, then writes
one typed, compact Parquet table with one row per year and LGD GP code.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import (  # noqa: E402
    GP_UNIVERSE_FIELDS,
    OFFICIAL_FINAL_GP_COUNTS,
    OFFICIAL_FINAL_GP_COUNTS_SOURCE,
    YEAR_CONFIGS,
)

BASE_URL = "https://pai.gov.in"
DISTRICTS_URL = f"{BASE_URL}/Handlers/Y_Lgd_Districts.ashx"
BLOCKS_URL = f"{BASE_URL}/Handlers/Y_LGD_Blocks.ashx"
GPS_URL = f"{BASE_URL}/Handlers/Y_GPs_By_LGD_Block.ashx"
USER_AGENT = "pai-scraper/0.1 (+https://github.com/in-rolls/pai)"
ROOT = Path(__file__).parent.parent
HIERARCHY_EXCLUSIONS = ROOT / "config" / "hierarchy_exclusions.csv"

UNIVERSE_SCHEMA = pa.schema(
    [pa.field(field, pa.string(), nullable=False) for field in GP_UNIVERSE_FIELDS]
)

RequestFn = Callable[[str, float], "HttpResponse"]
SleepFn = Callable[[float], None]
HierarchyKey = tuple[str, str, str]


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    url: str


@dataclass(frozen=True)
class CachedResponse:
    body: bytes
    provenance: dict[str, Any]


class StateOptionParser(HTMLParser):
    """Read value/label pairs from the portal's state selector."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_state_select = False
        self.option_value: str | None = None
        self.option_text: list[str] = []
        self.options: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "select" and attributes.get("id") == "ddl_State":
            self.in_state_select = True
        elif tag == "option" and self.in_state_select:
            self.option_value = attributes.get("value", "")
            self.option_text = []

    def handle_data(self, data: str) -> None:
        if self.option_value is not None:
            self.option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self.option_value is not None:
            self.options.append((self.option_value, "".join(self.option_text)))
            self.option_value = None
            self.option_text = []
        elif tag == "select" and self.in_state_select:
            self.in_state_select = False


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    atomic_write_bytes(path, f"{payload}\n".encode())


def atomic_write_parquet(path: Path, table: pa.Table) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        pq.write_table(table, temporary, compression="zstd")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_exclusion_source(exclusion: Mapping[str, str], provenance: Mapping[str, object]) -> None:
    """A reviewed exclusion is evidence about one response; a changed response needs new review."""
    if str(provenance.get("sha256")) != exclusion["source_sha256"]:
        raise ValueError(
            "hierarchy exclusion for district "
            f"{exclusion['district_value']} was reviewed against response "
            f"{exclusion['source_sha256'][:12]}, but the district list now hashes to "
            f"{str(provenance.get('sha256'))[:12]}; re-review before excluding"
        )


def load_hierarchy_exclusions(path: Path) -> dict[HierarchyKey, dict[str, str]]:
    """Load reviewed district placements that the portal assigns to the wrong state."""
    exclusions: dict[HierarchyKey, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (
                row.get("year", "").strip(),
                row.get("state_value", "").strip(),
                row.get("district_value", "").strip(),
            )
            correct_state = row.get("correct_state_value", "").strip()
            source_sha256 = row.get("source_sha256", "").strip()
            evidence = row.get("evidence", "").strip()
            if (
                key[0] not in YEAR_CONFIGS
                or not all(value.isdecimal() for value in key[1:])
                or not correct_state.isdecimal()
                or correct_state == key[1]
                or len(source_sha256) != 64
                or not evidence
            ):
                raise ValueError(f"{path}: every hierarchy exclusion requires valid evidence")
            if key in exclusions:
                raise ValueError(f"{path}: duplicate hierarchy exclusion {key}")
            exclusions[key] = row
    return exclusions


def request_url(url: str, params: Mapping[str, str]) -> str:
    return f"{url}?{urllib.parse.urlencode(sorted(params.items()))}" if params else url


def http_get(url: str, timeout: float) -> HttpResponse:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                status=response.status,
                body=response.read(),
                url=response.geturl(),
            )
    except urllib.error.HTTPError as error:
        return HttpResponse(status=error.code, body=error.read(), url=error.geturl())


class RateLimiter:
    def __init__(
        self,
        delay: float,
        *,
        sleeper: SleepFn = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if delay < 0:
            raise ValueError("delay must be non-negative")
        self.delay = delay
        self.sleeper = sleeper
        self.clock = clock
        self.last_request: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.last_request is not None:
            remaining = self.delay - (now - self.last_request)
            if remaining > 0:
                self.sleeper(remaining)
        self.last_request = self.clock()


def fetch_with_retries(
    url: str,
    *,
    timeout: float,
    retries: int,
    retry_backoff: float,
    limiter: RateLimiter,
    request_fn: RequestFn = http_get,
    sleeper: SleepFn = time.sleep,
    validate: Callable[[bytes], object] | None = None,
) -> HttpResponse:
    if retries < 0:
        raise ValueError("retries must be non-negative")
    if retry_backoff < 0:
        raise ValueError("retry_backoff must be non-negative")

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            response = request_fn(url, timeout)
            if response.status == 200:
                if validate is not None:
                    try:
                        validate(response.body)
                    except ValueError as error:
                        last_error = error
                    else:
                        return response
                else:
                    return response
            elif response.status not in {408, 425, 429} and response.status < 500:
                raise RuntimeError(f"GET {url} returned HTTP {response.status}")
            else:
                last_error = RuntimeError(f"GET {url} returned HTTP {response.status}")
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            last_error = error

        if attempt < retries:
            sleeper(retry_backoff * (2**attempt))

    raise RuntimeError(f"GET {url} failed after {retries + 1} attempts") from last_error


def canonical_id(value: object, *, level: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{level} ID must not be boolean")
    if isinstance(value, int):
        identifier = str(value)
    elif isinstance(value, str):
        identifier = value.strip()
    else:
        raise ValueError(f"{level} ID must be an integer or decimal string: {value!r}")
    if not identifier.isdecimal() or int(identifier) <= 0:
        raise ValueError(f"{level} ID must be a positive decimal value: {value!r}")
    return identifier


def clean_name(label: str) -> str:
    name = re.sub(r"\s*\[[^]]+]\s*$", "", label).strip()
    if not name:
        raise ValueError(f"empty hierarchy label after removing LGD suffix: {label!r}")
    return name


def clean_hierarchy_name(label: str, identifier: str, *, level: str) -> str:
    match = re.fullmatch(r"\s*(.*)\s*\[([^][]+)]\s*", label)
    if match is None:
        raise ValueError(f"{level} label does not end in its LGD ID: {label!r}")
    suffix = match.group(2).strip()
    if suffix != identifier:
        raise ValueError(
            f"{level} label LGD suffix {suffix!r} disagrees with row ID {identifier!r}"
        )
    name = match.group(1).strip()
    if not name:
        raise ValueError(f"empty {level} label before LGD suffix: {label!r}")
    return name


def parse_hierarchy_json(
    body: bytes,
    *,
    expected_id_column: str,
    level: str,
) -> list[dict[str, str]]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{level} response is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{level} response must be a JSON object")
    if payload.get("columns") != [expected_id_column, "nm"]:
        raise ValueError(
            f"{level} response columns must be {[expected_id_column, 'nm']!r}; "
            f"got {payload.get('columns')!r}"
        )
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raise ValueError(f"{level} response rows must be a list")

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row_number, row in enumerate(raw_rows, start=1):
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError(f"{level} row {row_number} must contain exactly two values")
        if row == [None, None]:
            if row_number != len(raw_rows):
                raise ValueError(f"{level} null sentinel must be the final response row")
            continue
        identifier = canonical_id(row[0], level=level)
        if identifier in seen:
            raise ValueError(f"{level} response contains duplicate ID {identifier}")
        if not isinstance(row[1], str) or not row[1].strip():
            raise ValueError(f"{level} row {row_number} has an empty or non-string label")
        seen.add(identifier)
        rows.append(
            {
                "value": identifier,
                "name": clean_hierarchy_name(row[1], identifier, level=level),
            }
        )
    return rows


def count_null_sentinel_rows(body: bytes) -> int:
    """Count exact two-null hierarchy sentinels after schema validation has passed."""
    payload = json.loads(body)
    return sum(row == [None, None] for row in payload["rows"])


def parse_state_page(body: bytes) -> list[dict[str, str]]:
    try:
        document = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("state page is not valid UTF-8") from error
    parser = StateOptionParser()
    parser.feed(document)
    if not parser.options:
        raise ValueError("state page does not contain #ddl_State options")

    states: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_value, raw_label in parser.options:
        value = raw_value.strip()
        if value in {"", "0", "-1"}:
            continue
        identifier = canonical_id(value, level="state")
        if identifier in seen:
            raise ValueError(f"state page contains duplicate ID {identifier}")
        seen.add(identifier)
        states.append({"value": identifier, "name": clean_name(raw_label)})
    if not states:
        raise ValueError("state page does not contain any real state options")
    return states


def validate_explicit_states(states: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for name, raw_value in states:
        clean_state = name.strip()
        if not clean_state:
            raise ValueError("explicit state name must not be empty")
        value = canonical_id(raw_value, level="state")
        if value in seen:
            raise ValueError(f"explicit states contain duplicate ID {value}")
        seen.add(value)
        result.append({"name": clean_state, "value": value})
    return result


def parse_state_argument(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("state must be LABEL=VALUE, for example Uttar Pradesh=9")
    name, identifier = value.rsplit("=", 1)
    try:
        canonical = canonical_id(identifier, level="state")
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if not name.strip():
        raise argparse.ArgumentTypeError("state label must not be empty")
    return name.strip(), canonical


def load_or_fetch(
    raw_path: Path,
    *,
    url: str,
    params: Mapping[str, str],
    validate: Callable[[bytes], object],
    timeout: float,
    retries: int,
    retry_backoff: float,
    limiter: RateLimiter,
    request_fn: RequestFn,
    sleeper: SleepFn,
) -> CachedResponse:
    provenance_path = raw_path.with_suffix(".provenance.json")
    expected_params = dict(sorted(params.items()))
    expected_url = request_url(url, params)

    if raw_path.exists() and provenance_path.exists():
        body = raw_path.read_bytes()
        provenance = json.loads(provenance_path.read_text())
        if provenance.get("request_url") != expected_url:
            raise ValueError(f"cached request URL does not match for {raw_path}")
        if provenance.get("params") != expected_params:
            raise ValueError(f"cached request parameters do not match for {raw_path}")
        if provenance.get("http_status") != 200:
            raise ValueError(f"cached HTTP status is not 200 for {raw_path}")
        if provenance.get("sha256") != sha256_bytes(body):
            raise ValueError(f"cached SHA-256 does not match for {raw_path}")
        validate(body)
        return CachedResponse(body=body, provenance=provenance)

    response = fetch_with_retries(
        expected_url,
        timeout=timeout,
        retries=retries,
        retry_backoff=retry_backoff,
        limiter=limiter,
        request_fn=request_fn,
        sleeper=sleeper,
        validate=validate,
    )
    provenance = {
        "request_url": expected_url,
        "response_url": response.url,
        "params": expected_params,
        "retrieved_utc": utc_now(),
        "http_status": response.status,
        "sha256": sha256_bytes(response.body),
        "bytes": len(response.body),
    }
    atomic_write_bytes(raw_path, response.body)
    atomic_write_json(provenance_path, provenance)
    return CachedResponse(body=response.body, provenance=provenance)


def collect_universe(
    out_dir: Path,
    *,
    years: Sequence[str],
    explicit_states: Sequence[tuple[str, str]] = (),
    hierarchy_exclusions: Mapping[HierarchyKey, Mapping[str, str]] | None = None,
    delay: float = 1.0,
    timeout: float = 120.0,
    retries: int = 3,
    retry_backoff: float = 1.0,
    request_fn: RequestFn = http_get,
    sleeper: SleepFn = time.sleep,
) -> tuple[pa.Table, dict[str, Any]]:
    unknown_years = sorted(set(years) - set(YEAR_CONFIGS))
    if unknown_years:
        raise ValueError(f"unknown PAI years: {', '.join(unknown_years)}")
    if len(years) != len(set(years)):
        raise ValueError("years must not contain duplicates")
    if not years:
        raise ValueError("at least one year is required")

    selected_states = validate_explicit_states(explicit_states)
    hierarchy_exclusions = hierarchy_exclusions or {}
    limiter = RateLimiter(delay, sleeper=sleeper)
    rows: list[dict[str, str]] = []
    counts_by_year: dict[str, dict[str, int]] = {}
    counts_by_state: dict[str, dict[str, int]] = {}
    states_without_published_score_control: dict[str, list[dict[str, str]]] = {}
    applied_exclusions: list[dict[str, str]] = []
    expected_exclusions: set[HierarchyKey] = set()

    for year in years:
        year_root = out_dir / "source" / year
        if selected_states:
            states = selected_states
        else:
            page_url = YEAR_CONFIGS[year]["url"]
            state_response = load_or_fetch(
                year_root / "states.html",
                url=page_url,
                params={},
                validate=parse_state_page,
                timeout=timeout,
                retries=retries,
                retry_backoff=retry_backoff,
                limiter=limiter,
                request_fn=request_fn,
                sleeper=sleeper,
            )
            states = parse_state_page(state_response.body)

        official_counts = OFFICIAL_FINAL_GP_COUNTS.get(year)
        if official_counts:
            expected_state_names = set(official_counts) - {"__india__"}
            selected_state_names = {state["name"] for state in states}
            unknown_names = selected_state_names - expected_state_names
            if unknown_names:
                states_without_published_score_control[year] = [
                    state for state in states if state["name"] in unknown_names
                ]
            if not selected_states and not expected_state_names.issubset(selected_state_names):
                missing = expected_state_names - selected_state_names
                raise ValueError(
                    f"{year}: portal state inventory does not match the official PAI control "
                    f"table; missing={sorted(missing)!r}"
                )

        year_districts = 0
        year_blocks = 0
        year_gps = 0
        hierarchy_null_sentinels = 0
        for state in states:
            expected_exclusions.update(
                key for key in hierarchy_exclusions if key[0] == year and key[1] == state["value"]
            )
            state_gp_start = len(rows)
            state_root = year_root / f"state={state['value']}"
            hierarchy_params = {
                "SID": state["value"],
                "YID": YEAR_CONFIGS[year]["expected_fy_value"],
            }
            district_response = load_or_fetch(
                state_root / "districts.json",
                url=DISTRICTS_URL,
                params=hierarchy_params,
                validate=lambda body: parse_hierarchy_json(
                    body, expected_id_column="d_id", level="district"
                ),
                timeout=timeout,
                retries=retries,
                retry_backoff=retry_backoff,
                limiter=limiter,
                request_fn=request_fn,
                sleeper=sleeper,
            )
            districts = parse_hierarchy_json(
                district_response.body, expected_id_column="d_id", level="district"
            )
            hierarchy_null_sentinels += count_null_sentinel_rows(district_response.body)
            year_districts += len(districts)

            for district in districts:
                exclusion_key = (year, state["value"], district["value"])
                if exclusion_key in hierarchy_exclusions:
                    check_exclusion_source(
                        hierarchy_exclusions[exclusion_key], district_response.provenance
                    )
                    applied_exclusions.append(
                        {
                            **dict(hierarchy_exclusions[exclusion_key]),
                            "district": district["name"],
                        }
                    )
                    continue
                district_root = state_root / f"district={district['value']}"
                block_params = hierarchy_params | {"ZID": district["value"]}
                block_response = load_or_fetch(
                    district_root / "blocks.json",
                    url=BLOCKS_URL,
                    params=block_params,
                    validate=lambda body: parse_hierarchy_json(
                        body, expected_id_column="b_id", level="block"
                    ),
                    timeout=timeout,
                    retries=retries,
                    retry_backoff=retry_backoff,
                    limiter=limiter,
                    request_fn=request_fn,
                    sleeper=sleeper,
                )
                blocks = parse_hierarchy_json(
                    block_response.body, expected_id_column="b_id", level="block"
                )
                hierarchy_null_sentinels += count_null_sentinel_rows(block_response.body)
                year_blocks += len(blocks)

                for block in blocks:
                    block_root = district_root / f"block={block['value']}"
                    gp_params = block_params | {"BID": block["value"]}
                    gp_response = load_or_fetch(
                        block_root / "gps.json",
                        url=GPS_URL,
                        params=gp_params,
                        validate=lambda body: parse_hierarchy_json(
                            body, expected_id_column="gp_code", level="GP"
                        ),
                        timeout=timeout,
                        retries=retries,
                        retry_backoff=retry_backoff,
                        limiter=limiter,
                        request_fn=request_fn,
                        sleeper=sleeper,
                    )
                    gps = parse_hierarchy_json(
                        gp_response.body, expected_id_column="gp_code", level="GP"
                    )
                    hierarchy_null_sentinels += count_null_sentinel_rows(gp_response.body)
                    year_gps += len(gps)
                    for gp in gps:
                        rows.append(
                            {
                                "year": year,
                                "state": state["name"],
                                "state_value": state["value"],
                                "district": district["name"],
                                "district_value": district["value"],
                                "block": block["name"],
                                "block_value": block["value"],
                                "gp_code": gp["value"],
                                "gp_name": gp["name"],
                                "source_url": gp_response.provenance["request_url"],
                                "retrieved_utc": gp_response.provenance["retrieved_utc"],
                                "source_sha256": gp_response.provenance["sha256"],
                            }
                        )

            state_gp_rows = len(rows) - state_gp_start
            counts_by_state[f"{year}:{state['value']}"] = {
                "hierarchy_gp_rows": state_gp_rows,
                "published_scored_gp_count": (
                    official_counts.get(state["name"], -1) if official_counts else -1
                ),
            }
            if official_counts and state["name"] in official_counts:
                counts_by_state[f"{year}:{state['value']}"]["hierarchy_minus_published"] = (
                    state_gp_rows - official_counts[state["name"]]
                )

        counts_by_year[year] = {
            "states": len(states),
            "districts": year_districts,
            "blocks": year_blocks,
            "gp_endpoint_rows": year_gps,
            "hierarchy_null_sentinels": hierarchy_null_sentinels,
        }
        if official_counts and not selected_states:
            counts_by_year[year]["published_scored_gp_count"] = official_counts["__india__"]
            counts_by_year[year]["hierarchy_minus_published"] = (
                year_gps - official_counts["__india__"]
            )

    observed_exclusions = {
        (row["year"], row["state_value"], row["district_value"]) for row in applied_exclusions
    }
    if observed_exclusions != expected_exclusions:
        raise ValueError(
            "reviewed hierarchy exclusions did not match the portal inventory: "
            f"missing={sorted(expected_exclusions - observed_exclusions)}, "
            f"unexpected={sorted(observed_exclusions - expected_exclusions)}"
        )

    keys = [(row["year"], row["gp_code"]) for row in rows]
    if len(keys) != len(set(keys)):
        duplicate_count = len(keys) - len(set(keys))
        raise ValueError(
            f"official hierarchy produced {duplicate_count} duplicate (year, gp_code) rows"
        )
    if sum(values["gp_endpoint_rows"] for values in counts_by_year.values()) != len(rows):
        raise AssertionError("GP endpoint row counts do not reconcile to collected rows")

    rows.sort(key=lambda row: (row["year"], row["state_value"], row["gp_code"]))
    table = pa.Table.from_pylist(rows, schema=UNIVERSE_SCHEMA)
    if table.num_rows != len(rows) or table.schema != UNIVERSE_SCHEMA:
        raise AssertionError("Parquet table construction violated the universe contract")
    for year, counts in counts_by_year.items():
        counts["parquet_rows"] = sum(row["year"] == year for row in rows)
        if counts["parquet_rows"] != counts["gp_endpoint_rows"]:
            raise AssertionError(f"{year}: endpoint and Parquet row counts do not match")

    parquet_path = out_dir / "gp_universe.parquet"
    atomic_write_parquet(parquet_path, table)
    roundtrip = pq.read_table(parquet_path)
    if roundtrip.schema != UNIVERSE_SCHEMA or roundtrip.num_rows != table.num_rows:
        raise AssertionError("gp_universe.parquet failed its schema/row-count round trip")
    roundtrip_keys = list(
        zip(
            roundtrip.column("year").to_pylist(),
            roundtrip.column("gp_code").to_pylist(),
            strict=True,
        )
    )
    if len(roundtrip_keys) != len(set(roundtrip_keys)):
        raise AssertionError("gp_universe.parquet contains duplicate (year, gp_code) keys")

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "created_utc": utc_now(),
        "years": list(years),
        "counts_by_year": counts_by_year,
        "counts_by_state": counts_by_state,
        "states_without_published_score_control": states_without_published_score_control,
        "hierarchy_exclusions_applied": applied_exclusions,
        "row_count": table.num_rows,
        "key": ["year", "gp_code"],
        "parquet": {
            "path": "gp_universe.parquet",
            "sha256": sha256_file(parquet_path),
            "schema": [
                {"name": field.name, "type": str(field.type), "nullable": field.nullable}
                for field in UNIVERSE_SCHEMA
            ],
        },
        "source_endpoints": {
            "districts": DISTRICTS_URL,
            "blocks": BLOCKS_URL,
            "gps": GPS_URL,
        },
        "official_final_gp_counts_source": OFFICIAL_FINAL_GP_COUNTS_SOURCE,
    }
    atomic_write_json(out_dir / "collection_manifest.json", manifest)
    return table, manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("runs/pai_universe"),
        help="collection directory (default: runs/pai_universe)",
    )
    parser.add_argument(
        "--years",
        nargs="+",
        choices=list(YEAR_CONFIGS),
        default=list(YEAR_CONFIGS),
        help="PAI years to collect (default: every configured year)",
    )
    parser.add_argument(
        "--state",
        action="append",
        type=parse_state_argument,
        default=[],
        metavar="LABEL=VALUE",
        help="limit collection to an explicit state; repeat as needed",
    )
    parser.add_argument(
        "--hierarchy-exclusions",
        type=Path,
        default=HIERARCHY_EXCLUSIONS,
        help="reviewed wrong-state district placements to exclude",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between HTTP requests")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="retries after the first attempt")
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=1.0,
        help="initial exponential retry backoff in seconds",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    table, manifest = collect_universe(
        args.out,
        years=args.years,
        explicit_states=args.state,
        hierarchy_exclusions=load_hierarchy_exclusions(args.hierarchy_exclusions),
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
    )
    print(f"Wrote {table.num_rows:,} rows to {args.out / 'gp_universe.parquet'}")
    for year, counts in manifest["counts_by_year"].items():
        print(
            f"{year}: {counts['states']} states, {counts['districts']} districts, "
            f"{counts['blocks']} blocks, {counts['parquet_rows']:,} GPs"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

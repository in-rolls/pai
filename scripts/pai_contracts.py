"""Typed Parquet writers and integrity contracts for derived PAI tables."""

import base64
import csv
import hashlib
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pyarrow as pa
import pyarrow.parquet as pq
from pai_common import (
    BLOCK_TABLE_FIELDS,
    BLOCK_TABLES,
    CANONICAL_THEME_SLUGS,
    EXPECTED_SCORE_ROWS_PER_GP,
    GP_METADATA_FIELDS,
    GP_SCORE_FIELDS,
    GP_UNIVERSE_FIELDS,
    OFFICIAL_FINAL_GP_COUNTS,
    OFFICIAL_FINAL_GP_COUNTS_SOURCE,
    OVERALL_SLUG,
    WIDE_THEME_FIELDS,
)

# PAI 1.0 pages omitted LGD codes on some rows; every later vintage must carry
# the code and scorecard link on every row.
LEGACY_VINTAGE = "2022-2023"

# A parsed row must always say where it came from; a blank here is a parser bug.
BLOCK_IDENTITY_FIELDS = (
    "year",
    "state",
    "state_value",
    "district",
    "district_value",
    "block",
    "block_value",
    "gp_name",
)
INTEGER_FIELDS = {
    "block_page": pa.int32(),
    "theme_order": pa.int8(),
    "html_pages": pa.int32(),
    "gp_rows": pa.int32(),
    "score_rows": pa.int32(),
}
SCORE_VALUE_EXCEPTIONS = Path(__file__).parent.parent / "config" / "score_value_exceptions.csv"
GP_SCORE_VECTOR_LINKS = Path(__file__).parent.parent / "config" / "gp_score_vector_links.csv"
THEME_HEADER_LINKS = Path(__file__).parent.parent / "config" / "theme_header_links.csv"
OFFICIAL_COUNT_EXCEPTIONS = (
    Path(__file__).parent.parent / "config" / "official_count_exceptions.csv"
)
ScoreValueKey = tuple[str, str, str]
LegacyIdentityBase = tuple[str, str, str, str, str]
ScoreSignature = tuple[str, ...]
SCORE_SIGNATURE_FIELDS = tuple(f"{slug}_score" for slug in CANONICAL_THEME_SLUGS)


def load_score_value_exceptions(
    path: Path = SCORE_VALUE_EXCEPTIONS,
) -> dict[ScoreValueKey, dict[str, str]]:
    """Load the reviewed set of source-blank scores that must remain null."""
    exceptions: dict[ScoreValueKey, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (row["year"].strip(), row["gp_code"].strip(), row["theme_slug"].strip())
            if (
                not all(key)
                or not row["evidence"].strip()
                or not row["source_path"].strip()
                or len(row.get("source_sha256", "").strip()) != 64
            ):
                raise ValueError(f"{path}: every score exception requires key/evidence/source_path")
            if key in exceptions:
                raise ValueError(f"{path}: duplicate score exception {key}")
            exceptions[key] = row
    return exceptions


def load_official_count_exceptions(
    path: Path = OFFICIAL_COUNT_EXCEPTIONS,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Reviewed cases where the portal displays fewer GPs than the Ministry's table."""
    exceptions: dict[tuple[str, str], dict[str, Any]] = {}
    if not path.exists():
        return exceptions
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if not row.get("evidence", "").strip():
                raise ValueError(f"{path}: every official-count exception requires evidence")
            official = int(row["official_count"])
            portal = int(row["portal_count"])
            if not 0 <= portal < official:
                raise ValueError(f"{path}: portal count must be below the official count")
            exceptions[(row["year"], row["state"])] = {
                "official_count": official,
                "portal_count": portal,
                "evidence": row["evidence"].strip(),
            }
    return exceptions


def expected_state_count(
    year: str, state: str, controls: Mapping[str, int], exceptions: Mapping[tuple[str, str], Any]
) -> tuple[int, dict[str, Any] | None]:
    """The count a collection must reproduce for a state, and the reviewed exception if any."""
    exception = exceptions.get((year, state))
    if exception is None:
        return controls[state], None
    if exception["official_count"] != controls[state]:
        raise AssertionError(f"{year} {state}: exception official count differs from controls")
    return exception["portal_count"], exception


@cache
def load_theme_header_links(path: Path = THEME_HEADER_LINKS) -> dict[str, dict[str, str]]:
    """Load the reviewed multilingual PAI theme-header dictionary."""
    links: dict[str, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            header = " ".join(row.get("theme_header", "").split())
            slug = row.get("theme_slug", "").strip()
            if (
                not header
                or slug not in CANONICAL_THEME_SLUGS
                or not row.get("language", "").strip()
                or not row.get("evidence", "").strip()
            ):
                raise ValueError(f"{path}: every theme link requires header, slug, and evidence")
            if header in links:
                raise ValueError(f"{path}: duplicate theme header {header!r}")
            links[header] = row
    observed_slugs = Counter(row["theme_slug"] for row in links.values())
    if set(observed_slugs) != set(CANONICAL_THEME_SLUGS):
        raise ValueError(f"{path}: theme dictionary does not cover all canonical slugs")
    return links


def canonical_theme_slug(header: Any) -> str:
    """Map one exact reviewed source header to its stable English slug."""
    cleaned = " ".join(str(header or "").split())
    link = load_theme_header_links().get(cleaned)
    if link is None:
        raise AssertionError(f"unreviewed PAI theme header: {cleaned!r}")
    return link["theme_slug"]


def apply_reviewed_theme_headers(scores: list[dict[str, Any]], wide: list[dict[str, Any]]) -> int:
    """Canonicalize multilingual theme slugs and rebuild wide theme columns."""
    changed = 0
    scores_by_key: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in scores:
        slug = canonical_theme_slug(row.get("theme_header"))
        changed += int(str(row.get("theme_slug") or "") != slug)
        row["theme_slug"] = slug
        scores_by_key[gp_key(row)].append(row)

    wide_by_key: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in wide:
        key = gp_key(row)
        if key in wide_by_key:
            raise AssertionError(f"duplicate wide GP while normalizing themes: {key}")
        wide_by_key[key] = row
    if set(scores_by_key) != set(wide_by_key):
        raise AssertionError("long and wide GP keys differ while normalizing themes")

    metadata_fields = set(GP_METADATA_FIELDS)
    for key, row in wide_by_key.items():
        for field in list(row):
            if field not in metadata_fields:
                del row[field]
        for score in scores_by_key[key]:
            slug = str(score["theme_slug"])
            row[f"{slug}_score"] = score.get("score", "")
            row[f"{slug}_grade"] = score.get("grade", "")
            row[f"{slug}_band"] = score.get("band", "")
            row[f"{slug}_raw"] = score.get("raw_value", "")
        missing = set(WIDE_THEME_FIELDS) - set(row)
        if missing:
            raise AssertionError(f"{key}: canonical wide row is missing {sorted(missing)}")
    return changed


def canonicalize_parsed_themes(parsed: dict[str, Any]) -> dict[str, Any]:
    """Apply the reviewed header dictionary immediately after browser parsing."""
    for result in parsed.get("rows", []):
        scores = result.get("scores", [])
        wide = result.get("wide", {})
        identity = {field: wide.get(field, "") for field in wide if field in GP_METADATA_FIELDS}
        for score in scores:
            score["theme_slug"] = canonical_theme_slug(score.get("theme_header"))
        rebuilt = dict(identity)
        for score in scores:
            slug = score["theme_slug"]
            rebuilt[f"{slug}_score"] = score.get("score", "")
            rebuilt[f"{slug}_grade"] = score.get("grade", "")
            rebuilt[f"{slug}_band"] = score.get("band", "")
            rebuilt[f"{slug}_raw"] = score.get("raw_value", "")
        result["wide"] = rebuilt
    return parsed


def _canonical_score(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"invalid score in identity signature: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"non-finite score in identity signature: {value!r}")
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def score_signature(row: dict[str, Any]) -> ScoreSignature:
    """Canonical overall-plus-nine-theme signature for a wide score row."""
    return tuple(_canonical_score(row.get(field)) for field in SCORE_SIGNATURE_FIELDS)


def legacy_identity_base(row: dict[str, Any]) -> LegacyIdentityBase:
    """Block-local identity for old PAI 2.0 rows whose portal omitted LGD codes."""
    return tuple(
        str(row.get(field) or "").strip()
        for field in ("year", "state_value", "district_value", "block_value", "gp_name")
    )  # type: ignore[return-value]


def load_gp_score_vector_links(
    path: Path = GP_SCORE_VECTOR_LINKS,
) -> dict[LegacyIdentityBase, dict[ScoreSignature, dict[str, str]]]:
    """Load reviewed score-vector links for same-name legacy PAI 2.0 rows."""
    links: dict[LegacyIdentityBase, dict[ScoreSignature, dict[str, str]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            base = legacy_identity_base({**row, "gp_name": row.get("score_gp_name", "")})
            signature = tuple(
                _canonical_score(value) for value in row.get("score_signature", "").split("|")
            )
            gp_code = row.get("gp_code", "").strip()
            scorecard_url = row.get("scorecard_url", "").strip()
            required_evidence = (
                row.get("evidence_url", "").strip(),
                row.get("retrieved_utc", "").strip(),
                row.get("source_sha256", "").strip(),
                row.get("evidence", "").strip(),
            )
            if not all(base) or len(signature) != len(SCORE_SIGNATURE_FIELDS):
                raise ValueError(f"{path}: invalid identity or ten-score signature")
            if not gp_code.isdecimal() or not scorecard_url or not all(required_evidence):
                raise ValueError(f"{path}: every link requires code, URL, and source evidence")
            if len(required_evidence[2]) != 64:
                raise ValueError(f"{path}: source_sha256 must contain 64 hex characters")
            if gp_key({"year": base[0], "scorecard_url": scorecard_url})[-1] != gp_code:
                raise ValueError(f"{path}: scorecard URL does not encode GP {gp_code}")
            by_signature = links.setdefault(base, {})
            if signature in by_signature:
                raise ValueError(f"{path}: duplicate score signature for {base}")
            if any(link["gp_code"] == gp_code for link in by_signature.values()):
                raise ValueError(f"{path}: duplicate GP code {gp_code} for {base}")
            by_signature[signature] = row
    return links


def _metadata_fingerprint(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get(field) or "")
        for field in GP_METADATA_FIELDS
        if field not in {"gp_code", "scorecard_url"}
    )


def apply_reviewed_score_vector_links(
    metadata: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    wide: list[dict[str, Any]],
    links: dict[LegacyIdentityBase, dict[ScoreSignature, dict[str, str]]] | None = None,
) -> int:
    """Restore LGD identities in reviewed same-name legacy blocks.

    The code assignment is determined by the complete score vector documented in
    the current official scorecard, never by display order. Metadata and long rows
    are then matched to that wide row on their non-identity metadata and theme score.
    """
    reviewed = links if links is not None else load_gp_score_vector_links()
    wide_groups: dict[LegacyIdentityBase, list[int]] = defaultdict(list)
    for index, row in enumerate(wide):
        if not str(row.get("gp_code") or "").strip():
            wide_groups[legacy_identity_base(row)].append(index)

    repaired = 0
    for base, by_signature in reviewed.items():
        wide_indices = wide_groups.get(base, [])
        if not wide_indices:
            continue
        if len(wide_indices) != len(by_signature):
            raise AssertionError(
                f"{base}: reviewed identity count differs from legacy wide rows: "
                f"{len(by_signature)} != {len(wide_indices)}"
            )

        originals = [(index, wide[index].copy()) for index in wide_indices]
        observed = [score_signature(row) for _, row in originals]
        if Counter(observed) != Counter(by_signature.keys()):
            raise AssertionError(f"{base}: legacy scores differ from reviewed identity vectors")

        meta_unused = [
            index
            for index, row in enumerate(metadata)
            if not str(row.get("gp_code") or "").strip() and legacy_identity_base(row) == base
        ]
        score_unused = [
            index
            for index, row in enumerate(scores)
            if not str(row.get("gp_code") or "").strip() and legacy_identity_base(row) == base
        ]
        if len(meta_unused) != len(originals):
            raise AssertionError(f"{base}: metadata count differs from reviewed wide rows")
        if len(score_unused) != len(originals) * EXPECTED_SCORE_ROWS_PER_GP:
            raise AssertionError(f"{base}: long-score count differs from reviewed wide rows")

        for wide_index, original in originals:
            link = by_signature[score_signature(original)]
            identity = {
                "gp_code": link["gp_code"].strip(),
                "scorecard_url": link["scorecard_url"].strip(),
            }
            wide[wide_index].update(identity)
            fingerprint = _metadata_fingerprint(original)

            matching_meta = [
                index
                for index in meta_unused
                if _metadata_fingerprint(metadata[index]) == fingerprint
            ]
            if not matching_meta:
                raise AssertionError(f"{base}: no metadata row matches reviewed score vector")
            meta_index = matching_meta[0]
            meta_unused.remove(meta_index)
            metadata[meta_index].update(identity)

            for field, expected_score in zip(
                SCORE_SIGNATURE_FIELDS, score_signature(original), strict=True
            ):
                slug = field.removesuffix("_score")
                matching_scores = [
                    index
                    for index in score_unused
                    if _metadata_fingerprint(scores[index]) == fingerprint
                    and str(scores[index].get("theme_slug") or "") == slug
                    and _canonical_score(scores[index].get("score")) == expected_score
                ]
                if not matching_scores:
                    raise AssertionError(
                        f"{base}: no long row matches reviewed {slug}={expected_score}"
                    )
                score_index = matching_scores[0]
                score_unused.remove(score_index)
                scores[score_index].update(identity)
            repaired += 1

        if meta_unused or score_unused:
            raise AssertionError(f"{base}: reviewed identity repair left unmatched rows")
    return repaired


def parquet_type(field: str, kind: str) -> pa.DataType:
    """Return the declared storage type for a derived column."""
    if field in INTEGER_FIELDS:
        return INTEGER_FIELDS[field]
    if field == "score" or (kind == "wide" and field.endswith("_score")):
        return pa.float64()
    return pa.string()


def typed_schema(fields: list[str], kind: str) -> pa.Schema:
    return pa.schema(
        [
            pa.field(field, parquet_type(field, kind), nullable=kind != "universe")
            for field in fields
        ]
    )


def rows_to_table(rows: list[dict[str, Any]], fields: list[str], kind: str) -> pa.Table:
    """Coerce dict rows onto the declared typed schema; a non-numeric score raises."""
    schema = typed_schema(fields, kind)
    normalized = []
    for row in rows:
        typed: dict[str, Any] = {}
        for field in fields:
            dtype = schema.field(field).type
            try:
                typed[field] = _coerce(row.get(field), dtype)
            except (TypeError, ValueError) as exc:
                raise AssertionError(f"{field} is not {dtype}: {row.get(field)!r}") from exc
        normalized.append(typed)
    return pa.Table.from_pylist(normalized, schema=schema)


def rows_to_typed_parquet(
    rows: list[dict[str, Any]], fields: list[str], dst: Path, kind: str
) -> pa.Table:
    """Write in-memory rows to typed Parquet, with an atomic read-back check."""
    schema = typed_schema(fields, kind)
    table = rows_to_table(rows, fields, kind)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        pq.write_table(table, tmp, compression="zstd", compression_level=7)
        restored = pq.read_table(tmp)
        if restored.schema != schema or restored.num_rows != len(rows):
            raise AssertionError(f"{dst}: Parquet round-trip contract failed")
        tmp.replace(dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    return table


def _coerce(value: Any, dtype: pa.DataType) -> Any:
    if value is None or (isinstance(value, str) and value == ""):
        return None if not pa.types.is_string(dtype) else ""
    if pa.types.is_integer(dtype):
        return int(value)
    if pa.types.is_floating(dtype):
        return float(value)
    return str(value)


def write_block_tables(
    block_dir: Path,
    metadata: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    wide: list[dict[str, Any]],
) -> dict[str, Path]:
    """Persist one block's three tables as typed Parquet and return their paths."""
    tables = {"metadata": metadata, "scores": scores, "wide": wide}
    written: dict[str, Path] = {}
    for kind, rows in tables.items():
        dst = block_dir / BLOCK_TABLES[kind]
        rows_to_typed_parquet(rows, list(BLOCK_TABLE_FIELDS[kind]), dst, kind)
        written[kind] = dst
    return written


def scorecard_gp_code(scorecard_url: str) -> str:
    """Decode the full LGD GP code embedded in a PAI scorecard URL."""
    encoded = parse_qs(urlparse(scorecard_url).query).get("gp_id", [""])[0]
    if encoded:
        try:
            padding = "=" * (-len(encoded) % 4)
            decoded = base64.b64decode(encoded + padding, validate=True).decode("ascii")
        except ValueError, UnicodeDecodeError:
            decoded = ""
        if decoded.isdecimal():
            return decoded
    return ""


def canonicalize_score_gp_codes(*tables: list[dict[str, Any]]) -> int:
    """Replace blank or truncated display codes with URL-encoded LGD codes."""
    changed = 0
    for rows in tables:
        for row in rows:
            decoded = scorecard_gp_code(str(row.get("scorecard_url") or ""))
            if decoded and str(row.get("gp_code") or "").strip() != decoded:
                row["gp_code"] = decoded
                changed += 1
    return changed


def gp_key(row: dict[str, Any]) -> tuple[str, ...]:
    """Stable GP-year key; LGD code is preferred, location/name explains legacy fallback."""
    year = str(row.get("year") or "")
    decoded = scorecard_gp_code(str(row.get("scorecard_url") or ""))
    if decoded:
        return (year, "scorecard_lgd", decoded)
    code = str(row.get("gp_code") or "").strip()
    if code:
        return (year, "lgd", code)
    return (
        year,
        "location_name",
        str(row.get("state_value") or row.get("state") or ""),
        str(row.get("district_value") or row.get("district") or ""),
        str(row.get("block_value") or row.get("block") or ""),
        str(row.get("gp_name") or ""),
    )


def score_value_key(row: dict[str, Any]) -> ScoreValueKey:
    return str(row.get("year") or ""), gp_key(row)[-1], str(row.get("theme_slug") or "")


def validate_block_rows(
    metadata: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    wide: list[dict[str, Any]],
    require_current_pai2_identity: bool = True,
    allowed_null_scores: set[ScoreValueKey] | None = None,
    observed_null_scores: set[ScoreValueKey] | None = None,
) -> dict[str, int]:
    """Fail before persisting a block that violates the GP/theme contract."""
    if len(metadata) != len(wide):
        raise AssertionError(f"metadata/wide row mismatch: {len(metadata)} != {len(wide)}")
    for row in metadata:
        missing = set(GP_METADATA_FIELDS) - set(row)
        if missing:
            raise AssertionError(f"metadata schema missing fields: {sorted(missing)}")
        blank = [field for field in BLOCK_IDENTITY_FIELDS if not str(row.get(field) or "").strip()]
        if blank:
            raise AssertionError(f"metadata row has blank identity fields: {blank}")
        if require_current_pai2_identity and row.get("year") != LEGACY_VINTAGE:
            if not str(row.get("gp_code") or "").strip():
                raise AssertionError("PAI 2.0 row is missing its GP LGD code")
            if not str(row.get("scorecard_url") or "").strip():
                raise AssertionError("PAI 2.0 row is missing its scorecard URL")
    for row in scores:
        missing = set(GP_SCORE_FIELDS) - set(row)
        if missing:
            raise AssertionError(f"score schema missing fields: {sorted(missing)}")
    for row in wide:
        missing = (set(GP_METADATA_FIELDS) | set(WIDE_THEME_FIELDS)) - set(row)
        if missing:
            raise AssertionError(f"wide schema missing fields: {sorted(missing)}")
    keys = [gp_key(row) for row in metadata]
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    if duplicates:
        raise AssertionError(f"duplicate GP-year keys: {duplicates[:3]}")
    wide_keys = [gp_key(row) for row in wide]
    if Counter(keys) != Counter(wide_keys):
        raise AssertionError("metadata and wide tables contain different GP-year keys")
    expected_scores = len(metadata) * EXPECTED_SCORE_ROWS_PER_GP
    if len(scores) != expected_scores:
        raise AssertionError(f"score row conservation failed: {len(scores)} != {expected_scores}")

    by_gp: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    wide_by_key = {gp_key(row): row for row in wide}
    for row in scores:
        by_gp[gp_key(row)].append(row)
    if set(by_gp) != set(keys):
        raise AssertionError("metadata and scores contain different GP-year keys")
    for key, rows in by_gp.items():
        slugs = [str(row.get("theme_slug") or "") for row in rows]
        orders = [int(row["theme_order"]) for row in rows]
        if len(rows) != EXPECTED_SCORE_ROWS_PER_GP or set(slugs) != set(CANONICAL_THEME_SLUGS):
            raise AssertionError(f"{key}: expected the 10 canonical themes, got {slugs}")
        if len(set(orders)) != EXPECTED_SCORE_ROWS_PER_GP:
            raise AssertionError(f"{key}: expected 10 unique theme orders, got {orders}")
        if slugs.count(OVERALL_SLUG) != 1:
            raise AssertionError(f"{key}: expected exactly one overall score")
        for row in rows:
            slug = str(row["theme_slug"])
            wide_score = wide_by_key[key].get(f"{slug}_score")
            if _canonical_score(wide_score) != _canonical_score(row.get("score")):
                raise AssertionError(f"{key}: long/wide score differs for {slug}")
            if row.get("score") in (None, ""):
                null_key = score_value_key(row)
                if null_key not in (allowed_null_scores or set()):
                    raise AssertionError(f"{key}: unreviewed null score {null_key}")
                if observed_null_scores is not None:
                    observed_null_scores.add(null_key)
                continue
            try:
                score = float(row["score"])
            except (KeyError, TypeError, ValueError) as exc:
                raise AssertionError(f"{key}: non-numeric score {row.get('score')!r}") from exc
            if not math.isfinite(score) or not 0 <= score <= 100:
                raise AssertionError(f"{key}: score outside [0, 100]: {score}")
    return {"gp_rows": len(metadata), "score_rows": len(scores), "wide_rows": len(wide)}


def validate_global_tables(
    metadata: Path,
    scores: Path,
    wide: Path,
    expected_state_gps: dict[tuple[str, str], int] | None = None,
    require_national: bool = False,
    require_current_pai2_identity: bool = True,
    score_value_exceptions: dict[ScoreValueKey, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Stream Parquet contracts, retaining only compact GP keys and counters."""
    files = (
        ("metadata", metadata, set(GP_METADATA_FIELDS)),
        ("scores", scores, set(GP_SCORE_FIELDS)),
        ("wide", wide, set(GP_METADATA_FIELDS) | set(WIDE_THEME_FIELDS)),
    )
    parquet = {name: pq.ParquetFile(path) for name, path, _ in files}
    for name, _, required in files:
        schema = parquet[name].schema_arrow
        missing = required - set(schema.names)
        if missing:
            raise AssertionError(f"{name} Parquet schema missing fields: {sorted(missing)}")
    if parquet["scores"].schema_arrow.field("theme_order").type != pa.int8():
        raise AssertionError("scores.theme_order must be int8")
    if parquet["scores"].schema_arrow.field("score").type != pa.float64():
        raise AssertionError("scores.score must be float64")
    for field in parquet["wide"].schema_arrow:
        if field.name.endswith("_score") and field.type != pa.float64():
            raise AssertionError(f"wide.{field.name} must be float64")

    meta_keys: set[tuple[str, ...]] = set()
    state_counts: Counter[tuple[str, str]] = Counter()
    for batch in parquet["metadata"].iter_batches(batch_size=65_536):
        for row in batch.to_pylist():
            key = gp_key(row)
            if key in meta_keys:
                raise AssertionError(f"duplicate GP-year key in metadata: {key}")
            meta_keys.add(key)
            state_counts[(str(row["year"]), str(row["state"]))] += 1
            if require_current_pai2_identity and row.get("year") != LEGACY_VINTAGE:
                if not str(row.get("gp_code") or "").strip():
                    raise AssertionError("PAI 2.0 row is missing its GP LGD code")
                if not str(row.get("scorecard_url") or "").strip():
                    raise AssertionError("PAI 2.0 row is missing its scorecard URL")

    wide_keys: set[tuple[str, ...]] = set()
    for batch in parquet["wide"].iter_batches(batch_size=65_536):
        for row in batch.to_pylist():
            key = gp_key(row)
            if key in wide_keys:
                raise AssertionError(f"duplicate GP-year key in wide: {key}")
            wide_keys.add(key)
    if meta_keys != wide_keys:
        raise AssertionError("metadata and wide tables contain different GP-year keys")

    score_keys: set[tuple[str, ...]] = set()
    current_key: tuple[str, ...] | None = None
    current_slugs: set[str] = set()
    current_orders: set[int] = set()
    current_rows = 0
    current_overall = 0

    def finish_score_gp() -> None:
        nonlocal current_key, current_slugs, current_orders, current_rows, current_overall
        if current_key is None:
            return
        if current_key in score_keys:
            raise AssertionError(f"non-contiguous duplicate GP-year score key: {current_key}")
        if current_rows != EXPECTED_SCORE_ROWS_PER_GP:
            raise AssertionError(
                f"{current_key}: expected {EXPECTED_SCORE_ROWS_PER_GP} scores, got {current_rows}"
            )
        if current_slugs != set(CANONICAL_THEME_SLUGS):
            raise AssertionError(f"{current_key}: score theme slugs are not canonical")
        if len(current_orders) != EXPECTED_SCORE_ROWS_PER_GP:
            raise AssertionError(f"{current_key}: score theme orders are not unique")
        if current_overall != 1:
            raise AssertionError(f"{current_key}: expected exactly one overall score")
        score_keys.add(current_key)
        current_key = None
        current_slugs = set()
        current_orders = set()
        current_rows = 0
        current_overall = 0

    score_rows = 0
    exception_rows = score_value_exceptions or load_score_value_exceptions()
    present_year_codes = {(key[0], key[-1]) for key in meta_keys}
    allowed_null_scores = {key for key in exception_rows if (key[0], key[1]) in present_year_codes}
    observed_null_scores: set[ScoreValueKey] = set()
    for batch in parquet["scores"].iter_batches(batch_size=65_536):
        for row in batch.to_pylist():
            key = gp_key(row)
            if current_key is not None and key != current_key:
                finish_score_gp()
            if current_key is None:
                current_key = key
            slug = str(row.get("theme_slug") or "")
            current_slugs.add(slug)
            current_orders.add(int(row["theme_order"]))
            current_rows += 1
            current_overall += int(slug == OVERALL_SLUG)
            score_rows += 1
            if row["score"] is None:
                null_key = score_value_key(row)
                if null_key not in allowed_null_scores:
                    raise AssertionError(f"{key}: unreviewed null score {null_key}")
                observed_null_scores.add(null_key)
                continue
            score = float(row["score"])
            if not math.isfinite(score) or not 0 <= score <= 100:
                raise AssertionError(f"{key}: score outside [0, 100]: {score}")
    finish_score_gp()
    if score_keys != meta_keys:
        raise AssertionError("metadata and scores contain different GP-year keys")
    expected_score_rows = len(meta_keys) * EXPECTED_SCORE_ROWS_PER_GP
    if score_rows != expected_score_rows:
        raise AssertionError(
            f"score row conservation failed: {score_rows} != {expected_score_rows}"
        )
    if observed_null_scores != allowed_null_scores:
        raise AssertionError(
            "reviewed null-score set mismatch: "
            f"observed_only={sorted(observed_null_scores - allowed_null_scores)}, "
            f"configured_only={sorted(allowed_null_scores - observed_null_scores)}"
        )

    counts = {
        "gp_rows": len(meta_keys),
        "score_rows": score_rows,
        "wide_rows": len(wide_keys),
        "reviewed_null_scores": len(observed_null_scores),
        "score_quality_flag": "source_blank_preserved_as_null"
        if observed_null_scores
        else "complete",
        "reviewed_null_score_details": [
            exception_rows[key] for key in sorted(observed_null_scores)
        ],
    }
    checked: dict[str, dict[str, int]] = {}
    for (year, state), expected in (expected_state_gps or {}).items():
        actual = state_counts[(year, state)]
        if actual != expected:
            raise AssertionError(
                f"official GP count failed for {state} {year}: {actual} != {expected}"
            )
        checked[f"{year}:{state}"] = {"actual": actual, "expected": expected}
    if require_national:
        present_years = {observed_year for observed_year, _ in state_counts}
        for year, controls in OFFICIAL_FINAL_GP_COUNTS.items():
            if year not in present_years:
                continue
            expected_states = {state for state in controls if not state.startswith("__")}
            actual_states = {
                state for observed_year, state in state_counts if observed_year == year
            }
            missing_states = sorted(expected_states - actual_states)
            if missing_states:
                raise AssertionError(
                    f"national state universe failed for {year}: missing={missing_states}"
                )
            # Rows the portal displays for a state the Ministry did not validate are
            # kept in the collection and counted here; the release package drops them.
            for state in sorted(actual_states - expected_states):
                checked[f"{year}:{state}"] = {
                    "actual": state_counts[(year, state)],
                    "expected": 0,
                    "status": "unvalidated_state_rows",
                }
            count_exceptions = load_official_count_exceptions()
            shortfall = 0
            for state in sorted(expected_states):
                actual = state_counts[(year, state)]
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
            national_actual = sum(
                count
                for (observed_year, state), count in state_counts.items()
                if observed_year == year and state in expected_states
            )
            if national_actual != controls["__india__"] - shortfall:
                raise AssertionError(
                    f"official India GP count failed for {year}: "
                    f"{national_actual} != {controls['__india__']} - {shortfall}"
                )
            checked[f"{year}:__india__"] = {
                "actual": national_actual,
                "expected": controls["__india__"],
                "reviewed_portal_shortfall": shortfall,
            }
    return {**counts, "official_counts_checked": checked}


def official_state_expectations(
    states: list[str], year: str = "2023-2024"
) -> dict[tuple[str, str], int]:
    controls = OFFICIAL_FINAL_GP_COUNTS.get(year, {})
    return {(year, state): controls[state] for state in states if state in controls}


def validate_universe_parquet(universe: Path, metadata: Path) -> dict[str, int]:
    """Validate the full handler denominator and score membership within it."""
    universe_file = pq.ParquetFile(universe)
    if universe_file.schema_arrow != typed_schema(GP_UNIVERSE_FIELDS, "universe"):
        raise AssertionError("gp_universe.parquet has an unexpected schema")
    universe_keys: set[tuple[str, str]] = set()
    for batch in universe_file.iter_batches(batch_size=65_536):
        for row in batch.to_pylist():
            key = (str(row["year"]), str(row["gp_code"]))
            if not all(str(row[field]).strip() for field in GP_UNIVERSE_FIELDS):
                raise AssertionError(f"GP-universe row has blank required values: {key}")
            if key in universe_keys:
                raise AssertionError(f"duplicate GP-universe year/code key: {key}")
            universe_keys.add(key)

    score_keys: set[tuple[str, str]] = set()
    metadata_file = pq.ParquetFile(metadata)
    for batch in metadata_file.iter_batches(columns=["year", "gp_code"], batch_size=65_536):
        for row in batch.to_pylist():
            key = (str(row["year"]), str(row["gp_code"]))
            if not key[1]:
                raise AssertionError("metadata cannot reconcile to universe with blank gp_code")
            score_keys.add(key)
    if not score_keys.issubset(universe_keys):
        unexpected_scores = sorted(score_keys - universe_keys)[:10]
        raise AssertionError(
            "score metadata contain GPs outside the hierarchy universe: "
            f"unexpected_scores={unexpected_scores}"
        )
    return {
        "universe_rows": len(universe_keys),
        "scored_universe_rows": len(score_keys),
        "unscored_universe_rows": len(universe_keys - score_keys),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_collection_manifest(
    dst: Path, files: list[Path], contract: dict[str, Any], source_dir: Path
) -> None:
    payload = {
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "source_collection": source_dir.name,
        "raw_cache": {
            "html": "rendered source pages under <year>/.../html/",
            "json": "resumable context/status plus official handler source/provenance JSON",
        },
        "derived": {
            path.name: {
                "rows": pq.read_metadata(path).num_rows,
                "sha256": sha256_file(path),
                "schema": str(pq.read_schema(path)),
            }
            for path in files
        },
        "contracts": contract,
        "official_control_source": OFFICIAL_FINAL_GP_COUNTS_SOURCE,
        "key_contract": {
            "preferred": ["year", "gp_code"],
            "fallback_when_gp_code_blank": [
                "year",
                "state_value_or_name",
                "district_value_or_name",
                "block_value_or_name",
                "gp_name",
            ],
            "cardinality": "one row per GP-year in metadata/wide; ten unique themes in long",
        },
        "universe_key_contract": {
            "key": ["year", "gp_code"],
            "cardinality": "one valid handler row per GP-year; every score belongs to universe",
        },
    }
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

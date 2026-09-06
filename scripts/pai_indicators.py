#!/usr/bin/env python3
"""Fetch the PAI indicator framework, per theme and version, from the PAI portal.

Output: docs/pai_indicators.csv, one row per (version, theme, indicator), under the
column contract in INDICATOR_SCHEMA and the checks in validate(). The portal's
indicator browser is versioned by `s` (1 = PAI 1.0, 2 = PAI 2.0) and theme by `t`.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import http.cookiejar
import re
import sys
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pai_common import CANONICAL_THEME_SLUGS  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "docs" / "pai_indicators.csv"
PORTAL = "https://pai.gov.in/MMS/Indicator/Theme-Indicators.aspx"
VERSIONS = {1: ("PAI 1.0", "2022-2023"), 2: ("PAI 2.0", "2023-2024")}
THEME_SLUGS = {int(slug[1]): slug for slug in CANONICAL_THEME_SLUGS if re.match(r"t\d_", slug)}
# The Ministry's "516 and 150 indicators" count theme-indicator rows; an indicator
# used in several themes is listed once per theme. Distinct ids are fewer.
EXPECTED_ROWS = {"PAI 1.0": 516, "PAI 2.0": 150}
EXPECTED_DISTINCT_IDS = {"PAI 1.0": 435, "PAI 2.0": 119}
# The portal renders the PAI 1.0 tables only once a session cookie exists, so a
# PAI 2.0 page is requested first through one cookie-carrying opener.
FETCH_ORDER = tuple((s, t) for s in (2, 1) for t in sorted(THEME_SLUGS))

INDICATOR_SCHEMA = pa.schema(
    [
        ("pai_version", pa.string()),
        ("fiscal_year", pa.string()),
        ("theme_number", pa.int8()),
        ("theme_slug", pa.string()),
        ("indicator_id", pa.int64()),
        ("mandatory", pa.string()),
        ("kind", pa.string()),
        ("indicator", pa.string()),
        ("numerator", pa.string()),
        ("denominator", pa.string()),
        ("source_url", pa.string()),
        ("retrieved_utc", pa.string()),
    ]
)
KEY = ("pai_version", "theme_number", "indicator_id")
KINDS = {"ratio", "binary"}
MANDATORY = {"Mandatory", "Optional"}
REQUIRED_TEXT = ("indicator", "numerator", "source_url", "retrieved_utc")

ID_SUFFIX = re.compile(r"\s*\[(\d+)\]\s*$")
ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
TAG = re.compile(r"<[^>]+>")


def page_url(session_id: int, theme: int) -> str:
    return f"{PORTAL}?t={theme}&s={session_id}"


def fetch(session_id: int, theme: int, opener: urllib.request.OpenerDirector) -> str:
    request = urllib.request.Request(
        page_url(session_id, theme), headers={"User-Agent": "Mozilla/5.0"}
    )
    with opener.open(request, timeout=60) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_pages(
    opener: urllib.request.OpenerDirector, fetch_one: Callable[..., str] = fetch
) -> dict[tuple[int, int], str]:
    """Fetch every (version, theme) page in FETCH_ORDER, sharing one session."""
    return {key: fetch_one(*key, opener) for key in FETCH_ORDER}


def _text(cell: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAG.sub("", cell))).strip()


def parse(page: str) -> list[tuple[str, str, str, str]]:
    """Return (mandatory, indicator, numerator, denominator) per five-cell table row."""
    rows = []
    for row in ROW.findall(page):
        cells = [_text(cell) for cell in CELL.findall(row)]
        if len(cells) == 5:
            rows.append((cells[1], cells[2], cells[3], cells[4]))
    return rows


def classify(indicator: str, numerator: str, denominator: str) -> str:
    """A ratio has a denominator distinct from its numerator; the rest are yes/no checks."""
    if indicator.lower().startswith("whether"):
        return "binary"
    if denominator and denominator.lower() != numerator.lower():
        return "ratio"
    return "binary"


def build(pages: dict[tuple[int, int], str], retrieved_utc: str) -> list[dict]:
    records = []
    for (session_id, theme), page in pages.items():
        version, fiscal_year = VERSIONS[session_id]
        seen: set[int] = set()
        for mandatory, indicator, numerator, denominator in parse(page):
            match = ID_SUFFIX.search(indicator)
            if match is None:
                raise ValueError(f"{version} T{theme}: indicator without an id: {indicator!r}")
            indicator_id = int(match.group(1))
            # The portal repeats an indicator under alias wordings; the id is the key.
            if indicator_id in seen:
                continue
            seen.add(indicator_id)
            name = ID_SUFFIX.sub("", indicator)
            numerator_text = ID_SUFFIX.sub("", numerator)
            denominator_text = ID_SUFFIX.sub("", denominator)
            records.append(
                {
                    "pai_version": version,
                    "fiscal_year": fiscal_year,
                    "theme_number": theme,
                    "theme_slug": THEME_SLUGS[theme],
                    "indicator_id": indicator_id,
                    "mandatory": mandatory,
                    "kind": classify(name, numerator_text, denominator_text),
                    "indicator": name,
                    "numerator": numerator_text,
                    "denominator": denominator_text,
                    "source_url": page_url(session_id, theme),
                    "retrieved_utc": retrieved_utc,
                }
            )
    records.sort(key=lambda r: tuple(r[k] for k in KEY))
    return records


def validate(table: pa.Table) -> pa.Table:
    """Raise unless the table meets the documented contract; return it unchanged."""
    if not table.schema.equals(INDICATOR_SCHEMA):
        raise ValueError(f"Schema differs from the contract:\n{table.schema}")
    rows = table.to_pylist()
    for row in rows:
        if any(row[c] is None for c in table.column_names):
            raise ValueError(f"Missing value in {row}")
        if any(row[c] == "" for c in REQUIRED_TEXT):
            raise ValueError(f"{REQUIRED_TEXT} must be filled: {row}")
        if row["kind"] not in KINDS:
            raise ValueError(f"kind outside {sorted(KINDS)}: {row}")
        if row["mandatory"] not in MANDATORY:
            raise ValueError(f"mandatory outside {sorted(MANDATORY)}: {row}")
        if row["kind"] == "ratio" and row["denominator"] == "":
            raise ValueError(f"A ratio indicator lacks a denominator: {row}")
        if row["theme_slug"] != THEME_SLUGS.get(row["theme_number"]):
            raise ValueError(f"theme_slug does not match theme_number: {row}")
        if row["fiscal_year"] != dict(VERSIONS.values())[row["pai_version"]]:
            raise ValueError(f"fiscal_year does not match pai_version: {row}")
    keys = [tuple(r[k] for k in KEY) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{KEY} is not unique")
    counts = _count(rows, lambda r: r["pai_version"])
    if counts != EXPECTED_ROWS:
        raise ValueError(f"Theme-indicator rows {counts} differ from {EXPECTED_ROWS}")
    distinct = {v: len({r["indicator_id"] for r in rows if r["pai_version"] == v}) for v in counts}
    if distinct != EXPECTED_DISTINCT_IDS:
        raise ValueError(f"Distinct ids {distinct} differ from {EXPECTED_DISTINCT_IDS}")
    themes = _count(rows, lambda r: (r["pai_version"], r["theme_number"]))
    if sorted(themes) != sorted((v, t) for v in counts for t in THEME_SLUGS):
        raise ValueError("Every version must cover the nine themes")
    return table


def _count(rows: Iterable[dict], key: Callable[[dict], object]) -> dict:
    counts: dict = {}
    for row in rows:
        counts[key(row)] = counts.get(key(row), 0) + 1
    return counts


def read(path: Path = OUTPUT) -> pa.Table:
    table = pacsv.read_csv(
        path,
        convert_options=pacsv.ConvertOptions(
            column_types=INDICATOR_SCHEMA, strings_can_be_null=False
        ),
    )
    return validate(table.select(INDICATOR_SCHEMA.names))


def write(records: list[dict], path: Path) -> pa.Table:
    table = validate(pa.Table.from_pylist(records, schema=INDICATOR_SCHEMA))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDICATOR_SCHEMA.names, lineterminator="\n")
        writer.writeheader()
        writer.writerows(records)
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()

    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    retrieved = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    records = build(fetch_pages(opener), retrieved)
    write(records, args.output)
    read(args.output)
    for version in EXPECTED_ROWS:
        subset = [r for r in records if r["pai_version"] == version]
        kinds = _count(subset, lambda r: r["kind"])
        print(f"{version}: {len(subset)} theme-indicator rows, {kinds}")
    print(f"Wrote {len(records)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

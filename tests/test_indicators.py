"""Contract tests for the committed PAI indicator framework and its fetcher."""

import pai_indicators as ind
import pyarrow as pa
import pytest


def test_committed_indicator_table_meets_its_contract():
    table = ind.read()
    rows = table.to_pylist()
    per_version = {v: sum(r["pai_version"] == v for r in rows) for v in ("PAI 1.0", "PAI 2.0")}
    assert per_version == {"PAI 1.0": 516, "PAI 2.0": 150}
    theme8 = [r for r in rows if r["theme_number"] == 8]
    kinds = {}
    for r in theme8:
        kinds[(r["pai_version"], r["kind"])] = kinds.get((r["pai_version"], r["kind"]), 0) + 1
    assert kinds == {
        ("PAI 1.0", "binary"): 37,
        ("PAI 1.0", "ratio"): 25,
        ("PAI 2.0", "binary"): 23,
        ("PAI 2.0", "ratio"): 3,
    }


def test_validate_rejects_duplicates_bad_kinds_and_lost_rows():
    table = ind.read()
    rows = table.to_pylist()
    with pytest.raises(ValueError, match="not unique"):
        ind.validate(pa.Table.from_pylist(rows + rows[:1], schema=ind.INDICATOR_SCHEMA))
    wrong = [dict(rows[0], kind="percentage"), *rows[1:]]
    with pytest.raises(ValueError, match="kind outside"):
        ind.validate(pa.Table.from_pylist(wrong, schema=ind.INDICATOR_SCHEMA))
    with pytest.raises(ValueError, match="Theme-indicator rows"):
        ind.validate(pa.Table.from_pylist(rows[1:], schema=ind.INDICATOR_SCHEMA))
    untyped = pa.Table.from_pylist(rows).cast(
        pa.schema([(f.name, pa.string()) for f in ind.INDICATOR_SCHEMA])
    )
    with pytest.raises(ValueError, match="Schema differs"):
        ind.validate(untyped)


def test_pai_2_pages_are_requested_before_pai_1_to_seed_the_session():
    calls = []

    def fake_fetch(session_id, theme, _opener):
        calls.append((session_id, theme))
        return ""

    pages = ind.fetch_pages(opener=object(), fetch_one=fake_fetch)
    assert len(pages) == 18
    assert calls[:9] == [(2, t) for t in range(1, 10)]
    assert calls[9:] == [(1, t) for t in range(1, 10)]


def test_parse_dedupes_aliases_and_classify_uses_the_denominator():
    page = (
        "<table><tr><td>1</td><td>Mandatory</td>"
        "<td>Percentage of Grievances redressed [469]</td>"
        "<td>Grievances redressed [900]</td><td>Grievances received [901]</td></tr>"
        "<tr><td>2</td><td>Mandatory</td>"
        "<td>Share of grievances redressed [469]</td>"
        "<td>Grievances redressed [900]</td><td>Grievances received [901]</td></tr>"
        "<tr><td>3</td><td>Optional</td>"
        "<td>Whether Gram Sabha has been conducted [717]</td>"
        "<td>Whether Gram Sabha has been conducted [717]</td><td></td></tr></table>"
    )
    records = ind.build({(2, 8): page}, "2026-01-01T00:00:00Z")
    assert [(r["indicator_id"], r["kind"], r["mandatory"]) for r in records] == [
        (469, "ratio", "Mandatory"),
        (717, "binary", "Optional"),
    ]
    assert records[0]["theme_slug"] == "t8_panchayat_with_good_governance"
    assert ind.classify("Number of works monitored", "works", "") == "binary"

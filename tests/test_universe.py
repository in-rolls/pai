"""Tests for the official LGD Gram Panchayat universe collector."""

import hashlib
import json
from pathlib import Path

import pai_collect_universe as universe
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def json_response(columns, rows):
    return json.dumps({"columns": columns, "rows": rows}).encode()


def test_parse_hierarchy_strips_matching_lgd_suffix():
    rows = universe.parse_hierarchy_json(
        json_response(["gp_code", "nm"], [[272000, "Adhiyarawa [272000]"]]),
        expected_id_column="gp_code",
        level="GP",
    )
    assert rows == [{"value": "272000", "name": "Adhiyarawa"}]


def test_parse_hierarchy_uses_final_lgd_suffix_when_source_name_contains_bracket():
    rows = universe.parse_hierarchy_json(
        json_response(["gp_code", "nm"], [[55537, "Mahmood[Urmilak [55537]"]]),
        expected_id_column="gp_code",
        level="GP",
    )
    assert rows == [{"value": "55537", "name": "Mahmood[Urmilak"}]


def test_parse_hierarchy_drops_only_an_exact_trailing_null_sentinel():
    body = json_response(
        ["d_id", "nm"],
        [[118, "Agra [118]"], [None, None]],
    )
    assert universe.parse_hierarchy_json(body, expected_id_column="d_id", level="district") == [
        {"value": "118", "name": "Agra"}
    ]
    assert universe.count_null_sentinel_rows(body) == 1

    with pytest.raises(ValueError, match="final response row"):
        universe.parse_hierarchy_json(
            json_response(
                ["d_id", "nm"],
                [[None, None], [118, "Agra [118]"]],
            ),
            expected_id_column="d_id",
            level="district",
        )
    with pytest.raises(ValueError, match="integer or decimal string"):
        universe.parse_hierarchy_json(
            json_response(["d_id", "nm"], [[None, "Select"]]),
            expected_id_column="d_id",
            level="district",
        )


def test_parse_hierarchy_rejects_disagreeing_lgd_suffix():
    with pytest.raises(ValueError, match="disagrees"):
        universe.parse_hierarchy_json(
            json_response(["gp_code", "nm"], [[272000, "Adhiyarawa [999999]"]]),
            expected_id_column="gp_code",
            level="GP",
        )


@pytest.mark.parametrize(
    "body, message",
    [
        (json_response(["wrong", "nm"], [[1, "A [1]"]]), "columns"),
        (json_response(["gp_code", "nm"], [[1, "A [1]"], [1, "A [1]"]]), "duplicate"),
        (json_response(["gp_code", "nm"], [[1, "A [1]", "extra"]]), "exactly two"),
        (json_response(["gp_code", "nm"], [[0, "A [0]"]]), "positive decimal"),
    ],
)
def test_parse_hierarchy_rejects_schema_and_key_failures(body, message):
    with pytest.raises(ValueError, match=message):
        universe.parse_hierarchy_json(
            body,
            expected_id_column="gp_code",
            level="GP",
        )


def test_parse_state_page():
    body = b"""
        <select id="ddl_State">
          <option value="0">-Select-</option>
          <option value="9">Uttar Pradesh [9, S-1]</option>
          <option value="8">Rajasthan [8, S-1]</option>
        </select>
    """
    assert universe.parse_state_page(body) == [
        {"value": "9", "name": "Uttar Pradesh"},
        {"value": "8", "name": "Rajasthan"},
    ]


class FakePortal:
    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> universe.HttpResponse:
        del timeout
        self.calls.append(url)
        parsed = universe.urllib.parse.urlparse(url)
        params = dict(universe.urllib.parse.parse_qsl(parsed.query))
        if parsed.path.endswith("TW-GP.aspx"):
            body = b'<select id="ddl_State"><option value="9">Uttar Pradesh [9]</option></select>'
        elif parsed.path.endswith("Y_Lgd_Districts.ashx"):
            body = json_response(["d_id", "nm"], [[118, "Agra [118]"]])
        elif parsed.path.endswith("Y_LGD_Blocks.ashx"):
            body = json_response(["b_id", "nm"], [[803, "Achhnera [803]"]])
        elif parsed.path.endswith("Y_GPs_By_LGD_Block.ashx"):
            year_id = params["YID"]
            gp_code = 1000 + int(year_id)
            body = json_response(["gp_code", "nm"], [[gp_code, f"Village {year_id} [{gp_code}]"]])
        else:
            raise AssertionError(f"unexpected request: {url}")
        return universe.HttpResponse(status=200, body=body, url=url)


def test_collect_universe_is_typed_resumable_and_conserves_rows(tmp_path: Path, monkeypatch):
    # The fake portal serves one state; the real control table lists 29.
    monkeypatch.setitem(
        universe.OFFICIAL_FINAL_GP_COUNTS, "2022-2023", {"Uttar Pradesh": 1, "__india__": 1}
    )
    portal = FakePortal()
    table, manifest = universe.collect_universe(
        tmp_path,
        years=["2022-2023"],
        delay=0,
        retry_backoff=0,
        request_fn=portal,
        sleeper=lambda _seconds: None,
    )

    assert table.schema == universe.UNIVERSE_SCHEMA
    assert table.num_rows == 1
    assert table.to_pylist()[0] == {
        "year": "2022-2023",
        "state": "Uttar Pradesh",
        "state_value": "9",
        "district": "Agra",
        "district_value": "118",
        "block": "Achhnera",
        "block_value": "803",
        "gp_code": "1001",
        "gp_name": "Village 1",
        "source_url": (
            "https://pai.gov.in/Handlers/Y_GPs_By_LGD_Block.ashx?BID=803&SID=9&YID=1&ZID=118"
        ),
        "retrieved_utc": table.to_pylist()[0]["retrieved_utc"],
        "source_sha256": table.to_pylist()[0]["source_sha256"],
    }
    assert manifest["counts_by_year"]["2022-2023"] == {
        "states": 1,
        "districts": 1,
        "blocks": 1,
        "gp_endpoint_rows": 1,
        "hierarchy_null_sentinels": 0,
        "published_scored_gp_count": 1,
        "hierarchy_minus_published": 0,
        "parquet_rows": 1,
    }

    parquet_path = tmp_path / "gp_universe.parquet"
    assert pq.read_schema(parquet_path) == universe.UNIVERSE_SCHEMA
    gp_raw = tmp_path / "source/2022-2023/state=9/district=118/block=803/gps.json"
    provenance = json.loads(gp_raw.with_suffix(".provenance.json").read_text())
    assert provenance["http_status"] == 200
    assert provenance["params"] == {"BID": "803", "SID": "9", "YID": "1", "ZID": "118"}
    assert provenance["sha256"] == hashlib.sha256(gp_raw.read_bytes()).hexdigest()
    assert provenance["retrieved_utc"].endswith("+00:00")

    first_call_count = len(portal.calls)
    cached_table, _ = universe.collect_universe(
        tmp_path,
        years=["2022-2023"],
        delay=0,
        retry_backoff=0,
        request_fn=portal,
        sleeper=lambda _seconds: None,
    )
    assert cached_table.equals(table)
    assert len(portal.calls) == first_call_count


def test_collect_rejects_gp_code_reused_across_blocks(tmp_path: Path):
    def duplicate_portal(url: str, timeout: float) -> universe.HttpResponse:
        del timeout
        path = universe.urllib.parse.urlparse(url).path
        if path.endswith("Y_Lgd_Districts.ashx"):
            body = json_response(["d_id", "nm"], [[118, "Agra [118]"]])
        elif path.endswith("Y_LGD_Blocks.ashx"):
            body = json_response(["b_id", "nm"], [[803, "A [803]"], [804, "B [804]"]])
        elif path.endswith("Y_GPs_By_LGD_Block.ashx"):
            body = json_response(["gp_code", "nm"], [[1001, "Village [1001]"]])
        else:
            raise AssertionError(url)
        return universe.HttpResponse(status=200, body=body, url=url)

    with pytest.raises(ValueError, match=r"duplicate \(year, gp_code\)"):
        universe.collect_universe(
            tmp_path,
            years=["2022-2023"],
            explicit_states=[("Uttar Pradesh", "9")],
            delay=0,
            retry_backoff=0,
            request_fn=duplicate_portal,
            sleeper=lambda _seconds: None,
        )


def test_pai2_hierarchy_and_published_score_counts_are_distinct(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(
        universe.OFFICIAL_FINAL_GP_COUNTS,
        "2023-2024",
        {"Uttar Pradesh": 2, "__india__": 2},
    )

    portal = FakePortal()
    table, manifest = universe.collect_universe(
        tmp_path,
        years=["2023-2024"],
        explicit_states=[("Uttar Pradesh", "9")],
        delay=0,
        retry_backoff=0,
        request_fn=portal,
        sleeper=lambda _seconds: None,
    )

    assert table.num_rows == 1
    assert manifest["counts_by_state"]["2023-2024:9"] == {
        "hierarchy_gp_rows": 1,
        "published_scored_gp_count": 2,
        "hierarchy_minus_published": -1,
    }


def test_repository_hierarchy_exclusions_are_evidenced_and_exact():
    exclusions = universe.load_hierarchy_exclusions(universe.HIERARCHY_EXCLUSIONS)
    assert set(exclusions) == {
        ("2022-2023", "22", "340"),
        ("2022-2023", "22", "426"),
        ("2022-2023", "22", "442"),
    }
    assert {row["correct_state_value"] for row in exclusions.values()} == {"20", "23", "24"}
    assert all(len(row["source_sha256"]) == 64 for row in exclusions.values())


def test_fetch_retries_transient_status_only_within_bound():
    statuses = iter([503, 429, 200])
    calls = 0

    def flaky(url: str, timeout: float) -> universe.HttpResponse:
        nonlocal calls
        del timeout
        calls += 1
        return universe.HttpResponse(status=next(statuses), body=b"ok", url=url)

    response = universe.fetch_with_retries(
        "https://example.test/data",
        timeout=1,
        retries=2,
        retry_backoff=0,
        limiter=universe.RateLimiter(0),
        request_fn=flaky,
        sleeper=lambda _seconds: None,
    )
    assert response.status == 200
    assert calls == 3


def test_fetch_retries_invalid_success_payload_within_bound():
    bodies = iter([b"not JSON", json_response(["gp_code", "nm"], [[1001, "A [1001]"]])])
    calls = 0

    def flaky(url: str, timeout: float) -> universe.HttpResponse:
        nonlocal calls
        del timeout
        calls += 1
        return universe.HttpResponse(status=200, body=next(bodies), url=url)

    response = universe.fetch_with_retries(
        "https://example.test/data",
        timeout=1,
        retries=1,
        retry_backoff=0,
        limiter=universe.RateLimiter(0),
        request_fn=flaky,
        sleeper=lambda _seconds: None,
        validate=lambda body: universe.parse_hierarchy_json(
            body, expected_id_column="gp_code", level="GP"
        ),
    )
    assert response.body.startswith(b'{"columns"')
    assert calls == 2


def test_parquet_schema_keeps_identifiers_as_strings():
    expected = {field.name: field.type for field in universe.UNIVERSE_SCHEMA}
    assert expected["state_value"] == pa.string()
    assert expected["district_value"] == pa.string()
    assert expected["block_value"] == pa.string()
    assert expected["gp_code"] == pa.string()
    assert all(not field.nullable for field in universe.UNIVERSE_SCHEMA)


def test_exclusion_applies_only_to_the_reviewed_response():
    exclusion = {"district_value": "340", "source_sha256": "a" * 64, "evidence": "reviewed"}
    universe.check_exclusion_source(exclusion, {"sha256": "a" * 64})
    with pytest.raises(ValueError, match="re-review before excluding"):
        universe.check_exclusion_source(exclusion, {"sha256": "b" * 64})

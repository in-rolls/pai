"""Data contracts fail loudly when a portal or parser change corrupts PAI rows."""

import hashlib
import json
from pathlib import Path

import pai_common as c
import pai_contracts
import pai_rebuild_index
import pai_scraper_resumable as scraper
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def make_rows(n=2):
    metadata = []
    scores = []
    wide = []
    for i in range(n):
        base = {
            **dict.fromkeys(c.GP_METADATA_FIELDS, ""),
            "year": "2023-2024",
            "state": "Uttar Pradesh",
            "state_value": "9",
            "district": "D",
            "district_value": "1",
            "block": "B",
            "block_value": "2",
            "gp_name": f"GP {i}",
            "gp_code": str(100 + i),
            "scorecard_url": f"https://pai.gov.in/score/{100 + i}",
        }
        metadata.append(base)
        wide_row = {**base}
        for theme, slug in enumerate(c.CANONICAL_THEME_SLUGS):
            score = str(50 + theme / 10)
            wide_row[f"{slug}_score"] = score
            wide_row[f"{slug}_grade"] = ""
            wide_row[f"{slug}_band"] = ""
            wide_row[f"{slug}_raw"] = score
            scores.append(
                {
                    **dict.fromkeys(c.GP_SCORE_FIELDS, ""),
                    **base,
                    "theme_order": str(theme),
                    "theme_header": next(
                        header
                        for header, link in pai_contracts.load_theme_header_links().items()
                        if link["theme_slug"] == slug and link["language"] == "en"
                    ),
                    "theme_slug": slug,
                    "score": score,
                }
            )
        wide.append(wide_row)
    return metadata, scores, wide


def test_current_pai2_route_is_unified_legacy_page():
    config = c.YEAR_CONFIGS["2023-2024"]
    assert config == {
        "url": "https://pai.gov.in/PS/Public/TW-GP.aspx",
        "expected_fy_value": "2",
        "result_table": "#GVdataT",
        "layout": "legacy",
    }


def test_pai1_keeps_its_explicit_year_route():
    assert c.YEAR_CONFIGS["2022-2023"]["url"].endswith("TW-GP.aspx?s=1")
    assert c.YEAR_CONFIGS["2022-2023"]["expected_fy_value"] == "1"


def test_official_pai2_controls_cover_33_states_and_reconcile():
    controls = c.OFFICIAL_FINAL_GP_COUNTS["2023-2024"]
    states = {state: count for state, count in controls.items() if not state.startswith("__")}
    assert len(states) == 33
    assert sum(states.values()) == controls["__india__"] == 259_867


def test_block_contract_accepts_ten_unique_themes():
    metadata, scores, wide = make_rows()
    assert pai_contracts.validate_block_rows(metadata, scores, wide) == {
        "gp_rows": 2,
        "score_rows": 20,
        "wide_rows": 2,
    }


def test_pai2_contract_requires_lgd_code_and_scorecard():
    metadata, scores, wide = make_rows()
    metadata[0]["gp_code"] = ""
    with pytest.raises(AssertionError, match="LGD code"):
        pai_contracts.validate_block_rows(metadata, scores, wide)


@pytest.mark.parametrize("failure", ["missing_theme", "duplicate_gp", "bad_score"])
def test_block_contract_rejects_corruption(failure):
    metadata, scores, wide = make_rows()
    if failure == "missing_theme":
        scores.pop()
    elif failure == "duplicate_gp":
        metadata.append(metadata[0].copy())
        wide.append(wide[0].copy())
        scores.extend(scores[:10])
    else:
        scores[0]["score"] = "101"
    with pytest.raises(AssertionError):
        pai_contracts.validate_block_rows(metadata, scores, wide)


def test_typed_parquet_preserves_ids_and_types(tmp_path):
    dst = tmp_path / "scores.parquet"
    pai_contracts.rows_to_typed_parquet(
        [{"gp_code": "007", "theme_order": "0", "score": "51.25"}],
        ["gp_code", "theme_order", "score"],
        dst,
        "scores",
    )
    table = pq.read_table(dst)
    assert table.schema == pa.schema(
        [
            pa.field("gp_code", pa.string()),
            pa.field("theme_order", pa.int8()),
            pa.field("score", pa.float64()),
        ]
    )
    assert table.to_pylist() == [{"gp_code": "007", "theme_order": 0, "score": 51.25}]


def test_typed_table_rejects_non_numeric_score():
    with pytest.raises(AssertionError, match="score is not double: 'abc'"):
        pai_contracts.rows_to_table(
            [{"gp_code": "1", "theme_order": "0", "score": "abc"}],
            ["gp_code", "theme_order", "score"],
            "scores",
        )


def test_block_tables_have_fixed_typed_schemas(tmp_path):
    metadata, scores, wide = make_rows(1)
    written = pai_contracts.write_block_tables(tmp_path, metadata, scores, wide)
    for kind, path in written.items():
        assert path.name == c.BLOCK_TABLES[kind]
        assert pq.read_schema(path) == pai_contracts.typed_schema(
            list(c.BLOCK_TABLE_FIELDS[kind]), kind
        )
    wide_schema = pq.read_schema(written["wide"])
    assert wide_schema.field(c.OVERALL_COL).type == pa.float64()
    assert wide_schema.field("gp_code").type == pa.string()


def test_expected_count_parser():
    assert pai_rebuild_index.parse_expected(
        ["Uttar Pradesh=57678", "2023-2024:Rajasthan=11037"]
    ) == {
        ("2023-2024", "Uttar Pradesh"): 57_678,
        ("2023-2024", "Rajasthan"): 11_037,
    }


def test_gp_universe_requires_exact_code_set_or_reviewed_missing():
    assert scraper.validate_gp_universe({"1", "2"}, {"1", "2"})["status"] == (
        "exact_universe_match"
    )
    with pytest.raises(AssertionError, match="does not match"):
        scraper.validate_gp_universe({"1"}, {"1", "2"})
    reviewed = {
        "allowed_missing_gp_codes": {"2"},
        "evidence": "Portal documents GP 2 as intentionally unscored.",
    }
    assert scraper.validate_gp_universe({"1"}, {"1", "2"}, reviewed)["status"] == (
        "reviewed_exception"
    )
    with pytest.raises(AssertionError, match="absent from"):
        scraper.validate_gp_universe({"1", "3"}, {"1", "2"}, reviewed)


def test_missing_gp_codes_link_by_block_name_not_row_order():
    metadata, scores, wide = make_rows()
    for rows in (metadata, scores, wide):
        for row in rows:
            row["gp_code"] = ""
    # Deliberately reverse the handler order: linkage must be name-based.
    universe = {"101": "GP 1", "100": "GP 0"}
    scraper.link_missing_gp_codes(metadata, scores, wide, universe)
    assert [row["gp_code"] for row in metadata] == ["100", "101"]
    assert {row["gp_code"] for row in scores if row["gp_name"] == "GP 0"} == {"100"}


def test_handler_name_link_replaces_truncated_legacy_display_code():
    metadata, scores, wide = make_rows(1)
    for rows in (metadata, scores, wide):
        for row in rows:
            row["gp_code"] = "1986"
            row["gp_name"] = "Adhiyarawa"
    scraper.link_missing_gp_codes(metadata, scores, wide, {"272000": "Adhiyarawa"})
    assert metadata[0]["gp_code"] == "272000"
    assert {row["gp_code"] for row in scores} == {"272000"}


def test_historical_key_uses_full_lgd_code_encoded_in_scorecard_url():
    row = {
        "year": "2022-2023",
        "gp_code": "1986",
        "scorecard_url": "/PS/Public/SC.aspx?gp_id=MTk4Njgx",
    }
    assert pai_contracts.gp_key(row) == ("2022-2023", "scorecard_lgd", "198681")


def test_scorecard_url_repairs_blank_and_truncated_codes_in_every_table():
    metadata, scores, wide = make_rows(1)
    for rows in (metadata, scores, wide):
        for row in rows:
            row["gp_code"] = "1986"
            row["scorecard_url"] = "/PS/Public/SC.aspx?gp_id=MTk4Njgx"
    assert pai_contracts.canonicalize_score_gp_codes(metadata, scores, wide) == 12
    assert {row["gp_code"] for rows in (metadata, scores, wide) for row in rows} == {"198681"}


def test_ambiguous_gp_name_requires_reviewed_link():
    metadata, scores, wide = make_rows(1)
    for rows in (metadata, scores, wide):
        for row in rows:
            row["gp_code"] = ""
    universe = {"100": "GP 0", "101": "GP   0"}
    with pytest.raises(AssertionError, match="reviewed name link"):
        scraper.link_missing_gp_codes(metadata, scores, wide, universe)
    scraper.link_missing_gp_codes(
        metadata,
        scores,
        wide,
        universe,
        {"gp 0": {"gp_code": "100", "evidence": "manual portal review"}},
    )
    assert metadata[0]["gp_code"] == "100"


def test_handler_name_suffix_is_removed_and_must_match_code():
    assert scraper.clean_universe_gp_name("272000", "Adhiyarawa [272000]") == "Adhiyarawa"
    assert scraper.clean_universe_gp_name("97946", "Paroo [N] [97946]") == "Paroo [N]"
    assert scraper.clean_universe_gp_name("97855", "Bariyarpur[East] [97855]") == "Bariyarpur[East]"
    with pytest.raises(AssertionError, match="disagrees"):
        scraper.clean_universe_gp_name("272000", "Adhiyarawa [999999]")


def test_only_exact_reviewed_source_blank_score_is_allowed():
    metadata, scores, wide = make_rows(1)
    for rows in (metadata, scores, wide):
        for row in rows:
            row["year"] = "2022-2023"
            row["scorecard_url"] = ""
    scores[2]["score"] = ""
    wide[0][f"{c.CANONICAL_THEME_SLUGS[2]}_score"] = ""
    null_key = ("2022-2023", "100", c.CANONICAL_THEME_SLUGS[2])
    with pytest.raises(AssertionError, match="unreviewed null"):
        pai_contracts.validate_block_rows(metadata, scores, wide)
    observed = set()
    pai_contracts.validate_block_rows(
        metadata,
        scores,
        wide,
        allowed_null_scores={null_key},
        observed_null_scores=observed,
    )
    assert observed == {null_key}


def test_repository_score_null_exceptions_are_evidenced_and_exact():
    exceptions = pai_contracts.load_score_value_exceptions()
    assert len(exceptions) == 38
    assert {key[2] for key in exceptions} == {"t2_healthy_panchayat"}
    assert all(row["source_path"].endswith("/html/page_001.html") for row in exceptions.values())


def test_reviewed_same_name_score_vectors_restore_lgd_identity_without_row_order():
    metadata, _, wide = make_rows(2)
    for rows in (metadata, wide):
        for row in rows:
            row["gp_name"] = "Same Name"
            row["gp_code"] = ""
            row["scorecard_url"] = ""
    for index, row in enumerate(wide):
        for offset, field in enumerate(pai_contracts.SCORE_SIGNATURE_FIELDS):
            row[field] = str(40 + index * 20 + offset)

    scores = []
    for row in wide:
        for order, field in enumerate(pai_contracts.SCORE_SIGNATURE_FIELDS):
            scores.append(
                {
                    **dict.fromkeys(c.GP_SCORE_FIELDS, ""),
                    **{key: row[key] for key in c.GP_METADATA_FIELDS},
                    "theme_order": str(order),
                    "theme_slug": field.removesuffix("_score"),
                    "score": row[field],
                }
            )
    base = pai_contracts.legacy_identity_base(wide[0])
    links = {
        base: {
            pai_contracts.score_signature(wide[1]): {
                "gp_code": "101",
                "scorecard_url": "/PS/Public/SC.aspx?gp_id=MTAx",
            },
            pai_contracts.score_signature(wide[0]): {
                "gp_code": "100",
                "scorecard_url": "/PS/Public/SC.aspx?gp_id=MTAw",
            },
        }
    }

    assert pai_contracts.apply_reviewed_score_vector_links(metadata, scores, wide, links) == 2
    assert [(row[c.OVERALL_COL], row["gp_code"]) for row in wide] == [
        ("40", "100"),
        ("60", "101"),
    ]
    assert {row["gp_code"] for row in metadata} == {"100", "101"}
    assert pai_contracts.validate_block_rows(metadata, scores, wide)["gp_rows"] == 2


def test_repository_reviewed_score_vector_links_are_evidenced_and_exact():
    links = pai_contracts.load_gp_score_vector_links()
    rows = [row for block in links.values() for row in block.values()]
    assert len(links) == 3
    assert len(rows) == 6
    assert {row["gp_code"] for row in rows} == {
        "132189",
        "132753",
        "233002",
        "233996",
        "286265",
        "286297",
    }
    assert all(len(row["source_sha256"]) == 64 for row in rows)


def test_reviewed_hindi_theme_header_repairs_long_and_wide_schema():
    _metadata, scores, wide = make_rows(1)
    scores[0]["theme_header"] = "समग्र पी. ए. आई. स्कोर"
    scores[0]["theme_slug"] = "field"
    wide[0]["field_score"] = wide[0].pop(c.OVERALL_COL)

    assert pai_contracts.apply_reviewed_theme_headers(scores, wide) == 1
    assert scores[0]["theme_slug"] == c.OVERALL_SLUG
    assert wide[0][c.OVERALL_COL] == "50.0"
    assert "field_score" not in wide[0]
    assert set(wide[0]) == set(c.GP_METADATA_FIELDS) | set(c.WIDE_THEME_FIELDS)


def test_repository_theme_dictionary_is_exact_and_evidenced():
    links = pai_contracts.load_theme_header_links()
    counts = {}
    for row in links.values():
        counts[row["language"]] = counts.get(row["language"], 0) + 1
    assert counts == {"en": 10, "hi": 10}
    assert {row["theme_slug"] for row in links.values()} == set(c.CANONICAL_THEME_SLUGS)
    assert all(row["evidence"] for row in links.values())


def _write_universe_source(root, *, corrupt_checksum=False):
    source = root / "2023-2024" / "UP__9" / "D__1" / "B__2" / "source"
    source.mkdir(parents=True)
    payload = {"columns": ["gp_code", "nm"], "rows": [["100", "GP 0 [100]"]]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    (source / "gp_universe.json").write_bytes(raw)
    c.write_json(
        source / "gp_universe_provenance.json",
        {
            "retrieved_utc": "2026-09-01T00:00:00+00:00",
            "year": "2023-2024",
            "state": "Uttar Pradesh",
            "state_value": "9",
            "district": "D",
            "district_value": "1",
            "block": "B",
            "block_value": "2",
            "url": scraper.GP_UNIVERSE_URL,
            "http_status": 200,
            "gp_rows": 1,
            "sha256": "bad" if corrupt_checksum else hashlib.sha256(raw).hexdigest(),
        },
    )


def test_cached_universe_provenance_is_verified_before_parquet(tmp_path):
    _write_universe_source(tmp_path)
    dst = tmp_path / "universe.parquet"
    assert pai_rebuild_index.write_universe_from_store(tmp_path, dst, ["2023-2024"]) == 1
    assert pq.read_table(dst).to_pylist()[0]["gp_name"] == "GP 0"

    broken = tmp_path / "broken"
    _write_universe_source(broken, corrupt_checksum=True)
    with pytest.raises(AssertionError, match="checksum"):
        pai_rebuild_index.write_universe_from_store(
            broken, broken / "universe.parquet", ["2023-2024"]
        )


def test_full_universe_can_contain_unscored_gps_but_not_vice_versa(tmp_path):
    universe_path = tmp_path / "universe.parquet"
    metadata_path = tmp_path / "metadata.parquet"
    universe_rows = []
    for code in ("100", "101"):
        universe_rows.append(
            {
                "year": "2023-2024",
                "state": "Uttar Pradesh",
                "state_value": "9",
                "district": "D",
                "district_value": "1",
                "block": "B",
                "block_value": "2",
                "gp_code": code,
                "gp_name": f"GP {code}",
                "source_url": "https://pai.gov.in/handler",
                "retrieved_utc": "2026-09-01T00:00:00+00:00",
                "source_sha256": "a" * 64,
            }
        )
    pq.write_table(
        pa.Table.from_pylist(
            universe_rows, schema=pai_contracts.typed_schema(c.GP_UNIVERSE_FIELDS, "universe")
        ),
        universe_path,
    )
    pq.write_table(pa.table({"year": ["2023-2024"], "gp_code": ["100"]}), metadata_path)
    assert pai_contracts.validate_universe_parquet(universe_path, metadata_path) == {
        "universe_rows": 2,
        "scored_universe_rows": 1,
        "unscored_universe_rows": 1,
    }

    pq.write_table(pa.table({"year": ["2023-2024"], "gp_code": ["999"]}), metadata_path)
    with pytest.raises(AssertionError, match="outside the hierarchy universe"):
        pai_contracts.validate_universe_parquet(universe_path, metadata_path)


def test_rebuild_promotes_only_complete_valid_bundle_and_manifest_is_portable(tmp_path):
    block = tmp_path / "2023-2024" / "UP__9" / "D__1" / "B__2"
    block.mkdir(parents=True)
    metadata, scores, wide = make_rows(1)
    pai_contracts.write_block_tables(block, metadata, scores, wide)
    c.write_json(
        block / c.DONE_JSON,
        {"status": "done", "gp_rows": 1, "score_rows": 10, "state": "Uttar Pradesh"},
    )
    _write_universe_source(tmp_path)

    out = tmp_path / "derived"
    pai_rebuild_index.build(
        tmp_path,
        out,
        {("2023-2024", "Uttar Pradesh"): 1},
        years=["2023-2024"],
    )
    manifest_path = out / "collection_manifest.json"
    before = manifest_path.read_bytes()
    manifest = json.loads(before)
    assert manifest["source_collection"] == tmp_path.name
    assert str(tmp_path.resolve()) not in before.decode()
    assert "gp_universe.parquet" in manifest["derived"]

    with pytest.raises(AssertionError, match="official GP count"):
        pai_rebuild_index.build(
            tmp_path,
            out,
            {("2023-2024", "Uttar Pradesh"): 2},
            years=["2023-2024"],
        )
    assert manifest_path.read_bytes() == before


def test_officially_unscored_state_allows_whole_block_no_data():
    universe = {"1": "A", "2": "B"}
    exc = scraper.officially_unscored_state_exception("2023-2024", "West Bengal", universe)
    assert exc["allowed_missing_gp_codes"] == {"1", "2"}
    assert "PressRelease" in exc["evidence"]
    assert scraper.validate_gp_universe(set(), set(universe), exc)["status"]
    assert scraper.officially_unscored_state_exception("2023-2024", "Bihar", universe) is None
    assert scraper.officially_unscored_state_exception("2021-2022", "West Bengal", universe) is None
    for state in ("West Bengal", "Meghalaya", "Nagaland", "Goa", "Puducherry"):
        assert scraper.officially_unscored_state_exception("2022-2023", state, universe)


def test_same_name_gps_keep_their_own_codes():
    universe = {"132753": "Pondi", "132189": "Pondi", "1": "Amba"}
    meta = [
        {"gp_name": "Pondi", "gp_code": "132753"},
        {"gp_name": "Pondi", "gp_code": "132189"},
        {"gp_name": "Amba", "gp_code": ""},
    ]
    scores = [dict(m) for m in meta]
    wide = [dict(m) for m in meta]
    scraper.link_missing_gp_codes(meta, scores, wide, universe)
    assert [m["gp_code"] for m in meta] == ["132753", "132189", "1"]
    assert [m["gp_code"] for m in scores] == ["132753", "132189", "1"]
    with pytest.raises(AssertionError, match="2 exact universe matches"):
        scraper.link_missing_gp_codes([{"gp_name": "Pondi", "gp_code": ""}], [], [], universe)


def test_partially_scored_state_accepts_subset_but_never_extra_codes(tmp_path):
    manifest = tmp_path / "collection_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "counts_by_state": {
                    "2023-2024:17": {"hierarchy_gp_rows": 6859, "published_scored_gp_count": 3069},
                    "2023-2024:22": {
                        "hierarchy_gp_rows": 11643,
                        "published_scored_gp_count": 11643,
                    },
                    "2022-2023:17": {"hierarchy_gp_rows": 6859, "published_scored_gp_count": -1},
                }
            }
        )
    )
    assert scraper.load_partially_scored_states(manifest) == {("2023-2024", "17")}
    assert scraper.load_partially_scored_states(None) == set()

    check = scraper.validate_gp_universe({"1"}, {"1", "2"}, allow_subset=True)
    assert check["status"] == "subset_in_partially_scored_state"
    assert check["missing"] == {"2"}
    with pytest.raises(AssertionError, match="empty score result"):
        scraper.validate_gp_universe(set(), {"1", "2"}, allow_subset=True)
    assert scraper.validate_gp_universe({"1", "2"}, {"1", "2"}, allow_subset=True)["status"] == (
        "exact_universe_match"
    )
    with pytest.raises(AssertionError, match="absent from"):
        scraper.validate_gp_universe({"1", "9"}, {"1", "2"}, allow_subset=True)


def test_zero_score_is_a_score_not_a_blank():
    assert pai_contracts._canonical_score(0.0) == "0"
    assert pai_contracts._canonical_score(0) == "0"
    assert pai_contracts._canonical_score("0.00") == "0"
    assert pai_contracts._canonical_score(None) == ""
    assert pai_contracts._canonical_score("") == ""


def test_block_contract_rejects_blank_identity_fields():
    metadata, scores, wide = make_rows(1)
    for rows in (metadata, scores, wide):
        for row in rows:
            row["state"] = ""
    with pytest.raises(AssertionError, match="blank identity"):
        pai_contracts.validate_block_rows(metadata, scores, wide)


def test_universe_from_store_refuses_a_finished_block_without_its_cache(tmp_path):
    block = tmp_path / "2023-2024" / "UP__9" / "D__1" / "B__2"
    block.mkdir(parents=True)
    c.write_json(block / c.DONE_JSON, {"status": "done_no_data_available", "gp_rows": 0})
    with pytest.raises(AssertionError, match="lack their universe cache"):
        pai_rebuild_index.write_universe_from_store(
            tmp_path, tmp_path / "universe.parquet", ["2023-2024"]
        )


def test_current_identity_contract_covers_every_vintage_after_pai_1():
    metadata, scores, wide = make_rows(1)
    for rows in (metadata, scores, wide):
        for row in rows:
            row["year"] = "2024-2025"
            row["gp_code"] = ""
            row["scorecard_url"] = ""
    with pytest.raises(AssertionError, match="LGD code"):
        pai_contracts.validate_block_rows(metadata, scores, wide)


def test_national_controls_require_the_independent_universe(tmp_path):
    with pytest.raises(AssertionError, match="requires --universe-data-dir"):
        pai_rebuild_index.build(tmp_path, tmp_path / "derived", {}, require_national=True)


def test_store_universe_rows_carry_the_exact_request_url(tmp_path):
    block = tmp_path / "2023-2024" / "UP__9" / "D__1" / "B__2"
    block.mkdir(parents=True)
    c.write_json(block / c.DONE_JSON, {"status": "done", "gp_rows": 1})
    _write_universe_source(tmp_path)
    provenance = block / "source" / "gp_universe_provenance.json"
    prov = json.loads(provenance.read_text())
    prov["params"] = {"SID": "9", "ZID": "1", "BID": "2", "YID": "2"}
    provenance.write_text(json.dumps(prov))
    out = tmp_path / "universe.parquet"
    pai_rebuild_index.write_universe_from_store(tmp_path, out, ["2023-2024"])
    url = pq.read_table(out).column("source_url")[0].as_py()
    assert url == f"{scraper.GP_UNIVERSE_URL}?BID=2&SID=9&YID=2&ZID=1"


def test_official_controls_are_internally_consistent():
    for year, controls in c.OFFICIAL_FINAL_GP_COUNTS.items():
        states = {k: v for k, v in controls.items() if not k.startswith("__")}
        assert sum(states.values()) == controls["__india__"], year
        assert year in c.OFFICIAL_FINAL_GP_COUNTS_SOURCE
    assert c.OFFICIAL_FINAL_GP_COUNTS["2022-2023"]["__india__"] == 216_285
    assert len(c.OFFICIAL_FINAL_GP_COUNTS["2022-2023"]) - 1 == 29
    assert c.OFFICIAL_FINAL_GP_COUNTS["2023-2024"]["__india__"] == 259_867


def test_officially_unvalidated_state_is_only_one_absent_from_a_controlled_vintage():
    assert scraper.officially_unvalidated_state("2022-2023", "Meghalaya")
    assert not scraper.officially_unvalidated_state("2022-2023", "Uttar Pradesh")
    assert not scraper.officially_unvalidated_state("2023-2024", "Meghalaya")
    assert not scraper.officially_unvalidated_state("2021-2022", "Meghalaya")


def test_reviewed_official_count_exception_requires_the_portal_count(tmp_path):
    ledger = tmp_path / "official_count_exceptions.csv"
    ledger.write_text(
        "year,state,official_count,portal_count,evidence\n"
        "2022-2023,Assam,2183,2154,portal displays 2154 in two independent double retrievals\n"
    )
    exceptions = pai_contracts.load_official_count_exceptions(ledger)
    controls = {"Assam": 2183, "__india__": 2183}
    assert pai_contracts.expected_state_count("2022-2023", "Assam", controls, exceptions)[0] == 2154
    assert pai_contracts.expected_state_count("2022-2023", "Assam", controls, {})[0] == 2183
    ledger.write_text(
        "year,state,official_count,portal_count,evidence\n2022-2023,Assam,2183,2154,\n"
    )
    with pytest.raises(ValueError, match="requires evidence"):
        pai_contracts.load_official_count_exceptions(ledger)


def test_repository_official_count_exceptions_are_consistent_with_controls():
    for (year, state), row in pai_contracts.load_official_count_exceptions().items():
        assert c.OFFICIAL_FINAL_GP_COUNTS[year][state] == row["official_count"], (year, state)


def test_missing_hierarchy_manifest_means_strict_contract(tmp_path):
    assert scraper.load_partially_scored_states(tmp_path / "absent.json") == set()


def test_partial_state_subset_may_not_be_empty():
    with pytest.raises(AssertionError, match="empty score result"):
        scraper.validate_gp_universe(set(), {"1", "2"}, allow_subset=True)
    assert scraper.validate_gp_universe({"1"}, {"1", "2"}, allow_subset=True)["status"] == (
        "subset_in_partially_scored_state"
    )


def test_alert_confirmed_no_data_is_a_valid_subset_but_a_blank_render_is_not():
    check = scraper.validate_gp_universe(
        set(), {"1", "2"}, allow_subset=True, no_data_confirmed=True
    )
    assert check["status"] == "subset_in_partially_scored_state"
    with pytest.raises(AssertionError, match="empty score result"):
        scraper.validate_gp_universe(set(), {"1", "2"}, allow_subset=True)
    assert scraper.load_partially_scored_states(Path(".")) == set()


def test_empty_hierarchy_manifest_file_means_strict_contract(tmp_path):
    empty = tmp_path / "collection_manifest.json"
    empty.write_bytes(b"")
    assert scraper.load_partially_scored_states(empty) == set()


def test_rebuild_regenerates_wide_scores_from_the_long_table(tmp_path):
    """An interrupted block write can leave a stale wide table beside new long scores;
    the rebuild must publish the long scores, never the stale wide ones."""
    block = tmp_path / "2023-2024" / "UP__9" / "D__1" / "B__2"
    block.mkdir(parents=True)
    metadata, scores, wide = make_rows(1)
    wide[0][c.OVERALL_COL] = "99"  # long table still says 50
    pai_contracts.write_block_tables(block, metadata, scores, wide)
    c.write_json(
        block / c.DONE_JSON,
        {"status": "done", "gp_rows": 1, "score_rows": 10, "state": "Uttar Pradesh"},
    )
    _write_universe_source(tmp_path)
    out = tmp_path / "derived"
    pai_rebuild_index.build(
        out.parent, out, {("2023-2024", "Uttar Pradesh"): 1}, years=["2023-2024"]
    )
    wide_row = pq.read_table(out / "gp_scores_wide.parquet").to_pylist()[0]
    long_rows = pq.read_table(out / "gp_scores_long.parquet").to_pylist()
    overall = next(r["score"] for r in long_rows if r["theme_slug"] == c.OVERALL_SLUG)
    assert wide_row[c.OVERALL_COL] == overall == 50.0


def _universe_dir(root, rows):
    universe = root / "universe"
    universe.mkdir()
    table = pa.Table.from_pylist(
        rows, schema=pai_contracts.typed_schema(c.GP_UNIVERSE_FIELDS, "universe")
    )
    pq.write_table(table, universe / "gp_universe.parquet")
    counts = {}
    for row in rows:
        counts.setdefault(row["year"], {"parquet_rows": 0})["parquet_rows"] += 1
    manifest = {
        "parquet": {
            "sha256": hashlib.sha256((universe / "gp_universe.parquet").read_bytes()).hexdigest()
        },
        "row_count": len(rows),
        "counts_by_year": counts,
    }
    (universe / "collection_manifest.json").write_text(json.dumps(manifest))
    return universe


def test_standalone_universe_must_match_its_collection_manifest(tmp_path):
    row = {
        "year": "2023-2024",
        "state": "Uttar Pradesh",
        "state_value": "9",
        "district": "D",
        "district_value": "1",
        "block": "B",
        "block_value": "2",
        "gp_code": "100",
        "gp_name": "GP 0",
        "source_url": "u",
        "retrieved_utc": "t",
        "source_sha256": "a" * 64,
    }
    universe = _universe_dir(tmp_path, [row])
    assert (
        pai_rebuild_index.verify_universe_source(universe, ["2023-2024"])["universe_source_rows"]
        == 1
    )
    with pytest.raises(AssertionError, match="no universe collection for 2022-2023"):
        pai_rebuild_index.verify_universe_source(universe, ["2022-2023"])
    # A shortened Parquet (one unscored GP lost) no longer matches the manifest.
    pq.write_table(
        pq.read_table(universe / "gp_universe.parquet").slice(0, 0),
        universe / "gp_universe.parquet",
    )
    with pytest.raises(AssertionError, match="checksum differs"):
        pai_rebuild_index.verify_universe_source(universe, ["2023-2024"])


def test_universe_rows_skip_the_null_sentinel_but_reject_other_nulls():
    payload = {"columns": ["gp_code", "nm"], "rows": [[1, "A [1]"], [None, None]]}
    assert scraper.universe_rows(payload) == [("1", "A [1]")]
    with pytest.raises(RuntimeError, match="malformed row"):
        scraper.universe_rows({"rows": [[1, None]]})


def test_universe_payload_with_sentinel_is_not_a_duplicate():
    payload = {"columns": ["gp_code", "nm"], "rows": [[1, "A [1]"], [None, None]]}
    assert scraper.universe_from_payload(payload) == {"1": "A"}
    with pytest.raises(RuntimeError, match="duplicate LGD codes"):
        scraper.universe_from_payload(
            {"columns": ["gp_code", "nm"], "rows": [[1, "A [1]"], [1, "A [1]"]]}
        )


def test_truncated_display_code_is_decoded_from_the_scorecard_url_before_linking():
    url = "/PS/Public/SC.aspx?f_id=Mg==&s_id=OQ==&d_id=MQ==&b_id=Mg==&Blank=1&gp_id=MjcyMDAw"
    universe = {"272000": "Amba", "272001": "Amba"}  # ambiguous by name
    meta = [{"gp_name": "Amba", "gp_code": "27", "scorecard_url": url}]
    rows = [dict(meta[0])]
    with pytest.raises(AssertionError, match="2 exact universe matches"):
        scraper.link_missing_gp_codes([dict(meta[0])], [], [], universe)
    pai_contracts.canonicalize_score_gp_codes(meta, rows, rows)
    scraper.link_missing_gp_codes(meta, rows, rows, universe)
    assert meta[0]["gp_code"] == "272000"


def test_a_universe_code_is_trusted_even_when_the_display_name_is_misspelled():
    universe = {"272000": "Amba"}
    meta = [{"gp_name": "Ambaa", "gp_code": "272000"}]
    scraper.link_missing_gp_codes(meta, [], [], universe)
    assert meta[0]["gp_code"] == "272000"

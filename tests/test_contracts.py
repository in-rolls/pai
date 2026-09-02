"""Data contracts fail loudly when a portal or parser change corrupts PAI rows."""

import hashlib
import json

import pai_common as c
import pai_contracts
import pai_rebuild_index
import pai_scraper_resumable as scraper
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from build_release import requires_national_controls


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


def test_pai2_release_requires_national_controls_by_default():
    assert requires_national_controls("2023-2024", allow_partial=False)
    assert not requires_national_controls("2023-2024", allow_partial=True)
    assert not requires_national_controls("2022-2023", allow_partial=False)


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
    src = tmp_path / "scores.csv"
    c.write_csv_rows(
        src,
        [{"gp_code": "007", "theme_order": "0", "score": "51.25"}],
        ["gp_code", "theme_order", "score"],
    )
    dst = tmp_path / "scores.parquet"
    pai_contracts.csv_to_typed_parquet(src, dst, "scores")
    table = pq.read_table(dst)
    assert table.schema == pa.schema(
        [
            pa.field("gp_code", pa.string()),
            pa.field("theme_order", pa.int8()),
            pa.field("score", pa.float64()),
        ]
    )
    assert table.to_pylist() == [{"gp_code": "007", "theme_order": 0, "score": 51.25}]


def test_csv_to_parquet_streams_multiple_batches(tmp_path):
    src = tmp_path / "scores.csv"
    rows = [
        {"gp_code": f"{i:06d}", "theme_order": str(i % 10), "score": "51.25"} for i in range(500)
    ]
    c.write_csv_rows(src, rows, ["gp_code", "theme_order", "score"])
    dst = tmp_path / "scores.parquet"
    pai_contracts.csv_to_typed_parquet(src, dst, "scores", block_size=256)
    parquet = pq.ParquetFile(dst)
    assert parquet.metadata.num_rows == 500
    assert parquet.metadata.num_row_groups > 1


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
    assert len(exceptions) == 22
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


def test_rebuild_promotes_only_complete_valid_bundle_and_manifest_is_portable(tmp_path):
    block = tmp_path / "2023-2024" / "UP__9" / "D__1" / "B__2"
    block.mkdir(parents=True)
    metadata, scores, wide = make_rows(1)
    c.write_csv_rows(block / c.METADATA_CSV, metadata, c.GP_METADATA_FIELDS)
    c.write_csv_rows(block / c.SCORES_LONG_CSV, scores, c.GP_SCORE_FIELDS)
    c.write_csv_rows(block / c.DATA_WIDE_CSV, wide)
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

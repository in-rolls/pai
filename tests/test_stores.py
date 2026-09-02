"""The block store must give the same answers from an archive as from a live tree."""

import json

import pai_common as c
import pai_compact
import pai_contracts
import pai_stores
from pai_stores import BlockStore


def _tree(root):
    """A miniature data dir: two blocks under one year, plus an html capture."""
    year = root / "2022-2023"
    for i, cols in ((1, ["a", "b"]), (2, ["a", "c"])):
        bd = year / "S__1" / "D__1" / f"B__{i}"
        bd.mkdir(parents=True)
        c.write_csv_rows(bd / "data_wide.csv", [{k: f"{k}{i}" for k in cols}], cols)
        c.write_json(bd / "DONE.json", {"status": "done", "state": "S", "gp_rows": i})
        (bd / "html").mkdir()
        (bd / "html" / "page_001.html").write_text(f"<html>{i}</html>", encoding="utf-8")
    return year


def test_live_and_archive_yield_the_same_blocks(tmp_path):
    _tree(tmp_path)
    store = BlockStore(tmp_path)
    live = {str(b.rel): b.files for b in store.iter_blocks("2022-2023")}

    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    assert store.mode("2022-2023") == "archive"

    archived = {str(b.rel): b.files for b in store.iter_blocks("2022-2023")}
    assert archived == live


def test_names_filter_loads_only_what_was_asked_for(tmp_path):
    _tree(tmp_path)
    store = BlockStore(tmp_path)
    for blk in store.iter_blocks("2022-2023", names={"DONE.json"}):
        assert set(blk.files) == {"DONE.json"}


def test_consolidate_matches_across_forms(tmp_path):
    _tree(tmp_path)
    store = BlockStore(tmp_path)
    before = tmp_path / "before.csv"
    n_before = c.consolidate_per_block(tmp_path, "data_wide.csv", before)

    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    after = tmp_path / "after.csv"
    n_after = c.consolidate_per_block(tmp_path, "data_wide.csv", after)

    assert n_before == n_after == 2
    assert c.read_csv(before) == c.read_csv(after)


def test_expand_restores_byte_identical_files(tmp_path):
    year = _tree(tmp_path)
    original = {
        p.relative_to(tmp_path).as_posix(): p.read_bytes()
        for p in sorted(year.rglob("*"))
        if p.is_file()
    }
    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)

    dest = tmp_path / "restored"
    assert pai_compact.expand_year(store, "2022-2023", dest)
    restored = {
        p.relative_to(dest).as_posix(): p.read_bytes()
        for p in sorted((dest / "2022-2023").rglob("*"))
        if p.is_file()
    }
    assert restored == original


def test_dropped_files_are_recorded_not_silently_removed(tmp_path):
    year = _tree(tmp_path)
    junk = year / "S__1" / "D__1" / "B__1" / "debug"
    junk.mkdir()
    (junk / "failed_attempt_1.png").write_bytes(b"\x89PNG")

    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    manifest = json.loads(store.manifest_path("2022-2023").read_text())
    assert any(p.endswith("debug/failed_attempt_1.png") for p in manifest["dropped"])


def test_read_global_prefers_parquet_and_keeps_strings(tmp_path):
    src = tmp_path / "t.csv"
    c.write_csv_rows(src, [{"code": "007", "n": "1"}], ["code", "n"])
    pai_compact.to_parquet(src, tmp_path / "t.parquet")
    src.unlink()
    # A leading zero surviving is the point: parquet type inference would eat it.
    assert pai_stores.read_global(tmp_path, "t") == [{"code": "007", "n": "1"}]


def test_verify_compacted_store_rebuilds_and_checks_contracts(tmp_path):
    year = "2022-2023"
    block = tmp_path / year / "S__1" / "D__2" / "B__3"
    block.mkdir(parents=True)
    base = {
        **dict.fromkeys(c.GP_METADATA_FIELDS, ""),
        "year": year,
        "state": "S",
        "state_value": "1",
        "district": "D",
        "district_value": "2",
        "block": "B",
        "block_value": "3",
        "gp_name": "GP",
        "gp_code": "007",
        "block_page": "1",
    }
    scores = []
    wide = {**base}
    theme_links = pai_contracts.load_theme_header_links()
    for order, slug in enumerate(c.CANONICAL_THEME_SLUGS):
        score = str(50 + order / 10)
        header = next(
            header
            for header, link in theme_links.items()
            if link["theme_slug"] == slug and link["language"] == "en"
        )
        scores.append(
            {
                **dict.fromkeys(c.GP_SCORE_FIELDS, ""),
                **base,
                "theme_order": str(order),
                "theme_header": header,
                "theme_slug": slug,
                "score": score,
            }
        )
        wide[f"{slug}_score"] = score
    c.write_csv_rows(block / c.METADATA_CSV, [base], c.GP_METADATA_FIELDS)
    c.write_csv_rows(block / c.SCORES_LONG_CSV, scores, c.GP_SCORE_FIELDS)
    c.write_csv_rows(block / c.DATA_WIDE_CSV, [wide])
    c.write_json(
        block / c.DONE_JSON,
        {"status": "done", "gp_rows": 1, "score_rows": 10, "state": "S"},
    )
    (block / "html").mkdir()
    (block / "html" / "page_001.html").write_text("<table></table>", encoding="utf-8")

    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, year, keep_debug=False)
    assert not (tmp_path / "gp_metadata.csv").exists()
    assert pai_compact.verify_rollup(tmp_path) == 0

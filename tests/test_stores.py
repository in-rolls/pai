"""The block store must give the same answers from an archive as from a live tree."""

import json
from pathlib import Path

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
        pai_contracts.rows_to_typed_parquet(
            [{k: f"{k}{i}" for k in cols}], cols, bd / "data_wide.parquet", "wide"
        )
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


def test_rows_are_typed_and_identical_across_forms(tmp_path):
    _tree(tmp_path)
    store = BlockStore(tmp_path)
    before = [b.rows("data_wide.parquet") for b in store.iter_blocks("2022-2023")]

    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    after = [b.rows("data_wide.parquet") for b in store.iter_blocks("2022-2023")]

    assert before == after == [[{"a": "a1", "b": "b1"}], [{"a": "a2", "c": "c2"}]]


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


def test_expand_refuses_to_overwrite_a_live_year(tmp_path):
    _tree(tmp_path)
    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    dest = tmp_path / "restored"
    assert pai_compact.expand_year(store, "2022-2023", dest)
    newer = dest / "2022-2023" / "S__1" / "D__1" / "B__1" / "DONE.json"
    newer.write_text('{"status": "done", "gp_rows": 99}', encoding="utf-8")
    assert not pai_compact.expand_year(store, "2022-2023", dest)
    assert '"gp_rows": 99' in newer.read_text(encoding="utf-8")


def test_dropped_files_are_recorded_not_silently_removed(tmp_path):
    year = _tree(tmp_path)
    junk = year / "S__1" / "D__1" / "B__1" / "debug"
    junk.mkdir()
    (junk / "failed_attempt_1.png").write_bytes(b"\x89PNG")

    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    manifest = json.loads(store.manifest_path("2022-2023").read_text())
    assert any(p.endswith("debug/failed_attempt_1.png") for p in manifest["dropped"])


def _valid_block(root, year, block_code="3", gp_code="007"):
    block = root / year / "S__1" / "D__2" / f"B__{block_code}"
    block.mkdir(parents=True)
    base = {
        **dict.fromkeys(c.GP_METADATA_FIELDS, ""),
        "year": year,
        "state": "S",
        "state_value": "1",
        "district": "D",
        "district_value": "2",
        "block": "B",
        "block_value": block_code,
        "gp_name": "GP",
        "gp_code": gp_code,
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
    pai_contracts.write_block_tables(block, [base], scores, [wide])
    c.write_json(
        block / c.DONE_JSON,
        {"status": "done", "gp_rows": 1, "score_rows": 10, "state": "S"},
    )
    (block / "html").mkdir()
    (block / "html" / "page_001.html").write_text("<table></table>", encoding="utf-8")
    return block


def test_verify_refuses_a_year_with_a_failed_block(tmp_path, capsys):
    _valid_block(tmp_path, "2022-2023")
    assert pai_compact.verify_rollup(tmp_path) == 0
    failed = tmp_path / "2022-2023" / "S__1" / "D__2" / "B__9"
    failed.mkdir(parents=True)
    c.write_json(failed / "FAILED.json", {"status": "failed", "error": "timeout"})
    assert pai_compact.verify_rollup(tmp_path) == 1
    assert "FAILED.json" in capsys.readouterr().out


def test_read_global_keeps_strings_and_concatenates_compacted_history(tmp_path):
    src = tmp_path / "t.csv"
    c.write_csv_rows(src, [{"code": "007", "n": "1"}], ["code", "n"])
    pai_compact.to_parquet(src, tmp_path / "t.parquet")
    src.unlink()
    # A leading zero surviving is the point: parquet type inference would eat it.
    assert pai_stores.read_global(tmp_path, "t") == [{"code": "007", "n": "1"}]

    # The scraper starts a fresh CSV after compaction; the log is parquet + csv.
    c.write_csv_rows(src, [{"code": "008", "n": "2"}], ["code", "n"])
    assert pai_stores.read_global(tmp_path, "t") == [
        {"code": "007", "n": "1"},
        {"code": "008", "n": "2"},
    ]
    # A second compaction folds the tail into the history instead of replacing it.
    pai_compact.to_parquet(src, tmp_path / "t.parquet")
    src.unlink()
    assert [r["code"] for r in pai_stores.read_global(tmp_path, "t")] == ["007", "008"]


def test_read_global_renames_legacy_manifest_columns(tmp_path):
    c.write_csv_rows(tmp_path / "block_manifest.csv", [{"data_wide_csv": "x/data_wide.csv"}])
    assert pai_stores.read_global(tmp_path, "block_manifest") == [{"wide_file": "x/data_wide.csv"}]


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
    pai_contracts.write_block_tables(block, [base], scores, [wide])
    c.write_json(
        block / c.DONE_JSON,
        {"status": "done", "gp_rows": 1, "score_rows": 10, "state": "S"},
    )
    (block / "html").mkdir()
    (block / "html" / "page_001.html").write_text("<table></table>", encoding="utf-8")

    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, year, keep_debug=False)
    assert pai_compact.verify_rollup(tmp_path) == 0


def test_failed_expansion_leaves_no_partial_live_tree(tmp_path):
    _tree(tmp_path)
    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    manifest_path = store.manifest_path("2022-2023")
    manifest = json.loads(manifest_path.read_text())
    first = next(iter(manifest["files"]))
    manifest["files"][first]["sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest))
    dest = tmp_path / "restored"
    assert not pai_compact.expand_year(store, "2022-2023", dest)
    assert not (dest / "2022-2023").exists()
    assert not any(p.name.startswith(".expand-") for p in dest.iterdir()) if dest.exists() else True


def test_files_nested_under_excluded_dirs_are_never_archived_as_block_data(tmp_path):
    year = _tree(tmp_path)
    nested = year / "S__1" / "D__1" / "B__1" / "debug" / "attempt_2"
    nested.mkdir(parents=True)
    (nested / "notes.json").write_text("{}", encoding="utf-8")
    data_files, _html, dropped = pai_compact.collect(year, tmp_path, keep_debug=False)
    assert not any(arc.endswith("debug/attempt_2/notes.json") for _p, arc in data_files)
    assert any(arc.endswith("debug/attempt_2/notes.json") for _p, arc in dropped)


def test_compact_retires_the_live_tree_out_of_the_namespace(tmp_path, monkeypatch):
    _tree(tmp_path)
    store = BlockStore(tmp_path)
    seen = []
    real_rmtree = pai_compact.shutil.rmtree

    def spy(path, *args, **kwargs):
        seen.append(Path(path).name)
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pai_compact.shutil, "rmtree", spy)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    # The delete ran on the renamed directory, never on the live year name.
    assert seen and all(name.startswith(".retired-2022-2023-") for name in seen)
    assert store.mode("2022-2023") == "archive"


def test_retired_and_staging_dirs_are_not_years(tmp_path):
    (tmp_path / "2022-2023").mkdir()
    (tmp_path / ".retired-2022-2023-123").mkdir()
    (tmp_path / ".expand-2023-2024-abc").mkdir()
    assert BlockStore(tmp_path).years() == ["2022-2023"]


def test_repeated_log_compaction_keeps_earlier_archives(tmp_path, monkeypatch):
    (tmp_path / "pai_scrape.log").write_text("first\n")
    pai_compact.compact_globals(tmp_path)
    (tmp_path / "pai_scrape.log").write_text("second\n")
    pai_compact.compact_globals(tmp_path)  # same second is fine: a serial is appended
    assert len(list(tmp_path.glob("pai_scrape.*.log.zst"))) == 2


def test_folding_the_same_csv_segment_twice_does_not_duplicate_rows(tmp_path):
    import pyarrow.parquet as pq

    src = tmp_path / "t.csv"
    c.write_csv_rows(src, [{"run_id": "r1", "n": "1"}], ["run_id", "n"])
    pai_compact.to_parquet(src, tmp_path / "t.parquet")
    # Interrupted before the CSV was removed: the retry sees the same segment again.
    pai_compact.to_parquet(src, tmp_path / "t.parquet")
    assert pq.read_table(tmp_path / "t.parquet").to_pylist() == [{"run_id": "r1", "n": "1"}]


def test_recompacting_without_captures_drops_the_obsolete_html_archive(tmp_path):
    _valid_block(tmp_path, "2022-2023")
    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    assert store.html_archive_path("2022-2023").exists()
    assert pai_compact.expand_year(store, "2022-2023", tmp_path)
    import shutil

    for html_dir in (tmp_path / "2022-2023").rglob("html"):
        shutil.rmtree(html_dir)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    assert not store.html_archive_path("2022-2023").exists()
    assert pai_compact.verify_rollup(tmp_path) == 0


def test_expansion_rejects_archive_members_the_manifest_never_described(tmp_path):
    _tree(tmp_path)
    store = BlockStore(tmp_path)
    assert pai_compact.compact_year(store, "2022-2023", keep_debug=False)
    # Rebuild the blocks archive with one extra, undeclared block.
    dest = tmp_path / "restore1"
    assert pai_compact.expand_year(store, "2022-2023", dest)
    extra = dest / "2022-2023" / "S__1" / "D__1" / "B__9"
    extra.mkdir(parents=True)
    c.write_json(extra / "DONE.json", {"status": "done", "gp_rows": 1})
    pairs = [
        (p, p.relative_to(dest).as_posix())
        for p in (dest / "2022-2023").rglob("*")
        if p.is_file() and p.suffix in (".parquet", ".json")
    ]
    pai_compact.write_archive(pairs, store.archive_path("2022-2023"))
    dest2 = tmp_path / "restore2"
    assert not pai_compact.expand_year(store, "2022-2023", dest2)
    assert not (dest2 / "2022-2023").exists()

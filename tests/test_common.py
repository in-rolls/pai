"""Unit tests for the shared pai_common helpers."""

import pai_common as c


def test_csv_roundtrip(tmp_path):
    path = tmp_path / "x.csv"
    rows = [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
    c.write_csv_rows(path, rows, ["a", "b"])
    assert c.read_csv(path) == rows


def test_read_csv_missing_returns_empty(tmp_path):
    assert c.read_csv(tmp_path / "nope.csv") == []


def test_json_roundtrip_preserves_unicode(tmp_path):
    path = tmp_path / "x.json"
    obj = {"status": "done", "gp_rows": 3, "name": "Thāngaon"}
    c.write_json(path, obj)
    assert c.read_json(path) == obj


def test_append_csv_rows_writes_header_once(tmp_path):
    path = tmp_path / "a.csv"
    c.append_csv_rows(path, [{"a": "1", "b": "2"}], ["a", "b"])
    c.append_csv_rows(path, [{"a": "3", "b": "4"}], ["a", "b"])
    rows = c.read_csv(path)
    assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_consolidate_per_block_unions_and_dedups(tmp_path):
    year_dir = tmp_path / "2022-2023"
    # block 1 has columns a,b; block 2 adds column c -> union header
    b1 = year_dir / "S__1" / "D__1" / "B__1"
    b2 = year_dir / "S__1" / "D__1" / "B__2"
    b1.mkdir(parents=True)
    b2.mkdir(parents=True)
    c.write_csv_rows(b1 / "data_wide.csv", [{"a": "1", "b": "2"}], ["a", "b"])
    c.write_csv_rows(b2 / "data_wide.csv", [{"a": "3", "c": "9"}], ["a", "c"])

    out = tmp_path / "wide.csv"
    n = c.consolidate_per_block(tmp_path, "data_wide.csv", out)
    assert n == 2
    rows = c.read_csv(out)
    assert {r["a"] for r in rows} == {"1", "3"}
    assert set(rows[0].keys()) == {"a", "b", "c"}  # union of headers


def test_consolidate_makes_cache_paths_collection_relative(tmp_path):
    block = tmp_path / "2023-2024" / "S__1" / "D__2" / "B__3"
    block.mkdir(parents=True)
    c.write_csv_rows(
        block / "metadata.csv",
        [
            {
                "block_dir": "runs/old_name/2023-2024/S__1/D__2/B__3",
                "block_data_wide_csv": "runs/old_name/.../data_wide.csv",
                "block_html_file": "runs/old_name/.../page_001.html",
                "block_page": "1",
            }
        ],
    )
    out = tmp_path / "metadata.csv"
    c.consolidate_per_block(tmp_path, "metadata.csv", out)
    row = c.read_csv(out)[0]
    rel = "2023-2024/S__1/D__2/B__3"
    assert row["block_dir"] == rel
    assert row["block_data_wide_csv"] == f"{rel}/data_wide.csv"
    assert row["block_html_file"] == f"{rel}/html/page_001.html"


def test_read_block_status_prefers_done(tmp_path):
    bd = tmp_path / "B__1"
    bd.mkdir()
    c.write_json(bd / "FAILED.json", {"status": "failed"})
    c.write_json(bd / "DONE.json", {"status": "done", "gp_rows": 5})
    assert c.read_block_status(bd)["status"] == "done"


def test_read_block_status_none_when_absent(tmp_path):
    assert c.read_block_status(tmp_path) is None

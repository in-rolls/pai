"""Tests for data_summary.build over a tiny synthetic block tree."""

import data_summary
import pai_common as c


def make_block(year_dir, state, district, block, status, gp_rows=0, overalls=()):
    bd = year_dir / f"{state}__1" / f"{district}__1" / f"{block}__1"
    bd.mkdir(parents=True)
    c.write_json(
        bd / "DONE.json",
        {
            "status": status,
            "state": state,
            "district": district,
            "gp_rows": gp_rows,
            "score_rows": gp_rows * 10,
        },
    )
    if overalls:
        rows = [{"gp_name": f"gp{i}", c.OVERALL_COL: str(v)} for i, v in enumerate(overalls)]
        c.write_csv_rows(bd / "data_wide.csv", rows, ["gp_name", c.OVERALL_COL])


def test_build_counts_and_mean(tmp_path):
    year_dir = tmp_path / "2022-2023"
    make_block(year_dir, "Alpha", "DistA", "B1", "done", gp_rows=2, overalls=(50.0, 60.0))
    make_block(year_dir, "Alpha", "DistA", "B2", "done_no_rows")
    make_block(year_dir, "Beta", "DistB", "B3", "done_no_data_available")

    year_rows, state_rows = data_summary.build(tmp_path, ["2022-2023"])

    yr = {r["year"]: r for r in year_rows}["2022-2023"]
    assert yr["gp_count"] == 2
    assert yr["score_rows"] == 20
    assert yr["blocks_with_data"] == 1
    assert yr["blocks_no_data"] == 2  # done_no_rows + done_no_data_available
    assert yr["states_total"] == 2
    assert yr["states_with_data"] == 1
    assert yr["districts"] == 2

    alpha = next(r for r in state_rows if r["state"] == "Alpha")
    assert alpha["gp_count"] == 2
    assert alpha["mean_overall_pai"] == 55.0


def test_build_skips_missing_year(tmp_path):
    year_rows, state_rows = data_summary.build(tmp_path, ["2099-2100"])
    assert year_rows == []
    assert state_rows == []

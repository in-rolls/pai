"""Progress reporting accepts any isolated resumable collection root."""

import pai_common as c
import pai_contracts
import pai_stores
import scrape_progress


def test_main_accepts_data_dir(tmp_path, capsys):
    block = tmp_path / "2023-2024" / "S__1" / "D__2" / "B__3"
    block.mkdir(parents=True)
    pai_contracts.rows_to_typed_parquet(
        [{"gp_name": "GP"}], ["gp_name"], block / c.BLOCK_TABLES["wide"], "wide"
    )
    c.append_csv_rows(
        tmp_path / "block_manifest.csv",
        [
            {
                **dict.fromkeys(c.BLOCK_MANIFEST_FIELDS, ""),
                "year": "2023-2024",
                "status": "done",
                "state": "S",
                "state_value": "1",
                "district_value": "2",
                "block_value": "3",
                "block_dir": str(block),
            }
        ],
        c.BLOCK_MANIFEST_FIELDS,
    )

    c.append_csv_rows(
        tmp_path / "block_manifest.csv",
        [
            {
                **dict.fromkeys(c.BLOCK_MANIFEST_FIELDS, ""),
                "year": "2023-2024",
                "status": "done",
                "state": "S",
                "state_value": "1",
                "district_value": "2",
                "block_value": "3",
                "block_dir": "runs/old_root/2023-2024/S__1/D__2/B__3",
            },
            {
                **dict.fromkeys(c.BLOCK_MANIFEST_FIELDS, ""),
                "year": "2023-2024",
                "status": "done",
                "state": "S",
                "state_value": "1",
                "district_value": "2",
                "block_value": "3",
                "block_dir": str(block),
            },
        ],
        c.BLOCK_MANIFEST_FIELDS,
    )
    results = scrape_progress.analyze_progress(
        pai_stores.read_global(tmp_path, "block_manifest"), "2023-2024"
    )
    assert len(results["successful"]) == 1

    assert scrape_progress.main(["--data-dir", str(tmp_path), "--year", "2023-2024"]) == 0
    output = capsys.readouterr().out
    assert "Successful with data" in output
    assert "Score tables on disk: 1" in output


def test_inspector_reads_the_derived_tables(tmp_path, capsys):
    import pai_inspect_output

    (tmp_path / "derived").mkdir()
    pai_contracts.rows_to_typed_parquet(
        [{"year": "2023-2024", "state": "S", "gp_code": "1"}],
        ["year", "state", "gp_code"],
        tmp_path / "derived" / "gp_metadata.parquet",
        "metadata",
    )
    pai_contracts.rows_to_typed_parquet(
        [{"year": "2023-2024", "gp_code": "1", "score": "1.0"}],
        ["year", "gp_code", "score"],
        tmp_path / "derived" / "gp_scores_long.parquet",
        "scores",
    )
    monkey_argv = ["x", "--out", str(tmp_path)]
    import sys

    old = sys.argv
    sys.argv = monkey_argv
    try:
        pai_inspect_output.main()
    finally:
        sys.argv = old
    assert "GP metadata rows: 1" in capsys.readouterr().out

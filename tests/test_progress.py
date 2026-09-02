"""Progress reporting accepts any isolated resumable collection root."""

import pai_common as c
import scrape_progress


def test_main_accepts_data_dir(tmp_path, capsys):
    block = tmp_path / "2023-2024" / "S__1" / "D__2" / "B__3"
    block.mkdir(parents=True)
    c.write_csv_rows(block / c.DATA_WIDE_CSV, [{"gp_name": "GP"}])
    c.append_csv_rows(
        tmp_path / "block_manifest.csv",
        [
            {
                **dict.fromkeys(c.BLOCK_MANIFEST_FIELDS, ""),
                "year": "2023-2024",
                "status": "done",
                "state": "S",
                "block_dir": str(block),
            }
        ],
        c.BLOCK_MANIFEST_FIELDS,
    )

    assert scrape_progress.main(["--data-dir", str(tmp_path), "--year", "2023-2024"]) == 0
    output = capsys.readouterr().out
    assert "Successful with data" in output
    assert "CSVs on disk: 1" in output

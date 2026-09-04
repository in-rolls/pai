"""Resumable skip decisions, incl. the --retry-no-data re-verification path."""

import pai_common as c
from pai_scraper_resumable import skip_decision


def test_confirmed_no_data_skipped_by_default():
    assert (
        skip_decision("done_no_data_available", 0, retry_no_data=False, retry_empty=False)
        == "no_data"
    )


def test_retry_no_data_reverifies_even_without_retry_empty():
    # The bug we fixed: a no-data block must re-scrape under --retry-no-data even
    # when --retry-empty is off (it must not fall through to the "done" skip).
    assert skip_decision("done_no_data_available", 0, retry_no_data=True, retry_empty=False) is None
    assert skip_decision("done_no_data_available", 0, retry_no_data=True, retry_empty=True) is None


def test_already_reverified_no_data_is_not_redone():
    # A no-data block already re-verified this recovery (stamped with confirmations) is
    # skipped even under --retry-no-data, so repeated passes only chase un-verified gaps.
    assert (
        skip_decision(
            "done_no_data_available",
            0,
            retry_no_data=True,
            retry_empty=False,
            prior_reverified=True,
        )
        == "no_data"
    )
    # ...but an un-stamped (original-scrape) no-data block still gets re-verified.
    assert (
        skip_decision(
            "done_no_data_available",
            0,
            retry_no_data=True,
            retry_empty=False,
            prior_reverified=False,
        )
        is None
    )


def test_block_with_data_always_skipped():
    assert skip_decision("done", 71, retry_no_data=True, retry_empty=True) == "done"


def test_zero_row_block_respects_retry_empty():
    assert skip_decision("done_no_rows", 0, retry_no_data=False, retry_empty=False) == "done"
    assert skip_decision("done_no_rows", 0, retry_no_data=False, retry_empty=True) is None


def test_resume_threshold_follows_the_flag_that_produced_the_stored_count():
    from pai_scraper_resumable import required_confirmations

    assert required_confirmations("done", complete_confirm=1, no_data_confirm=0) == 2
    assert (
        required_confirmations("done_no_data_available", complete_confirm=1, no_data_confirm=0) == 1
    )
    assert (
        required_confirmations("done_no_data_available", complete_confirm=3, no_data_confirm=1) == 2
    )


def test_scraper_refuses_years_that_exist_only_as_archives(tmp_path):
    from pai_scraper_resumable import archived_years

    (tmp_path / "blocks_2022-2023.tar.zst").write_bytes(b"")
    (tmp_path / "2023-2024").mkdir()
    assert archived_years(tmp_path, ["2022-2023", "2023-2024", "2099-2100"]) == ["2022-2023"]


def test_scraper_accepts_a_universe_dir_for_its_final_rebuild(monkeypatch):
    from pai_scraper_resumable import parse_args

    monkeypatch.setattr(
        "sys.argv", ["x", "--national-official-controls", "--universe-data-dir", "runs/u"]
    )
    args = parse_args()
    assert args.universe_data_dir == "runs/u"


def test_scraper_refuses_a_manifest_with_an_older_header(tmp_path):
    from pai_scraper_resumable import manifest_header_current

    path = tmp_path / "block_manifest.csv"
    assert manifest_header_current(path)
    c.append_csv_rows(path, [dict.fromkeys(c.BLOCK_MANIFEST_FIELDS, "")], c.BLOCK_MANIFEST_FIELDS)
    assert manifest_header_current(path)
    path.write_text("run_id,year,data_wide_csv\n", encoding="utf-8")
    assert not manifest_header_current(path)


def test_empty_manifest_file_is_a_blank_slate(tmp_path):
    from pai_scraper_resumable import manifest_header_current

    path = tmp_path / "block_manifest.csv"
    path.write_bytes(b"")
    assert manifest_header_current(path)

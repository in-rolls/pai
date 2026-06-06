"""Resumable skip decisions, incl. the --retry-no-data re-verification path."""

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


def test_block_with_data_always_skipped():
    assert skip_decision("done", 71, retry_no_data=True, retry_empty=True) == "done"


def test_zero_row_block_respects_retry_empty():
    assert skip_decision("done_no_rows", 0, retry_no_data=False, retry_empty=False) == "done"
    assert skip_decision("done_no_rows", 0, retry_no_data=False, retry_empty=True) is None

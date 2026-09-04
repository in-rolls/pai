"""The GridView keeps its page index across the Search postback (observed on the
2023-2024 page: the confirmation retrieval of a 101-GP block opened on page 2 and
saw one GP). Every retrieval must rewind to page 1 before parsing."""

import asyncio
import logging

import pai_scraper_resumable as scraper
import pytest


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    async def count(self):
        return 1 if self.selector in ("#btnPrev", "#GVdata", "#ddl_State") else 0

    async def is_disabled(self):
        return self.page.index == 0

    async def get_attribute(self, name):
        if name == "disabled":
            return "disabled" if self.page.index == 0 else None
        return "btn"

    async def evaluate(self, _script):
        return f"page-{self.page.index}"


class FakePage:
    """A result grid on page `index` (0-based); Prev moves back one page."""

    def __init__(self, index, sticky=False):
        self.index = index
        self.sticky = sticky
        self.clicks = 0
        self.searches = 0

    def locator(self, selector):
        return FakeLocator(self, selector)

    async def click(self, selector, timeout=None):
        if selector == "#btnSubmit":
            self.searches += 1
            return
        assert selector == "#btnPrev"
        self.clicks += 1
        if not self.sticky:
            self.index -= 1

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, _ms):
        return None


@pytest.fixture(autouse=True)
def _no_sweetalert(monkeypatch):
    async def none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scraper, "dismiss_sweetalert", none)


def test_rewind_from_last_page_clicks_prev_until_disabled():
    page = FakePage(index=2)
    clicks = asyncio.run(scraper.rewind_to_first_page(page, logging.getLogger("t"), "#GVdata"))
    assert clicks == 2
    assert page.index == 0
    assert page.searches == 1


def test_rewind_on_first_page_is_a_no_op():
    page = FakePage(index=0)
    clicks = asyncio.run(scraper.rewind_to_first_page(page, logging.getLogger("t"), "#GVdata"))
    assert clicks == 0
    assert page.clicks == 0
    assert page.searches == 0


def test_rewind_fails_loudly_when_prev_does_nothing():
    page = FakePage(index=1, sticky=True)
    with pytest.raises(RuntimeError, match="did not (change|render)"):
        asyncio.run(scraper.rewind_to_first_page(page, logging.getLogger("t"), "#GVdata"))


def test_reset_page_index_only_rewinds_without_searching():
    page = FakePage(index=1)
    clicks = asyncio.run(scraper.reset_page_index(page, logging.getLogger("t"), "#GVdata"))
    assert clicks == 1
    assert page.index == 0
    assert page.searches == 0


def test_rows_lack_grades_only_when_theme_grades_are_all_empty():
    def row(grade):
        return {
            "gp": {"gp_code": "1"},
            "scores": [
                {"theme_slug": "overall_pai_score", "score": "61.5", "grade": ""},
                {
                    "theme_slug": "t1_poverty_free_and_enhanced_livelihoods_panchayat",
                    "score": "70.1",
                    "grade": grade,
                },
            ],
        }

    assert scraper.rows_lack_grades([row(""), row("")])
    assert not scraper.rows_lack_grades([row("B"), row("")])
    assert not scraper.rows_lack_grades([])


class StuckPage(FakePage):
    """The pager click never renders: the table signature stays the same."""

    async def click(self, selector, timeout=None):
        self.clicks += 1


def test_pager_timeout_without_alert_raises_instead_of_ending_pagination(monkeypatch):
    monkeypatch.setattr(scraper, "PAGER_RENDER_POLLS", 2)
    page = StuckPage(index=0)
    with pytest.raises(RuntimeError, match="did not render"):
        asyncio.run(scraper.click_pager_button(page, "#btnNext", logging.getLogger("t"), "#GVdata"))


def test_pager_click_answered_by_no_data_alert_ends_pagination(monkeypatch):
    monkeypatch.setattr(scraper, "PAGER_RENDER_POLLS", 2)

    async def alert(*_args, **_kwargs):
        return "Details are not available for above request"

    monkeypatch.setattr(scraper, "dismiss_sweetalert", alert)
    page = StuckPage(index=0)
    assert not asyncio.run(
        scraper.click_pager_button(page, "#btnNext", logging.getLogger("t"), "#GVdata")
    )


def test_grades_appearing_on_the_last_rerender_are_accepted(monkeypatch, tmp_path):
    def result(grade):
        return {
            "rows": [
                {
                    "gp": {"gp_code": "1"},
                    "scores": [
                        {"theme_slug": "overall_pai_score", "score": "61.5", "grade": ""},
                        {
                            "theme_slug": "t1_poverty_free_and_enhanced_livelihoods_panchayat",
                            "score": "70.1",
                            "grade": grade,
                        },
                    ],
                }
            ]
        }

    parses = iter([result(""), result(""), result(""), result("B")])

    async def parse(*_args, **_kwargs):
        return next(parses)

    async def none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(scraper, "MAX_RERENDER_ATTEMPTS", 3)
    monkeypatch.setattr(scraper, "parse_current_table", parse)
    monkeypatch.setattr(scraper, "save_html", none)
    monkeypatch.setattr(scraper, "click_search", none)
    page = FakePage(index=0)
    parsed = asyncio.run(
        scraper.parse_rendered_page(
            page,
            result_selector="#GVdata",
            layout="legacy",
            html_path=tmp_path / "p.html",
            logger=logging.getLogger("t"),
            where="w",
        )
    )
    assert not scraper.rows_lack_grades(parsed["rows"])

"""Parser regression tests — the logic that broke repeatedly (GVdata flat vs
GVdataT legacy). Runs the actual in-browser parser over saved table fixtures via
Playwright set_content. Skips cleanly if chromium is unavailable."""

from pathlib import Path

import pai_scraper_resumable as scraper
import pytest
from playwright.async_api import async_playwright

FIXTURES = Path(__file__).parent / "fixtures"


async def _parse(fixture: str, selector: str, layout: str) -> dict:
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:  # chromium not installed in this env
            pytest.skip(f"chromium unavailable: {e}")
        page = await (await browser.new_context()).new_page()
        await page.set_content((FIXTURES / fixture).read_text(encoding="utf-8"))
        result = await scraper.parse_current_table(page, selector, layout)
        await browser.close()
        return result


async def test_flat_parser_extracts_overall_and_themes():
    rows = (await _parse("flat_GVdata_page.html", "#GVdata", "flat"))["rows"]
    assert len(rows) == 3  # Campbell Bay has 3 GPs

    first = rows[0]
    assert first["gp"]["gp_name"]
    slugs = [s["theme_slug"] for s in first["scores"]]
    assert slugs[0] == "overall_pai_score"
    assert len(first["scores"]) == 10  # overall + 9 themes
    assert first["wide"]["overall_pai_score_score"]  # populated overall score


async def test_legacy_parser_extracts_gp_code():
    rows = (await _parse("legacy_GVdataT_page.html", "#GVdataT", "legacy"))["rows"]
    assert len(rows) == 4  # Saspol has 4 GPs
    assert rows[0]["gp"]["gp_name"]
    assert rows[0]["gp"]["gp_code"]  # legacy layout carries the GP code


async def test_layout_dispatch_is_independent():
    """The flat selector must not match the legacy table and vice versa."""
    flat = (await _parse("flat_GVdata_page.html", "#GVdataT", "legacy"))["rows"]
    assert flat == []  # #GVdataT absent in the flat fixture

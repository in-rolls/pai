#!/usr/bin/env python3
"""
Resumable scraper for PAI Gram Panchayat score pages.

Core behavior:
- Iterates PAI year -> State/UT -> District -> Block.
- Saves rendered block-level HTML pages.
- Parses the results table (#GVdataT legacy / #GVdata flat) from the rendered page.
- Follows the "Next 100" pagination button to the last page.
- Writes per-block outputs (the source of truth; global indexes are rebuilt separately
  via pai_rebuild_index.py).
- Resumes by skipping blocks with DONE.json.
- Records failures and continues instead of crashing.

Install:
  uv sync && uv run playwright install chromium

Smoke test:
  uv run scripts/pai_scraper_resumable.py \
    --years 2022-2023 --state-contains Bihar --limit-districts 1 --limit-blocks 3

Full run:
  uv run scripts/pai_scraper_resumable.py --years 2022-2023 2023-2024 --headless --delay 1.5
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import (  # noqa: E402
    BLOCK_MANIFEST_FIELDS,
    DROPDOWN_INVENTORY_FIELDS,
    FLAT_PAGE_SIZE,
    GP_METADATA_FIELDS,
    GP_SCORE_FIELDS,
    YEAR_CONFIGS,
    append_csv_rows,
    read_json,
    write_csv_rows,
    write_json,
)

# Default selector for the legacy (2022-2023) page. The 2023-2024 path passes
# its own selector/layout explicitly via YEAR_CONFIGS.
RESULT_TABLE_SELECTOR = "#GVdataT"

# When --block-third-party is set, abort these. The PAI server serves its large
# DataTables export libraries (dataTables.js ~675KB, jszip ~386KB, vfs_fonts,
# pdfmake) very slowly during load spikes; because they are blocking <script>s
# in the <head>, the HTML parser stalls there and the form (#ddl_State) never
# renders in the browser even though the page itself returns fine. None of them
# are needed to read the raw results table or paginate via #btnNext, so we drop
# them. The small DDLFill_v1.js (dropdown cascade) is essential and NOT blocked.
BLOCKED_URL_SUBSTRINGS = (
    "googletagmanager",
    "google-analytics",
    "bhashini",
    "datatables",
    "jszip",
    "vfs_fonts",
    "pdfmake",
)
BLOCKED_RESOURCE_TYPES = ("image", "font", "media")

# The results table paginates 100 GPs per "Next 100 >>" click. On the 2023-2024
# (flat) page the server never marks #btnNext disabled, so we detect the last
# page by a short page (fewer than FLAT_PAGE_SIZE rows) plus a table-change check.
NEXT_BUTTON_SELECTOR = "#btnNext"
SWEETALERT_OVERLAY_SELECTOR = ".sweet-overlay"
SWEETALERT_CONFIRM_SELECTOR = (
    ".sweet-alert button.confirm, .sweet-alert .sa-confirm-button-container button"
)

BAD_OPTION_TEXT = (
    "-select-",
    "- select -",
    "- चुनें -",
    "लोडिंग",
    "loading",
    "चुनें",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clean_label(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s*\[[^\]]+\]\s*$", "", text)
    return text.strip()


def safe_component(text: str, fallback: str, max_len: int = 90) -> str:
    text = clean_label(text)
    text = re.sub(r"[^A-Za-z0-9_.() -]+", "_", text)
    text = re.sub(r"\s+", "_", text).strip("._ ")
    text = text[:max_len].strip("._ ")
    return text or fallback


def option_label_with_code(option: dict[str, str], fallback_prefix: str) -> str:
    label = safe_component(option["text"], fallback_prefix)
    return f"{label}__{option['value']}"


def is_real_option(option: dict[str, str]) -> bool:
    text = (option.get("text") or "").strip()
    value = (option.get("value") or "").strip()

    if not text or not value:
        return False
    if value in {"0", "-1"}:
        return False

    low = text.lower()
    return not any(bad in low for bad in BAD_OPTION_TEXT)


def skip_decision(
    prior_status: str, prior_rows: int, *, retry_no_data: bool, retry_empty: bool
) -> str | None:
    """Decide whether a previously-finished block should be skipped on a resumable run.

    Returns "no_data" (skip a confirmed no-data block), "done" (skip a finished block),
    or None (re-scrape it). `--retry-no-data` re-verifies no-data blocks; `--retry-empty`
    re-does zero-row blocks.
    """
    if prior_status == "done_no_data_available":
        return None if retry_no_data else "no_data"
    if prior_rows > 0 or not retry_empty:
        return "done"
    return None


def setup_logger(out_dir: Path) -> logging.Logger:
    out_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("pai_scraper")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    fh = logging.FileHandler(out_dir / "pai_scrape.log", encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


async def dismiss_sweetalert(page, logger: logging.Logger) -> str | None:
    """
    Dismiss any visible SweetAlert modal and return the message text if one was found.
    Returns None if no modal was visible.
    """
    try:
        overlay = page.locator(SWEETALERT_OVERLAY_SELECTOR)
        if await overlay.count() == 0:
            return None

        is_visible = await overlay.evaluate("el => window.getComputedStyle(el).display !== 'none'")
        if not is_visible:
            return None

        message = ""
        try:
            alert_box = page.locator(".sweet-alert")
            if await alert_box.count() > 0:
                message = await alert_box.inner_text()
                message = message.strip()
        except Exception:
            pass

        confirm_btn = page.locator(SWEETALERT_CONFIRM_SELECTOR)
        if await confirm_btn.count() > 0:
            await confirm_btn.first.click(timeout=5000)
            await page.wait_for_timeout(500)
            logger.info("Dismissed SweetAlert: %s", message[:100] if message else "(no message)")
            return message

        await overlay.click(timeout=5000)
        await page.wait_for_timeout(500)
        logger.info(
            "Dismissed SweetAlert by clicking overlay: %s",
            message[:100] if message else "(no message)",
        )
        return message

    except Exception as e:
        logger.debug("dismiss_sweetalert error (non-fatal): %s", e)
        return None


async def get_options(page, selector: str, timeout: int = 60) -> list[dict[str, str]]:
    start = asyncio.get_event_loop().time()

    while True:
        if asyncio.get_event_loop().time() - start > timeout:
            raise TimeoutError(f"Timeout waiting for real options in {selector}")

        options = await page.locator(f"{selector} option").evaluate_all(
            """
            opts => opts.map(o => ({
                text: (o.textContent || '').trim(),
                value: o.value || ''
            }))
            """
        )

        real = [o for o in options if is_real_option(o)]
        if real:
            return real

        await asyncio.sleep(1)


async def get_select_value(page, selector: str) -> str:
    if await page.locator(selector).count() == 0:
        return ""
    return (await page.locator(selector).input_value()).strip()


async def get_text(page, selector: str) -> str:
    if await page.locator(selector).count() == 0:
        return ""
    return (await page.locator(selector).inner_text()).strip()


async def select_value(page, selector: str, value: str, wait_ms: int = 900) -> None:
    await page.select_option(selector, value=value)
    await page.wait_for_timeout(wait_ms)


async def goto_pai_form(page, url: str, logger: logging.Logger) -> bool:
    """
    Robust navigation for the PAI ASP.NET pages.

    Do not wait for full domcontentloaded because third-party scripts can stall.
    Return False instead of raising so the scraper can record and continue.
    """
    last_error: Exception | None = None

    for attempt in range(1, 4):
        logger.info("Navigating attempt %s/3: %s", attempt, url)

        try:
            await page.goto(url, wait_until="commit", timeout=120000)
        except Exception as e:
            last_error = e
            logger.warning("goto(commit) failed on attempt %s/3: %s", attempt, e)

        try:
            # Give the parser time to build the form. Do not call window.stop too early.
            await page.wait_for_selector("#ddl_State", state="attached", timeout=120000)
            await page.wait_for_selector("#btnSubmit", state="attached", timeout=120000)
            return True
        except PlaywrightTimeoutError as e:
            last_error = e
            logger.warning(
                "Could not find PAI form controls on attempt %s/3. current_url=%s",
                attempt,
                page.url,
            )

            # Save a diagnostic page before blanking/retrying.
            try:
                logger.debug("Page title after failed navigation: %s", await page.title())
            except Exception:
                pass

            try:
                await page.goto("about:blank", wait_until="commit", timeout=15000)
            except Exception:
                pass

            await page.wait_for_timeout(2000 * attempt)

    logger.error("Could not load PAI form at %s. Last error: %r", url, last_error)
    return False


async def load_year_page(
    page,
    year: str,
    logger: logging.Logger,
    allow_year_mismatch: bool = False,
) -> dict[str, str] | None:
    conf = YEAR_CONFIGS[year]
    logger.info("Loading %s page: %s", year, conf["url"])

    ok = await goto_pai_form(page, conf["url"], logger)
    if not ok:
        return None

    info = {
        "page_url": page.url,
        "page_title": await page.title(),
        "page_heading": await get_text(page, "#lblHeading"),
        "actual_fy_value": await get_select_value(page, "#hdn_FYID"),
        "expected_fy_value": conf["expected_fy_value"],
    }

    logger.info("Loaded: url=%s", info["page_url"])
    logger.info("Heading: %s", info["page_heading"])
    logger.info(
        "FY value: actual=%s expected=%s", info["actual_fy_value"], info["expected_fy_value"]
    )

    if info["actual_fy_value"] and info["actual_fy_value"] != info["expected_fy_value"]:
        msg = (
            f"Year mismatch for {year}: expected hdn_FYID="
            f"{info['expected_fy_value']}, got {info['actual_fy_value']}. "
            f"URL={info['page_url']}, heading={info['page_heading']!r}"
        )
        if allow_year_mismatch:
            logger.warning(msg)
        else:
            logger.error(msg)
            return None

    return info


async def ensure_state_district(
    page, state: dict[str, str], district: dict[str, str], logger: logging.Logger
) -> None:
    await dismiss_sweetalert(page, logger)
    await page.wait_for_selector("#ddl_State", state="attached", timeout=60000)

    if await get_select_value(page, "#ddl_State") != state["value"]:
        await select_value(page, "#ddl_State", state["value"])
        await dismiss_sweetalert(page, logger)
        await get_options(page, "#ddl_District", timeout=60)

    if await get_select_value(page, "#ddl_District") != district["value"]:
        await select_value(page, "#ddl_District", district["value"])
        await dismiss_sweetalert(page, logger)
        await get_options(page, "#ddl_Block", timeout=60)


async def click_search(
    page, logger: logging.Logger, result_selector: str = RESULT_TABLE_SELECTOR
) -> str | None:
    """
    Click search and wait for results or a SweetAlert.
    Returns the SweetAlert message if one appeared (e.g., "Details are not available"),
    or None if results loaded normally.
    """
    await page.click("#btnSubmit")

    try:
        await page.wait_for_load_state("commit", timeout=15000)
    except Exception:
        pass

    for _ in range(60):
        await page.wait_for_timeout(500)

        alert_msg = await dismiss_sweetalert(page, logger)
        if alert_msg:
            return alert_msg

        try:
            if await page.locator("#ddl_State").count() > 0:
                if await page.locator(result_selector).count() > 0:
                    await page.wait_for_timeout(1000)
                    return None
        except Exception:
            pass

    try:
        await page.wait_for_selector("#ddl_State", state="attached", timeout=60000)
    except PlaywrightTimeoutError:
        logger.warning("Form controls did not reappear after search.")

    alert_msg = await dismiss_sweetalert(page, logger)
    if alert_msg:
        return alert_msg

    try:
        await page.wait_for_selector(result_selector, state="attached", timeout=60000)
    except PlaywrightTimeoutError:
        logger.warning("Result table did not appear within 60s.")

    await page.wait_for_timeout(500)
    return None


async def save_html(page, html_path: Path) -> None:
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(await page.content(), encoding="utf-8")


async def next_button_enabled(page) -> bool:
    loc = page.locator(NEXT_BUTTON_SELECTOR)
    if await loc.count() == 0:
        return False

    try:
        if await loc.is_disabled():
            return False
    except Exception:
        pass

    cls = (await loc.get_attribute("class")) or ""
    disabled_attr = await loc.get_attribute("disabled")
    return disabled_attr is None and "disabled" not in cls.lower()


async def table_signature(page, result_selector: str = RESULT_TABLE_SELECTOR) -> str:
    if await page.locator(result_selector).count() == 0:
        return ""
    return await page.locator(result_selector).evaluate(
        """
        table => {
            const rows = Array.from(table.querySelectorAll('tbody tr'));
            const first = rows[0] ? rows[0].innerText : '';
            const last = rows[rows.length - 1] ? rows[rows.length - 1].innerText : '';
            return `${rows.length}::${first.slice(0, 200)}::${last.slice(0, 200)}`;
        }
        """
    )


async def click_next_page(
    page, logger: logging.Logger, result_selector: str = RESULT_TABLE_SELECTOR
) -> bool:
    """Click "Next 100" and wait for the table to change. Returns True if it
    changed (a new page loaded), False if it did not (we are at the last page)."""
    await dismiss_sweetalert(page, logger)

    old_sig = await table_signature(page, result_selector)

    await page.click(NEXT_BUTTON_SELECTOR, timeout=30000)

    try:
        await page.wait_for_load_state("commit", timeout=15000)
    except Exception:
        pass

    for _ in range(30):
        await page.wait_for_timeout(1000)

        await dismiss_sweetalert(page, logger)

        try:
            if await page.locator("#ddl_State").count() > 0:
                new_sig = await table_signature(page, result_selector)
                if new_sig and new_sig != old_sig:
                    return True
        except Exception:
            pass

    logger.warning("Table signature did not change after Next click; treating as last page.")
    return False


async def parse_current_table(
    page,
    result_selector: str = RESULT_TABLE_SELECTOR,
    layout: str = "legacy",
) -> dict[str, Any]:
    """
    Parse the current results table into {headers, rows}.

    Dispatches on the page layout:
      - "legacy" (2022-2023, #GVdataT): GP-anchor-first columns with stacked
        score/grade/band cells.
      - "flat"   (2023-2024, #GVdata):  State/District/Block/GP/Overall columns
        followed by per-theme (score, grade) column pairs.

    Both variants return the same row shape so scrape_block stays layout-agnostic:
        {"gp": {...}, "scores": [...], "wide": {...}}
    """
    if layout == "flat":
        return await parse_table_flat(page, result_selector)
    return await parse_table_legacy(page, result_selector)


async def parse_table_legacy(page, result_selector: str = RESULT_TABLE_SELECTOR) -> dict[str, Any]:
    """
    Parse the legacy 2022-2023 #GVdataT table into wide rows and long score rows.

    Important: this is a raw Python string so JS regex literals are not mangled.
    """
    if await page.locator(result_selector).count() == 0:
        return {"headers": [], "rows": []}

    return await page.locator(result_selector).evaluate(
        r"""
        table => {
            const clean = s => (s || '').replace(/\s+/g, ' ').trim();

            const slugify = s => clean(s).toLowerCase()
                .replace(/[^a-z0-9]+/g, '_')
                .replace(/^_+|_+$/g, '') || 'field';

            const parseGp = (cell) => {
                const a = cell.querySelector('a');
                const anchor = clean(a ? a.innerText : '');
                let gp_name = '';
                let gp_code = '';

                const m = anchor.match(/^(.*?)-\[(\d+)\]$/);
                if (m) {
                    gp_name = clean(m[1]);
                    gp_code = m[2];
                } else {
                    gp_name = anchor;
                }

                let scorecard_url = '';
                const onclick = a ? (a.getAttribute('onclick') || '') : '';
                const u = onclick.match(/['"]([^'"]*SC\.aspx[^'"]*)['"]/i);
                if (u) scorecard_url = u[1].replace(/&amp;/g, '&');

                return {
                    gp_name,
                    gp_code,
                    scorecard_url,
                    details_raw: clean(cell.innerText)
                };
            };

            const parseScore = (cell) => {
                const raw = (cell.innerText || '').trim();
                const lines = raw.split(/\n+/).map(clean).filter(Boolean);
                let score = '';
                let grade = '';
                let band = '';

                if (lines.length > 0) {
                    const sm = lines[0].match(/-?\d+(?:\.\d+)?/);
                    if (sm) score = sm[0];
                }
                if (lines.length > 1) grade = lines[1];
                if (lines.length > 2) band = lines.slice(2).join(' ');

                return {raw_value: clean(raw), score, grade, band};
            };

            const headerCells = Array.from(table.querySelectorAll('thead tr:last-child th'));
            const headers = headerCells.map(th => clean(th.innerText || th.textContent));

            const bodyRows = Array.from(table.querySelectorAll('tbody tr'));
            const rows = [];

            for (const tr of bodyRows) {
                const cells = Array.from(tr.querySelectorAll('td'));
                if (!cells.length) continue;

                const rowText = clean(tr.innerText);
                if (!rowText || /no data/i.test(rowText)) continue;

                const gp = parseGp(cells[0]);
                if (!gp.gp_name && !gp.gp_code) continue;

                const scores = [];
                for (let i = 1; i < cells.length; i++) {
                    const header = headers[i] || `score_${i}`;
                    scores.push({
                        theme_order: i,
                        theme_header: header,
                        theme_slug: slugify(header),
                        ...parseScore(cells[i])
                    });
                }

                const wide = {
                    gp_name: gp.gp_name,
                    gp_code: gp.gp_code,
                    scorecard_url: gp.scorecard_url,
                    details_raw: gp.details_raw
                };

                for (const sc of scores) {
                    wide[`${sc.theme_slug}_score`] = sc.score;
                    wide[`${sc.theme_slug}_grade`] = sc.grade;
                    wide[`${sc.theme_slug}_band`] = sc.band;
                    wide[`${sc.theme_slug}_raw`] = sc.raw_value;
                }

                rows.push({gp, scores, wide});
            }

            return {headers, rows};
        }
        """
    )


async def parse_table_flat(page, result_selector: str = "#GVdata") -> dict[str, Any]:
    """
    Parse the 2023-2024 PAI 2.0 #GVdata table.

    Layout (one row per GP):
        State Name | District Name | Block Name | GP Name | Overall PAI Score |
        <Theme> | <Theme>-Grade | <Theme> | <Theme>-Grade | ...

    Theme columns come in (score, grade) pairs; grade columns have a header
    ending in "-Grade". Overall PAI Score is emitted as theme_order 0.

    Returns the same row shape as the legacy parser so scrape_block is unchanged:
        {"gp": {...}, "scores": [...], "wide": {...}}
    """
    if await page.locator(result_selector).count() == 0:
        return {"headers": [], "rows": []}

    return await page.locator(result_selector).evaluate(
        r"""
        table => {
            const clean = s => (s || '').replace(/\s+/g, ' ').trim();

            const slugify = s => clean(s).toLowerCase()
                .replace(/[^a-z0-9]+/g, '_')
                .replace(/^_+|_+$/g, '') || 'field';

            const allRows = Array.from(table.querySelectorAll('tr'));
            const headerRow = allRows.find(r => r.querySelector('th'));
            const headers = headerRow
                ? Array.from(headerRow.querySelectorAll('th')).map(
                      th => clean(th.innerText || th.textContent))
                : [];

            const lower = headers.map(h => h.toLowerCase());
            const gpIdx = lower.findIndex(h => h === 'gp name');
            const overallIdx = lower.findIndex(h => h.includes('overall'));

            // Pair each theme score column with its trailing "-Grade" column.
            const startIdx = overallIdx >= 0 ? overallIdx + 1 : (gpIdx >= 0 ? gpIdx + 2 : 4);
            const themeCols = [];
            for (let i = startIdx; i < headers.length; i++) {
                if (/-\s*grade\s*$/i.test(headers[i])) {
                    if (themeCols.length) themeCols[themeCols.length - 1].gradeIdx = i;
                } else {
                    themeCols.push({header: headers[i], scoreIdx: i, gradeIdx: -1});
                }
            }

            const dataRows = allRows.filter(r => r.querySelector('td'));
            const rows = [];

            for (const tr of dataRows) {
                const cells = Array.from(tr.querySelectorAll('td'));
                if (!cells.length) continue;

                const text = cells.map(c => clean(c.innerText));
                const rowText = clean(tr.innerText);
                if (!rowText || /no data/i.test(rowText)) continue;

                const gp_name = gpIdx >= 0 ? (text[gpIdx] || '') : '';
                if (!gp_name) continue;

                let scorecard_url = '';
                const a = gpIdx >= 0 && cells[gpIdx] ? cells[gpIdx].querySelector('a') : null;
                if (a) {
                    const onclick = a.getAttribute('onclick') || '';
                    const u = onclick.match(/['"]([^'"]*SC\.aspx[^'"]*)['"]/i);
                    if (u) scorecard_url = u[1].replace(/&amp;/g, '&');
                    else if (a.getAttribute('href'))
                        scorecard_url = (a.getAttribute('href') || '').replace(/&amp;/g, '&');
                }

                const overall = overallIdx >= 0 ? (text[overallIdx] || '') : '';

                const scores = [];
                if (overallIdx >= 0) {
                    scores.push({
                        theme_order: 0,
                        theme_header: headers[overallIdx],
                        theme_slug: 'overall_pai_score',
                        score: overall,
                        grade: '',
                        band: '',
                        raw_value: overall
                    });
                }
                themeCols.forEach((tc, k) => {
                    const score = text[tc.scoreIdx] || '';
                    const grade = tc.gradeIdx >= 0 ? (text[tc.gradeIdx] || '') : '';
                    scores.push({
                        theme_order: k + 1,
                        theme_header: tc.header,
                        theme_slug: slugify(tc.header),
                        score: score,
                        grade: grade,
                        band: '',
                        raw_value: clean(score + (grade ? ' ' + grade : ''))
                    });
                });

                const gp = {
                    gp_name: gp_name,
                    gp_code: '',
                    scorecard_url: scorecard_url,
                    details_raw: gp_name,
                };

                const wide = {...gp};
                for (const sc of scores) {
                    wide[`${sc.theme_slug}_score`] = sc.score;
                    wide[`${sc.theme_slug}_grade`] = sc.grade;
                    wide[`${sc.theme_slug}_band`] = sc.band;
                    wide[`${sc.theme_slug}_raw`] = sc.raw_value;
                }

                rows.push({gp, scores, wide});
            }

            return {headers, rows};
        }
        """
    )


def build_block_dir(
    out_dir: Path, year: str, state: dict[str, str], district: dict[str, str], block: dict[str, str]
) -> Path:
    return (
        out_dir
        / year
        / option_label_with_code(state, "state")
        / option_label_with_code(district, "district")
        / option_label_with_code(block, "block")
    )


def done_status(block_dir: Path) -> dict[str, Any] | None:
    done = block_dir / "DONE.json"
    if done.exists():
        try:
            return read_json(done)
        except Exception:
            return {"status": "done_unreadable_json"}
    return None


def context_obj(
    run_id: str,
    year: str,
    page_info: dict[str, str],
    state: dict[str, str],
    district: dict[str, str],
    block: dict[str, str],
    block_dir: Path,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "timestamp_utc": utc_now(),
        "year": year,
        "page_info": page_info,
        "state": {
            "text": clean_label(state["text"]),
            "value": state["value"],
            "raw_text": state["text"],
        },
        "district": {
            "text": clean_label(district["text"]),
            "value": district["value"],
            "raw_text": district["text"],
        },
        "block": {
            "text": clean_label(block["text"]),
            "value": block["value"],
            "raw_text": block["text"],
        },
        "block_dir": str(block_dir),
    }


def enrich_metadata_row(
    base: dict[str, Any],
    gp: dict[str, Any],
    block_page: int,
    block_dir: Path,
    data_wide_csv: Path,
    html_file: Path,
    source_url: str,
) -> dict[str, Any]:
    return {
        **base,
        "gp_name": gp.get("gp_name", ""),
        "gp_code": gp.get("gp_code", ""),
        "scorecard_url": gp.get("scorecard_url", ""),
        "details_raw": gp.get("details_raw", ""),
        "block_page": block_page,
        "block_dir": str(block_dir),
        "block_data_wide_csv": str(data_wide_csv),
        "block_html_file": str(html_file),
        "source_url": source_url,
    }


async def scrape_block(
    page,
    year: str,
    page_info: dict[str, str],
    state: dict[str, str],
    district: dict[str, str],
    block: dict[str, str],
    out_dir: Path,
    run_id: str,
    args,
    logger: logging.Logger,
) -> dict[str, Any]:
    block_dir = build_block_dir(out_dir, year, state, district, block)
    html_dir = block_dir / "html"
    data_wide_csv = block_dir / "data_wide.csv"
    metadata_csv = block_dir / "metadata.csv"
    scores_long_csv = block_dir / "scores_long.csv"
    done_json = block_dir / "DONE.json"
    failed_json = block_dir / "FAILED.json"

    conf = YEAR_CONFIGS[year]
    result_selector = conf["result_table"]
    layout = conf["layout"]

    prior = done_status(block_dir)
    if prior and not args.overwrite:
        prior_status = prior.get("status", "")
        prior_rows = int(prior.get("gp_rows", 0) or 0)
        decision = skip_decision(
            prior_status,
            prior_rows,
            retry_no_data=args.retry_no_data,
            retry_empty=args.retry_empty,
        )
        if decision == "no_data":
            logger.info("Skipping block (no data available): %s", block_dir)
            return {
                "status": "skipped_no_data",
                "block_dir": str(block_dir),
                "data_wide_csv": str(data_wide_csv),
                "metadata_csv": str(metadata_csv),
                "scores_long_csv": str(scores_long_csv),
                "html_pages": 0,
                "gp_rows": 0,
                "score_rows": 0,
                "error": "",
            }
        if decision == "done":
            logger.info("Skipping done block: %s", block_dir)
            return {
                "status": "skipped_done",
                "block_dir": str(block_dir),
                "data_wide_csv": str(data_wide_csv),
                "metadata_csv": str(metadata_csv),
                "scores_long_csv": str(scores_long_csv),
                "html_pages": prior.get("html_pages", 0),
                "gp_rows": prior.get("gp_rows", 0),
                "score_rows": prior.get("score_rows", 0),
                "error": "",
            }

    block_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        block_dir / "context.json",
        context_obj(run_id, year, page_info, state, district, block, block_dir),
    )

    await ensure_state_district(page, state, district, logger)
    await select_value(page, "#ddl_Block", block["value"], wait_ms=700)
    alert_msg = await click_search(page, logger, result_selector)

    where = (
        f"{clean_label(state['text'])} / {clean_label(district['text'])} / "
        f"{clean_label(block['text'])}"
    )

    if alert_msg and "not available" in alert_msg.lower():
        # The PAI server returns a byte-identical "Details are not available" alert both
        # for genuinely empty blocks AND (spuriously) under load, so a single response
        # cannot distinguish the two. Confirm by re-searching: keep "no data" only if it
        # reproduces; recover if data appears; raise (retry) if the recheck is
        # indeterminate. A no-data classification must never be a transient false negative.
        confirmations = 1
        for attempt in range(1, args.no_data_confirm + 1):
            await select_value(page, "#ddl_Block", block["value"], wait_ms=700)
            recheck = await click_search(page, logger, result_selector)
            if recheck and "not available" in recheck.lower():
                confirmations += 1
                logger.info(
                    "[%s] %s: 'not available' reconfirmed (%s/%s)",
                    year,
                    where,
                    attempt,
                    args.no_data_confirm,
                )
                continue
            if await page.locator(result_selector).count() > 0:
                logger.warning(
                    "[%s] %s: 'not available' did NOT reproduce (recheck %s) -> data "
                    "present, recovering",
                    year,
                    where,
                    attempt,
                )
                alert_msg = None  # treat as data; fall through to parse below
                break
            raise RuntimeError(f"Indeterminate no-data recheck for {where} (server stress); retry")

        if alert_msg:  # still "not available" after all confirmations -> genuine
            logger.info("[%s] %s: No data available (confirmed x%s)", year, where, confirmations)
            done_obj = {
                "status": "done_no_data_available",
                "timestamp_utc": utc_now(),
                "run_id": run_id,
                "year": year,
                "state": clean_label(state["text"]),
                "state_value": state["value"],
                "district": clean_label(district["text"]),
                "district_value": district["value"],
                "block": clean_label(block["text"]),
                "block_value": block["value"],
                "block_dir": str(block_dir),
                "data_wide_csv": str(data_wide_csv),
                "metadata_csv": str(metadata_csv),
                "scores_long_csv": str(scores_long_csv),
                "html_pages": 0,
                "gp_rows": 0,
                "score_rows": 0,
                "server_message": alert_msg,
                "confirmations": confirmations,
            }
            write_json(done_json, done_obj)
            if failed_json.exists():
                failed_json.unlink()
            return done_obj

    # Guard against flaky-server false negatives. If the results table never
    # rendered (common when the PAI server stalls mid-stream) and the server
    # did not explicitly say "no data", treat this as a retryable failure
    # rather than silently writing an empty done_no_rows block that future
    # resumable runs would skip forever.
    if await page.locator(result_selector).count() == 0:
        raise RuntimeError(
            f"Result table {result_selector} did not load for "
            f"{clean_label(state['text'])} / {clean_label(district['text'])} / "
            f"{clean_label(block['text'])} (server slow/stalled); will retry"
        )

    all_wide_rows: list[dict[str, Any]] = []
    all_meta_rows: list[dict[str, Any]] = []
    all_score_rows: list[dict[str, Any]] = []

    base_meta = {
        "run_id": run_id,
        "timestamp_utc": utc_now(),
        "year": year,
        "state": clean_label(state["text"]),
        "state_value": state["value"],
        "district": clean_label(district["text"]),
        "district_value": district["value"],
        "block": clean_label(block["text"]),
        "block_value": block["value"],
    }

    page_no = 1
    prev_sig: tuple | None = None

    while True:
        html_path = html_dir / f"page_{page_no:03d}.html"
        await save_html(page, html_path)

        parsed = await parse_current_table(page, result_selector, layout)
        rows = parsed.get("rows", [])

        logger.info(
            "[%s] %s / %s / %s page %s: parsed %s GP rows",
            year,
            clean_label(state["text"]),
            clean_label(district["text"]),
            clean_label(block["text"]),
            page_no,
            len(rows),
        )

        cur_sig = tuple(item["gp"].get("gp_name", "") for item in rows)
        if page_no > 1 and cur_sig == prev_sig:
            # Same page as before -> we have paged past the end (the new-format
            # #btnNext never disables). Stop without re-appending duplicates.
            break
        prev_sig = cur_sig

        for item in rows:
            gp = item["gp"]
            meta = enrich_metadata_row(
                base=base_meta,
                gp=gp,
                block_page=page_no,
                block_dir=block_dir,
                data_wide_csv=data_wide_csv,
                html_file=html_path,
                source_url=page.url,
            )
            all_meta_rows.append(meta)

            wide = {**meta}
            wide.update(item.get("wide", {}))
            all_wide_rows.append(wide)

            for sc in item.get("scores", []):
                all_score_rows.append(
                    {
                        **meta,
                        "theme_order": sc.get("theme_order", ""),
                        "theme_header": sc.get("theme_header", ""),
                        "theme_slug": sc.get("theme_slug", ""),
                        "score": sc.get("score", ""),
                        "grade": sc.get("grade", ""),
                        "band": sc.get("band", ""),
                        "raw_value": sc.get("raw_value", ""),
                    }
                )

        if layout == "flat":
            # New page's #btnNext is never disabled; a short page (fewer than
            # 100 GPs) is the last page, so stop without an extra Next click.
            if len(rows) < FLAT_PAGE_SIZE:
                break
        else:
            if not await next_button_enabled(page):
                break

        page_no += 1
        if args.max_pages_per_block and page_no > args.max_pages_per_block:
            raise RuntimeError(f"Exceeded --max-pages-per-block={args.max_pages_per_block}")

        if not await click_next_page(page, logger, result_selector):
            break

    write_csv_rows(metadata_csv, all_meta_rows, GP_METADATA_FIELDS)
    write_csv_rows(scores_long_csv, all_score_rows, GP_SCORE_FIELDS)
    write_csv_rows(data_wide_csv, all_wide_rows)

    status = "done" if all_meta_rows else "done_no_rows"
    done_obj = {
        "status": status,
        "timestamp_utc": utc_now(),
        "run_id": run_id,
        "year": year,
        "state": clean_label(state["text"]),
        "state_value": state["value"],
        "district": clean_label(district["text"]),
        "district_value": district["value"],
        "block": clean_label(block["text"]),
        "block_value": block["value"],
        "block_dir": str(block_dir),
        "data_wide_csv": str(data_wide_csv),
        "metadata_csv": str(metadata_csv),
        "scores_long_csv": str(scores_long_csv),
        "html_pages": page_no,
        "gp_rows": len(all_meta_rows),
        "score_rows": len(all_score_rows),
    }
    write_json(done_json, done_obj)

    if failed_json.exists():
        failed_json.unlink()

    return done_obj


def filter_options(
    options: list[dict[str, str]],
    contains: str | None,
    values: list[str] | None,
) -> list[dict[str, str]]:
    out = options

    if contains:
        low = contains.lower()
        out = [o for o in out if low in o["text"].lower()]

    if values:
        keep = set(values)
        out = [o for o in out if o["value"] in keep]

    return out


async def reload_to_state_district(
    page,
    year: str,
    state: dict[str, str],
    district: dict[str, str] | None,
    args,
    logger: logging.Logger,
) -> dict[str, str] | None:
    page_info = await load_year_page(page, year, logger, args.allow_year_mismatch)
    if not page_info:
        return None

    await select_value(page, "#ddl_State", state["value"], wait_ms=1200)
    try:
        await get_options(page, "#ddl_District", timeout=60)
    except Exception as e:
        logger.error("[%s] Failed to load districts for %s: %s", year, state["text"], e)
        return None

    if district is not None:
        await select_value(page, "#ddl_District", district["value"], wait_ms=1200)
        try:
            await get_options(page, "#ddl_Block", timeout=60)
        except Exception as e:
            logger.error(
                "[%s] Failed to load blocks for %s / %s: %s",
                year,
                state["text"],
                district["text"],
                e,
            )
            return None

    return page_info


async def scrape_year(
    page,
    year: str,
    out_dir: Path,
    run_id: str,
    args,
    logger: logging.Logger,
) -> None:
    block_manifest_path = out_dir / "block_manifest.csv"
    dropdown_inventory_path = out_dir / "dropdown_inventory.csv"

    page_info = await load_year_page(page, year, logger, args.allow_year_mismatch)
    if not page_info:
        append_csv_rows(
            block_manifest_path,
            [
                {
                    "run_id": run_id,
                    "timestamp_utc": utc_now(),
                    "year": year,
                    "status": "year_page_load_failed",
                    "state": "",
                    "state_value": "",
                    "district": "",
                    "district_value": "",
                    "block": "",
                    "block_value": "",
                    "block_dir": "",
                    "data_wide_csv": "",
                    "metadata_csv": "",
                    "scores_long_csv": "",
                    "html_pages": "",
                    "gp_rows": "",
                    "score_rows": "",
                    "page_url": page.url,
                    "page_title": await page.title(),
                    "page_heading": "",
                    "actual_fy_value": "",
                    "expected_fy_value": YEAR_CONFIGS[year]["expected_fy_value"],
                    "error": "Could not load year page/form controls",
                }
            ],
            BLOCK_MANIFEST_FIELDS,
        )
        return

    states = await get_options(page, "#ddl_State", timeout=60)
    states = filter_options(states, args.state_contains, args.state_values)
    if args.limit_states is not None:
        states = states[: args.limit_states]

    append_csv_rows(
        dropdown_inventory_path,
        [
            {
                "run_id": run_id,
                "timestamp_utc": utc_now(),
                "year": year,
                "level": "state",
                "state": "",
                "state_value": "",
                "district": "",
                "district_value": "",
                "option_text": o["text"],
                "option_value": o["value"],
            }
            for o in states
        ],
        DROPDOWN_INVENTORY_FIELDS,
    )

    logger.info("[%s] States to process: %s", year, len(states))

    states_pbar = tqdm(states, desc=f"[{year}] States", unit="state", position=0, leave=True)
    for state in states_pbar:
        states_pbar.set_postfix_str(clean_label(state["text"])[:30])

        page_info = await reload_to_state_district(page, year, state, None, args, logger)
        if not page_info:
            append_csv_rows(
                block_manifest_path,
                [
                    {
                        "run_id": run_id,
                        "timestamp_utc": utc_now(),
                        "year": year,
                        "status": "state_page_load_failed",
                        "state": clean_label(state["text"]),
                        "state_value": state["value"],
                        "district": "",
                        "district_value": "",
                        "block": "",
                        "block_value": "",
                        "block_dir": "",
                        "data_wide_csv": "",
                        "metadata_csv": "",
                        "scores_long_csv": "",
                        "html_pages": "",
                        "gp_rows": "",
                        "score_rows": "",
                        "page_url": page.url,
                        "page_title": await page.title(),
                        "page_heading": "",
                        "actual_fy_value": "",
                        "expected_fy_value": YEAR_CONFIGS[year]["expected_fy_value"],
                        "error": "Could not reload year page/form controls for state",
                    }
                ],
                BLOCK_MANIFEST_FIELDS,
            )
            continue

        try:
            districts = await get_options(page, "#ddl_District", timeout=60)
        except Exception as e:
            logger.error("[%s] Could not get districts for state %s: %s", year, state["text"], e)
            continue

        districts = filter_options(districts, args.district_contains, args.district_values)
        if args.limit_districts is not None:
            districts = districts[: args.limit_districts]

        append_csv_rows(
            dropdown_inventory_path,
            [
                {
                    "run_id": run_id,
                    "timestamp_utc": utc_now(),
                    "year": year,
                    "level": "district",
                    "state": clean_label(state["text"]),
                    "state_value": state["value"],
                    "district": "",
                    "district_value": "",
                    "option_text": o["text"],
                    "option_value": o["value"],
                }
                for o in districts
            ],
            DROPDOWN_INVENTORY_FIELDS,
        )

        logger.info("[%s] Districts in %s: %s", year, clean_label(state["text"]), len(districts))

        districts_pbar = tqdm(districts, desc="  Districts", unit="dist", position=1, leave=False)
        for district in districts_pbar:
            districts_pbar.set_postfix_str(clean_label(district["text"])[:25])

            # A failed block earlier in this state may have left page_info=None after
            # a failed reload; re-establish it before using it for the manifest.
            if page_info is None:
                page_info = await reload_to_state_district(
                    page, year, state, district, args, logger
                )
                if page_info is None:
                    logger.error(
                        "[%s] Could not reload page for %s; skipping remaining districts.",
                        year,
                        state["text"],
                    )
                    break

            try:
                await ensure_state_district(page, state, district, logger)
                blocks = await get_options(page, "#ddl_Block", timeout=60)
            except Exception as e:
                logger.error(
                    "[%s] Could not get blocks for %s / %s: %s",
                    year,
                    state["text"],
                    district["text"],
                    e,
                )
                continue

            blocks = filter_options(blocks, args.block_contains, args.block_values)
            if args.limit_blocks is not None:
                blocks = blocks[: args.limit_blocks]

            append_csv_rows(
                dropdown_inventory_path,
                [
                    {
                        "run_id": run_id,
                        "timestamp_utc": utc_now(),
                        "year": year,
                        "level": "block",
                        "state": clean_label(state["text"]),
                        "state_value": state["value"],
                        "district": clean_label(district["text"]),
                        "district_value": district["value"],
                        "option_text": o["text"],
                        "option_value": o["value"],
                    }
                    for o in blocks
                ],
                DROPDOWN_INVENTORY_FIELDS,
            )

            logger.info(
                "[%s] Blocks in %s / %s: %s",
                year,
                clean_label(state["text"]),
                clean_label(district["text"]),
                len(blocks),
            )

            blocks_pbar = tqdm(blocks, desc="    Blocks", unit="block", position=2, leave=False)
            for block in blocks_pbar:
                block_dir = build_block_dir(out_dir, year, state, district, block)
                blocks_pbar.set_postfix_str(clean_label(block["text"])[:20])

                # Invariant: page_info is non-None here — the district-level guard
                # reloads it, and any in-loop reload that fails breaks the loop.
                assert page_info is not None
                manifest_base = {
                    "run_id": run_id,
                    "timestamp_utc": utc_now(),
                    "year": year,
                    "state": clean_label(state["text"]),
                    "state_value": state["value"],
                    "district": clean_label(district["text"]),
                    "district_value": district["value"],
                    "block": clean_label(block["text"]),
                    "block_value": block["value"],
                    "block_dir": str(block_dir),
                    "page_url": page_info.get("page_url", ""),
                    "page_title": page_info.get("page_title", ""),
                    "page_heading": page_info.get("page_heading", ""),
                    "actual_fy_value": page_info.get("actual_fy_value", ""),
                    "expected_fy_value": page_info.get("expected_fy_value", ""),
                }

                success_or_skip = False

                for attempt in range(1, args.max_retries + 1):
                    try:
                        result = await scrape_block(
                            page=page,
                            year=year,
                            page_info=page_info,
                            state=state,
                            district=district,
                            block=block,
                            out_dir=out_dir,
                            run_id=run_id,
                            args=args,
                            logger=logger,
                        )

                        append_csv_rows(
                            block_manifest_path,
                            [
                                {
                                    **manifest_base,
                                    "timestamp_utc": utc_now(),
                                    "status": result.get("status", ""),
                                    "data_wide_csv": result.get("data_wide_csv", ""),
                                    "metadata_csv": result.get("metadata_csv", ""),
                                    "scores_long_csv": result.get("scores_long_csv", ""),
                                    "html_pages": result.get("html_pages", ""),
                                    "gp_rows": result.get("gp_rows", ""),
                                    "score_rows": result.get("score_rows", ""),
                                    "error": result.get("error", ""),
                                }
                            ],
                            BLOCK_MANIFEST_FIELDS,
                        )

                        success_or_skip = True
                        break

                    except Exception as e:
                        logger.error(
                            "[%s] Block failed attempt %s/%s: %s / %s / %s :: %s",
                            year,
                            attempt,
                            args.max_retries,
                            state["text"],
                            district["text"],
                            block["text"],
                            e,
                        )

                        failed_obj = {
                            **manifest_base,
                            "timestamp_utc": utc_now(),
                            "status": "failed",
                            "error": repr(e),
                            "traceback": traceback.format_exc(),
                        }
                        write_json(block_dir / "FAILED.json", failed_obj)

                        try:
                            debug_dir = block_dir / "debug"
                            debug_dir.mkdir(parents=True, exist_ok=True)
                            await save_html(page, debug_dir / f"failed_attempt_{attempt}.html")
                            await page.screenshot(
                                path=str(debug_dir / f"failed_attempt_{attempt}.png"),
                                full_page=True,
                            )
                        except Exception:
                            pass

                        if attempt < args.max_retries:
                            page_info = await reload_to_state_district(
                                page, year, state, district, args, logger
                            )
                            if not page_info:
                                break

                if not success_or_skip:
                    append_csv_rows(
                        block_manifest_path,
                        [
                            {
                                **manifest_base,
                                "timestamp_utc": utc_now(),
                                "status": "failed",
                                "data_wide_csv": "",
                                "metadata_csv": "",
                                "scores_long_csv": "",
                                "html_pages": "",
                                "gp_rows": "",
                                "score_rows": "",
                                "error": f"failed after {args.max_retries} attempts",
                            }
                        ],
                        BLOCK_MANIFEST_FIELDS,
                    )

                    if args.stop_on_error:
                        raise RuntimeError(f"Failed block after retries: {block['text']}")

                    page_info = await reload_to_state_district(
                        page, year, state, district, args, logger
                    )
                    if not page_info:
                        logger.error(
                            "[%s] Could not recover page after failed block; moving to next state.",
                            year,
                        )
                        break

                await page.wait_for_timeout(int(args.delay * 1000))


async def main_async(args) -> None:
    out_dir = Path(args.out)
    logger = setup_logger(out_dir)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    logger.info("=" * 90)
    logger.info("PAI resumable scrape run_id=%s", run_id)
    logger.info("Output directory=%s", out_dir.resolve())
    logger.info("Years=%s", args.years)
    logger.info("=" * 90)

    if args.reset_global_indexes:
        for p in [
            out_dir / "block_manifest.csv",
            out_dir / "dropdown_inventory.csv",
            out_dir / "gp_metadata.csv",
            out_dir / "gp_scores_long.csv",
        ]:
            if p.exists():
                logger.info("Removing existing global index: %s", p)
                p.unlink()

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": args.headless,
            "slow_mo": args.slow_mo,
        }
        if args.browser_channel:
            launch_kwargs["channel"] = args.browser_channel

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1600, "height": 1000},
            locale="en-US",
            timezone_id="Asia/Kolkata",
        )

        page = await context.new_page()

        if args.block_third_party:

            async def route_handler(route):
                req = route.request
                url = req.url.lower()
                if req.resource_type in BLOCKED_RESOURCE_TYPES or any(
                    s in url for s in BLOCKED_URL_SUBSTRINGS
                ):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", route_handler)

        for year in args.years:
            try:
                await scrape_year(page, year, out_dir, run_id, args, logger)
            except Exception as e:
                logger.error("Year-level scrape failed for %s: %s", year, e)
                logger.error(traceback.format_exc())
                append_csv_rows(
                    out_dir / "block_manifest.csv",
                    [
                        {
                            "run_id": run_id,
                            "timestamp_utc": utc_now(),
                            "year": year,
                            "status": "year_crashed",
                            "state": "",
                            "state_value": "",
                            "district": "",
                            "district_value": "",
                            "block": "",
                            "block_value": "",
                            "block_dir": "",
                            "data_wide_csv": "",
                            "metadata_csv": "",
                            "scores_long_csv": "",
                            "html_pages": "",
                            "gp_rows": "",
                            "score_rows": "",
                            "page_url": page.url,
                            "page_title": await page.title(),
                            "page_heading": "",
                            "actual_fy_value": "",
                            "expected_fy_value": YEAR_CONFIGS[year]["expected_fy_value"],
                            "error": repr(e),
                        }
                    ],
                    BLOCK_MANIFEST_FIELDS,
                )
                if args.stop_on_error:
                    raise

        await context.close()
        await browser.close()

    logger.info("Done.")
    logger.info("Manifest: %s", (out_dir / "block_manifest.csv").resolve())
    logger.info("Rebuild global gp_metadata/gp_scores_long with: scripts/pai_rebuild_index.py")


def comma_values(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [x.strip() for x in value.split(",") if x.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--years", nargs="+", choices=list(YEAR_CONFIGS.keys()), default=list(YEAR_CONFIGS.keys())
    )
    parser.add_argument("--out", default="data")

    parser.add_argument("--state-contains", default=None)
    parser.add_argument("--district-contains", default=None)
    parser.add_argument("--block-contains", default=None)

    parser.add_argument(
        "--state-values",
        type=comma_values,
        default=None,
        help="Comma-separated state option values, e.g. 10,9",
    )
    parser.add_argument(
        "--district-values",
        type=comma_values,
        default=None,
        help="Comma-separated district option values",
    )
    parser.add_argument(
        "--block-values",
        type=comma_values,
        default=None,
        help="Comma-separated block option values",
    )

    parser.add_argument("--limit-states", type=int, default=None)
    parser.add_argument("--limit-districts", type=int, default=None)
    parser.add_argument("--limit-blocks", type=int, default=None)
    parser.add_argument("--max-pages-per-block", type=int, default=1000)

    parser.add_argument(
        "--overwrite", action="store_true", help="Re-scrape blocks even when DONE.json exists."
    )
    parser.add_argument(
        "--retry-empty", action="store_true", help="Re-scrape DONE blocks that have zero GP rows."
    )
    parser.add_argument(
        "--retry-no-data",
        action="store_true",
        help="Re-scrape blocks previously marked done_no_data_available (re-verify no-data).",
    )
    parser.add_argument(
        "--no-data-confirm",
        type=int,
        default=2,
        help="Times a 'not available' must reproduce on re-search before it is accepted as "
        "genuine no-data (0 = trust the first response; default 2).",
    )
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--stop-on-error", action="store_true")

    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--browser-channel", default=None, help="Optional, e.g. chrome")
    parser.add_argument("--slow-mo", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--block-third-party", action="store_true")
    parser.add_argument("--allow-year-mismatch", action="store_true")

    parser.add_argument("--reset-global-indexes", action="store_true")

    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))

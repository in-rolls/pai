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
import csv
import hashlib
import json
import logging
import os
import re
import sys
import traceback
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pai_common import (  # noqa: E402
    BLOCK_MANIFEST_FIELDS,
    BLOCK_TABLES,
    DROPDOWN_INVENTORY_FIELDS,
    FLAT_PAGE_SIZE,
    OFFICIAL_FINAL_GP_COUNTS,
    OFFICIAL_FINAL_GP_COUNTS_SOURCE,
    OVERALL_SLUG,
    YEAR_CONFIGS,
    append_csv_rows,
    read_csv,
    read_json,
    write_json,
)
from pai_contracts import (  # noqa: E402
    canonicalize_parsed_themes,
    canonicalize_score_gp_codes,
    load_score_value_exceptions,
    validate_block_rows,
    write_block_tables,
)
from pai_stores import BlockStore  # noqa: E402

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
# The GridView page index survives the Search postback, so a retrieval that
# follows a paginated one opens on that later page and silently drops page 1;
# every retrieval therefore rewinds with #btnPrev until it is disabled.
NEXT_BUTTON_SELECTOR = "#btnNext"
PREV_BUTTON_SELECTOR = "#btnPrev"
MAX_REWIND_CLICKS = 200
MAX_RERENDER_ATTEMPTS = 3
PAGER_RENDER_POLLS = 30
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

BLOCK_COUNT_AUDIT_FIELDS = [
    "run_id",
    "timestamp_utc",
    "year",
    "state",
    "state_value",
    "district",
    "district_value",
    "block",
    "block_value",
    "retrieval",
    "baseline_gp_rows",
    "universe_gp_rows",
    "live_gp_rows",
    "delta_from_baseline",
    "missing_universe_codes",
    "unexpected_score_codes",
    "status",
    "exception_evidence",
]

GP_UNIVERSE_URL = "https://pai.gov.in/Handlers/Y_GPs_By_LGD_Block.ashx"


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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


def manifest_header_current(path: Path) -> bool:
    """Appending rows in the current field order to an older header would misalign columns."""
    if not path.exists() or path.stat().st_size == 0:
        return True
    with FileLock(f"{path}.lock"), path.open(newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle), [])
    return header == BLOCK_MANIFEST_FIELDS


def archived_years(out_dir: Path, years: list[str]) -> list[str]:
    """Years whose block tree exists only as a compact archive.

    Scraping such a year would start a fresh live tree that BlockStore then
    prefers over the complete archive, hiding every prior block from rebuilds.
    """
    store = BlockStore(out_dir)
    return [year for year in years if store.mode(year) == "archive"]


def required_confirmations(prior_status: str, complete_confirm: int, no_data_confirm: int) -> int:
    """Confirmations a finished block must carry to be trusted on resume.

    A no-data block was confirmed by re-searching ``--no-data-confirm`` times; a
    data block by ``--complete-confirm`` repeat retrievals. Judging one by the
    other's threshold re-fetches it on every run without ever catching up.
    """
    if prior_status == "done_no_data_available":
        return 1 + no_data_confirm
    return 1 + complete_confirm


def skip_decision(
    prior_status: str,
    prior_rows: int,
    *,
    retry_no_data: bool,
    retry_empty: bool,
    prior_reverified: bool = False,
) -> str | None:
    """Decide whether a previously-finished block should be skipped on a resumable run.

    Returns "no_data" / "done" to skip, or None to re-scrape it. `--retry-no-data`
    re-verifies no-data blocks, but only those NOT already re-verified this recovery
    (`prior_reverified` = the DONE.json carries a `confirmations` stamp) — so repeated
    passes chase only the un-verified gaps and never re-do genuinely-empty blocks again.
    `--retry-empty` re-does zero-row blocks.
    """
    if prior_status == "done_no_data_available":
        if retry_no_data and not prior_reverified:
            return None
        return "no_data"
    if prior_rows > 0 or not retry_empty:
        return "done"
    return None


def block_key(
    year: str, state_value: str, district_value: str, block_value: str
) -> tuple[str, str, str, str]:
    return year, state_value, district_value, block_value


def load_baseline_gp_counts(data_dir: Path, years: list[str]) -> dict[tuple[str, ...], int]:
    """Load prior block counts for diagnostics, never as a completeness gate."""
    store = BlockStore(data_dir)
    counts: dict[tuple[str, ...], int] = {}
    for year in years:
        for block in store.iter_blocks(year, names={"DONE.json"}):
            status = block.json("DONE.json")
            if not status or status.get("status") != "done":
                continue
            key = block_key(
                year,
                str(status.get("state_value", "")),
                str(status.get("district_value", "")),
                str(status.get("block_value", "")),
            )
            if all(key[1:]):
                counts[key] = int(status.get("gp_rows", 0) or 0)
    return counts


def load_universe_exceptions(path: Path | None) -> dict[tuple[str, ...], dict[str, Any]]:
    """Load reviewed score/universe differences; every exception must carry evidence."""
    if path is None:
        return {}
    exceptions: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in read_csv(path):
        evidence = row.get("evidence", "").strip()
        if not evidence:
            raise ValueError(f"{path}: every block-count exception requires evidence")
        key = block_key(
            row.get("year", ""),
            row.get("state_value", ""),
            row.get("district_value", ""),
            row.get("block_value", ""),
        )
        exceptions[key] = {
            "allowed_missing_gp_codes": {
                code.strip()
                for code in row.get("allowed_missing_gp_codes", "").split(";")
                if code.strip()
            },
            "evidence": evidence,
        }
    return exceptions


def load_gp_name_links(path: Path | None) -> dict[tuple[str, ...], dict[str, dict[str, str]]]:
    """Load reviewed block-local score-name to universe-code links."""
    if path is None:
        return {}
    links: dict[tuple[str, ...], dict[str, dict[str, str]]] = {}
    for row in read_csv(path):
        evidence = row.get("evidence", "").strip()
        if not evidence:
            raise ValueError(f"{path}: every reviewed GP-name link requires evidence")
        key = block_key(
            row.get("year", ""),
            row.get("state_value", ""),
            row.get("district_value", ""),
            row.get("block_value", ""),
        )
        name_key = normalize_gp_name(row.get("score_gp_name", ""))
        gp_code = row.get("gp_code", "").strip()
        if not name_key or not gp_code:
            raise ValueError(f"{path}: score_gp_name and gp_code are required")
        links.setdefault(key, {})[name_key] = {"gp_code": gp_code, "evidence": evidence}
    return links


def normalize_gp_name(value: str) -> str:
    """Conservative Unicode/whitespace normalization for block-local exact links."""
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def universe_rows(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """(code, raw name) pairs from a GP handler payload, minus the exact ``[null, null]`` sentinel.

    The portal appends that sentinel to some hierarchy responses; any other null
    is a malformed row and fails, as in the standalone collector.
    """
    rows: list[tuple[str, str]] = []
    for row in payload.get("rows", []):
        if row == [None, None]:
            continue
        if row is None or len(row) != 2 or row[0] is None or row[1] is None:
            raise RuntimeError(f"GP-universe handler returned a malformed row: {row!r}")
        rows.append((str(row[0]), str(row[1])))
    return rows


def universe_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Validated {gp_code: name} for a GP handler payload; sentinel-free, no duplicates."""
    if payload.get("columns") != ["gp_code", "nm"] or not isinstance(payload.get("rows"), list):
        raise RuntimeError(f"Unexpected GP-universe response schema: {payload!r}")
    if any(not isinstance(row, list) or len(row) != 2 for row in payload["rows"]):
        raise RuntimeError("GP-universe handler returned malformed rows")
    rows = universe_rows(payload)
    universe = {code: clean_universe_gp_name(code, name) for code, name in rows}
    if len(universe) != len(rows):
        raise RuntimeError("GP-universe handler returned duplicate LGD codes")
    return universe


def clean_universe_gp_name(gp_code: str, raw_name: str) -> str:
    """Remove the handler's trailing ``[LGD code]`` after checking it.

    Only the numeric code is removed: suffixes such as ``Paroo [N]`` / ``Paroo [S]``
    are part of the official name and distinguish same-name GPs in a block.
    """
    match = re.match(r"^(.*?)\s*\[(\d+)\]\s*$", raw_name or "")
    if not match:
        return (raw_name or "").strip()
    if match.group(2) != gp_code:
        raise AssertionError(
            f"GP-universe code disagrees with name suffix: {gp_code} != {match.group(2)}"
        )
    return match.group(1).strip()


def link_missing_gp_codes(
    metadata: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    wide: list[dict[str, Any]],
    universe: dict[str, str],
    reviewed_links: dict[str, dict[str, str]] | None = None,
) -> None:
    """Fill absent score LGD codes by block-local name, never by row order."""
    reviewed_links = reviewed_links or {}
    codes_by_name: dict[str, list[str]] = {}
    for code, name in universe.items():
        codes_by_name.setdefault(normalize_gp_name(name), []).append(code)

    def resolve(row: dict[str, Any]) -> str:
        name = normalize_gp_name(str(row.get("gp_name", "")))
        existing = str(row.get("gp_code", "")).strip()
        if existing in universe:
            # The code (decoded from the scorecard link) is the identity; the
            # portal's display name may be misspelled relative to the handler's.
            return existing
        reviewed = reviewed_links.get(name)
        if reviewed:
            code = reviewed["gp_code"]
            if code not in universe:
                raise AssertionError(f"reviewed GP code {code} is absent from block universe")
            return code
        candidates = codes_by_name.get(name, [])
        if len(candidates) != 1:
            raise AssertionError(
                f"GP name {row.get('gp_name')!r} has {len(candidates)} exact universe "
                "matches; add a reviewed name link"
            )
        return candidates[0]

    # Resolved per row, not per name: a block may hold two GPs with the same displayed
    # name that the portal distinguishes only by their LGD codes.
    for rows in (metadata, scores, wide):
        for row in rows:
            row["gp_code"] = resolve(row)


def officially_unvalidated_state(year: str, state: str) -> bool:
    """A state absent from a vintage's Ministry table was not validated for it."""
    controls = OFFICIAL_FINAL_GP_COUNTS.get(year)
    return bool(controls) and state not in controls


def officially_unscored_state_exception(
    year: str, state: str, universe: dict[str, str]
) -> dict[str, Any] | None:
    """Allow a whole-state "no data" only where the Ministry's final table omits the state.

    West Bengal has GPs in the LGD hierarchy but no PAI 2.0 scores; the reviewed
    control table (with its PIB source) is the evidence, so no per-block ledger row is
    needed. Years without controls keep the strict per-block contract.
    """
    controls = OFFICIAL_FINAL_GP_COUNTS.get(year)
    if not controls or state in controls:
        return None
    return {
        "allowed_missing_gp_codes": set(universe),
        "evidence": (
            f"{state} is absent from the Ministry's final {year} PAI table "
            f"({OFFICIAL_FINAL_GP_COUNTS_SOURCE[year]}); the portal reports no scores for the state"
        ),
    }


def load_partially_scored_states(path: Path | None) -> set[tuple[str, str]]:
    """(year, state_value) pairs whose official scored total is below the hierarchy.

    Read from the universe collection manifest, which records both numbers per
    state. In such a state (Goa, Meghalaya) the portal scores a subset of each
    block's GPs, so the per-block contract is "subset of the universe" and the
    state total at rebuild time is the completeness check.
    """
    if path is None or not path.is_file() or path.stat().st_size == 0:
        return set()
    counts = read_json(path)["counts_by_state"]
    partial: set[tuple[str, str]] = set()
    for key, row in counts.items():
        published = int(row.get("published_scored_gp_count", -1))
        if 0 < published < int(row["hierarchy_gp_rows"]):
            year, state_value = key.split(":", 1)
            partial.add((year, state_value))
    return partial


def validate_gp_universe(
    score_codes: set[str],
    universe_codes: set[str],
    exception: dict[str, Any] | None = None,
    *,
    allow_subset: bool = False,
    no_data_confirmed: bool = False,
) -> dict[str, Any]:
    """Require score results to match the portal's block GP universe exactly.

    With ``allow_subset`` (a partially scored state) unscored universe GPs are
    permitted and reported; GPs outside the universe never are. An empty result
    is a subset only when the portal's "not available" alert confirmed it
    (``no_data_confirmed``); an empty table with no alert is a failed render.
    """
    missing = universe_codes - score_codes
    unexpected = score_codes - universe_codes
    allowed_missing = exception["allowed_missing_gp_codes"] if exception else set()

    if unexpected:
        raise AssertionError(
            "score result has GP codes absent from the official block universe: "
            + ";".join(sorted(unexpected))
        )
    if allow_subset and missing and missing != allowed_missing:
        if not score_codes and not no_data_confirmed:
            raise AssertionError("empty score result cannot be accepted as a universe subset")
        return {
            "status": "subset_in_partially_scored_state",
            "missing": missing,
            "unexpected": unexpected,
        }
    if missing != allowed_missing:
        raise AssertionError(
            "score result does not match the official block universe: "
            f"missing={';'.join(sorted(missing)) or '(none)'}; "
            f"reviewed_allowed={';'.join(sorted(allowed_missing)) or '(none)'}"
        )
    return {
        "status": "reviewed_exception" if missing else "exact_universe_match",
        "missing": missing,
        "unexpected": unexpected,
    }


def cached_universe_is_valid(block_dir: Path, done: dict[str, Any]) -> bool:
    """Only resume-skip a block whose cached handler source still validates."""
    raw_path = block_dir / "source" / "gp_universe.json"
    provenance_path = block_dir / "source" / "gp_universe_provenance.json"
    try:
        raw = raw_path.read_bytes()
        provenance = read_json(provenance_path)
        payload = json.loads(raw)
        rows = payload["rows"]
        return bool(
            payload.get("columns") == ["gp_code", "nm"]
            and isinstance(rows, list)
            and provenance.get("http_status") == 200
            and provenance.get("sha256") == hashlib.sha256(raw).hexdigest()
            and int(provenance.get("gp_rows", -1)) == len(universe_rows(payload))
            and int(done.get("universe_gp_rows", -1)) == len(universe_rows(payload))
        )
    except KeyError, OSError, TypeError, ValueError, json.JSONDecodeError:
        return False


async def fetch_gp_universe(
    page,
    *,
    year: str,
    state: dict[str, str],
    district: dict[str, str],
    block: dict[str, str],
    block_dir: Path,
) -> dict[str, str]:
    """Fetch and preserve the portal's official LGD GP universe for a block."""
    params = {
        "SID": state["value"],
        "ZID": district["value"],
        "BID": block["value"],
        "YID": YEAR_CONFIGS[year]["expected_fy_value"],
    }
    response = await page.request.get(GP_UNIVERSE_URL, params=params, timeout=120_000)
    if not response.ok:
        raise RuntimeError(f"GP-universe handler returned HTTP {response.status}")
    raw = await response.body()
    payload = json.loads(raw)
    universe = universe_from_payload(payload)
    codes = list(universe)

    source_dir = block_dir / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(source_dir / "gp_universe.json", raw)
    provenance = {
        "retrieved_utc": utc_now(),
        "year": year,
        "state": clean_label(state["text"]),
        "state_value": state["value"],
        "district": clean_label(district["text"]),
        "district_value": district["value"],
        "block": clean_label(block["text"]),
        "block_value": block["value"],
        "url": GP_UNIVERSE_URL,
        "params": params,
        "http_status": response.status,
        "gp_rows": len(codes),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    atomic_write_bytes(
        source_dir / "gp_universe_provenance.json",
        (json.dumps(provenance, ensure_ascii=False, indent=2) + "\n").encode(),
    )
    return universe


def result_signature(
    metadata: list[dict[str, Any]], scores: list[dict[str, Any]]
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    gp_codes = tuple(sorted(str(row.get("gp_code", "")) for row in metadata))
    score_cells = tuple(
        sorted(
            (
                str(row.get("gp_code", "")),
                str(row.get("theme_order", "")),
                str(row.get("theme_slug", "")),
                str(row.get("score", "")),
                str(row.get("grade", "")),
                str(row.get("band", "")),
            )
            for row in scores
        )
    )
    return gp_codes, score_cells


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


async def pager_button_enabled(page, selector: str) -> bool:
    loc = page.locator(selector)
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


async def click_pager_button(
    page,
    selector: str,
    logger: logging.Logger,
    result_selector: str = RESULT_TABLE_SELECTOR,
) -> bool:
    """Click a pager button and wait for the table to change.

    Returns True when another page rendered, False when the portal answered with
    its "not available" alert (no page in that direction: a block of exactly 100
    GPs has Next enabled on its only page). A click that renders nothing within
    the poll budget is a stalled server, not a last page, and raises so the
    block is retried rather than truncated.
    """
    await dismiss_sweetalert(page, logger)

    old_sig = await table_signature(page, result_selector)

    await page.click(selector, timeout=30000)

    try:
        await page.wait_for_load_state("commit", timeout=15000)
    except Exception:
        pass

    alert_seen = False
    for _ in range(PAGER_RENDER_POLLS):
        await page.wait_for_timeout(1000)

        alert_seen = bool(await dismiss_sweetalert(page, logger)) or alert_seen

        try:
            if await page.locator("#ddl_State").count() > 0:
                new_sig = await table_signature(page, result_selector)
                if new_sig and new_sig != old_sig:
                    return True
        except Exception:
            pass

    if alert_seen:
        logger.info("%s click answered by the no-data alert; treating as end.", selector)
        return False
    raise RuntimeError(
        f"results did not render within {PAGER_RENDER_POLLS}s after {selector} click"
    )


async def reset_page_index(
    page, logger: logging.Logger, result_selector: str = RESULT_TABLE_SELECTOR
) -> int:
    """Click Prev until it is disabled so the next postback starts from page 1.

    Needed before every Search as well as after one: a block of exactly 100 GPs
    leaves the index on an empty "page 2" (the server answers Next with the
    "not available" alert), and the following Search then also returns that alert.
    """
    clicks = 0
    while await pager_button_enabled(page, PREV_BUTTON_SELECTOR):
        if clicks >= MAX_REWIND_CLICKS:
            raise RuntimeError(f"Could not rewind results to page 1 in {clicks} clicks")
        if not await click_pager_button(page, PREV_BUTTON_SELECTOR, logger, result_selector):
            raise RuntimeError("Prev is enabled but the results table did not change")
        clicks += 1
    return clicks


async def rewind_to_first_page(
    page, logger: logging.Logger, result_selector: str = RESULT_TABLE_SELECTOR
) -> int:
    """Return to page 1 of a freshly retrieved result set. Returns the Prev clicks needed.

    The Prev postback renders score cells without their grade/band text, so after
    rewinding the page index the result is re-requested through Search, which renders
    page 1 in full now that the index is back at zero.
    """
    clicks = await reset_page_index(page, logger, result_selector)
    if clicks:
        alert = await click_search(page, logger, result_selector)
        if alert or await page.locator(result_selector).count() == 0:
            raise RuntimeError("Re-search after rewinding to page 1 returned no data/table")
        if await pager_button_enabled(page, PREV_BUTTON_SELECTOR):
            raise RuntimeError("Results did not stay on page 1 after re-search")
        logger.info("Rewound results table to page 1 (%s Prev clicks) and re-searched", clicks)
    return clicks


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
        parsed = await parse_table_flat(page, result_selector)
    else:
        parsed = await parse_table_legacy(page, result_selector)
    return canonicalize_parsed_themes(parsed)


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
        "block_html_file": str(html_file),
        "source_url": source_url,
    }


def rows_lack_grades(rows: list[dict[str, Any]]) -> bool:
    """True when a parsed page has theme scores but not one grade cell.

    The portal's render mode is server-global: for a second or so after any
    session clicks a pager button, every session's postback renders score cells
    as bare numbers. With several workers that collision is routine, so a
    grade-less page is re-requested rather than stored or compared.
    """
    theme_scores = [
        score
        for item in rows
        for score in item.get("scores", [])
        if score.get("theme_slug") != OVERALL_SLUG
    ]
    return bool(theme_scores) and all(not score.get("grade") for score in theme_scores)


def page_signature(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(item["gp"].get("gp_code") or item["gp"].get("gp_name", "") for item in rows)


async def parse_rendered_page(
    page,
    *,
    result_selector: str,
    layout: str,
    html_path: Path,
    logger: logging.Logger,
    where: str,
) -> dict[str, Any]:
    """Save and parse the current results page, re-rendering it if grades are missing."""
    await save_html(page, html_path)
    parsed = await parse_current_table(page, result_selector, layout)
    for attempt in range(1, MAX_RERENDER_ATTEMPTS + 1):
        if not rows_lack_grades(parsed.get("rows", [])):
            return parsed
        logger.warning("%s: page rendered without grades; re-rendering (%s)", where, attempt)
        before = page_signature(parsed["rows"])
        await page.wait_for_timeout(1500)
        alert = await click_search(page, logger, result_selector)
        if alert or await page.locator(result_selector).count() == 0:
            raise RuntimeError(f"{where}: re-render returned no data/table")
        await save_html(page, html_path)
        parsed = await parse_current_table(page, result_selector, layout)
        if page_signature(parsed.get("rows", [])) != before:
            raise RuntimeError(f"{where}: results changed while re-rendering; retry")
    if not rows_lack_grades(parsed.get("rows", [])):
        return parsed
    raise RuntimeError(f"{where}: page still rendered without grades; retry")


async def collect_result_pages(
    page,
    *,
    year: str,
    state: dict[str, str],
    district: dict[str, str],
    block: dict[str, str],
    base_meta: dict[str, Any],
    block_dir: Path,
    html_dir: Path,
    html_prefix: str,
    result_selector: str,
    layout: str,
    max_pages_per_block: int,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
    """Collect every result page for one retrieval of a selected block."""
    all_wide_rows: list[dict[str, Any]] = []
    all_meta_rows: list[dict[str, Any]] = []
    all_score_rows: list[dict[str, Any]] = []
    page_no = 1
    prev_sig: tuple | None = None
    await rewind_to_first_page(page, logger, result_selector)

    where = (
        f"{clean_label(state['text'])} / {clean_label(district['text'])} / "
        f"{clean_label(block['text'])}"
    )
    while True:
        html_path = html_dir / f"{html_prefix}page_{page_no:03d}.html"
        parsed = await parse_rendered_page(
            page,
            result_selector=result_selector,
            layout=layout,
            html_path=html_path,
            logger=logger,
            where=f"{where} {html_prefix}page {page_no}",
        )
        rows = parsed.get("rows", [])
        logger.info(
            "[%s] %s / %s / %s %spage %s: parsed %s GP rows",
            year,
            clean_label(state["text"]),
            clean_label(district["text"]),
            clean_label(block["text"]),
            "confirmation " if html_prefix else "",
            page_no,
            len(rows),
        )

        cur_sig = page_signature(rows)
        if page_no > 1 and cur_sig == prev_sig:
            break
        prev_sig = cur_sig

        for item in rows:
            gp = item["gp"]
            meta = enrich_metadata_row(
                base=base_meta,
                gp=gp,
                block_page=page_no,
                block_dir=block_dir,
                html_file=html_path,
                source_url=page.url,
            )
            all_meta_rows.append(meta)
            all_wide_rows.append({**meta, **item.get("wide", {})})
            for score in item.get("scores", []):
                all_score_rows.append(
                    {
                        **meta,
                        "theme_order": score.get("theme_order", ""),
                        "theme_header": score.get("theme_header", ""),
                        "theme_slug": score.get("theme_slug", ""),
                        "score": score.get("score", ""),
                        "grade": score.get("grade", ""),
                        "band": score.get("band", ""),
                        "raw_value": score.get("raw_value", ""),
                    }
                )

        if layout == "flat":
            if len(rows) < FLAT_PAGE_SIZE:
                break
        elif not await pager_button_enabled(page, NEXT_BUTTON_SELECTOR):
            break

        page_no += 1
        if max_pages_per_block and page_no > max_pages_per_block:
            raise RuntimeError(f"Exceeded --max-pages-per-block={max_pages_per_block}")
        if not await click_pager_button(page, NEXT_BUTTON_SELECTOR, logger, result_selector):
            break

    return all_meta_rows, all_score_rows, all_wide_rows, page_no


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
    table_paths = {kind: block_dir / name for kind, name in BLOCK_TABLES.items()}
    done_json = block_dir / "DONE.json"
    failed_json = block_dir / "FAILED.json"

    conf = YEAR_CONFIGS[year]
    result_selector = conf["result_table"]
    layout = conf["layout"]
    key = block_key(year, state["value"], district["value"], block["value"])
    baseline = args.baseline_gp_counts.get(key)
    exception = args.universe_exceptions.get(key)
    reviewed_name_links = args.gp_name_links.get(key)
    # The portal may display unvalidated scores for a state the Ministry excluded;
    # they are collected as displayed and kept out of the release by the package.
    allow_subset = (year, state["value"]) in args.partially_scored_states or (
        officially_unvalidated_state(year, clean_label(state["text"]))
    )
    universe: dict[str, str] = {}

    def audit_count(
        retrieval: int,
        live_codes: set[str],
        status: str,
        *,
        missing: set[str] | None = None,
        unexpected: set[str] | None = None,
    ) -> None:
        append_csv_rows(
            out_dir / "block_count_audit.csv",
            [
                {
                    "run_id": run_id,
                    "timestamp_utc": utc_now(),
                    "year": year,
                    "state": clean_label(state["text"]),
                    "state_value": state["value"],
                    "district": clean_label(district["text"]),
                    "district_value": district["value"],
                    "block": clean_label(block["text"]),
                    "block_value": block["value"],
                    "retrieval": retrieval,
                    "baseline_gp_rows": "" if baseline is None else baseline,
                    "universe_gp_rows": len(universe),
                    "live_gp_rows": len(live_codes),
                    "delta_from_baseline": "" if baseline is None else len(live_codes) - baseline,
                    "missing_universe_codes": ";".join(sorted(missing or set())),
                    "unexpected_score_codes": ";".join(sorted(unexpected or set())),
                    "status": status,
                    "exception_evidence": exception["evidence"] if exception else "",
                }
            ],
            BLOCK_COUNT_AUDIT_FIELDS,
        )

    prior = done_status(block_dir)
    if prior and not args.overwrite:
        prior_status = prior.get("status", "")
        prior_rows = int(prior.get("gp_rows", 0) or 0)
        required = required_confirmations(prior_status, args.complete_confirm, args.no_data_confirm)
        prior_confirmations = int(prior.get("retrieval_confirmations", 0) or 0)
        decision = None
        has_universe_contract = prior.get(
            "universe_contract_version"
        ) == 1 and cached_universe_is_valid(block_dir, prior)
        if prior_confirmations >= required and has_universe_contract:
            decision = skip_decision(
                prior_status,
                prior_rows,
                retry_no_data=args.retry_no_data,
                retry_empty=args.retry_empty,
                prior_reverified="confirmations" in prior,
            )
        else:
            logger.info(
                "Rechecking legacy/unconfirmed DONE block (%s/%s confirmations, "
                "universe contract=%s): %s",
                prior_confirmations,
                required,
                has_universe_contract,
                block_dir,
            )
        if decision == "no_data":
            logger.info("Skipping block (no data available): %s", block_dir)
            return {
                "status": "skipped_no_data",
                "block_dir": str(block_dir),
                "metadata_file": str(table_paths["metadata"]),
                "scores_file": str(table_paths["scores"]),
                "wide_file": str(table_paths["wide"]),
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
                "metadata_file": str(table_paths["metadata"]),
                "scores_file": str(table_paths["scores"]),
                "wide_file": str(table_paths["wide"]),
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
    universe = await fetch_gp_universe(
        page,
        year=year,
        state=state,
        district=district,
        block=block,
        block_dir=block_dir,
    )

    await ensure_state_district(page, state, district, logger)
    await select_value(page, "#ddl_Block", block["value"], wait_ms=700)
    await reset_page_index(page, logger, result_selector)
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
            await reset_page_index(page, logger, result_selector)
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
            no_data_exception = exception or officially_unscored_state_exception(
                year, clean_label(state["text"]), universe
            )
            try:
                universe_check = validate_gp_universe(
                    set(),
                    set(universe),
                    no_data_exception,
                    allow_subset=allow_subset,
                    no_data_confirmed=True,
                )
            except AssertionError:
                audit_count(
                    confirmations,
                    set(),
                    "universe_mismatch",
                    missing=set(universe),
                )
                raise
            audit_count(
                confirmations,
                set(),
                universe_check["status"],
                missing=universe_check["missing"],
                unexpected=universe_check["unexpected"],
            )
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
                "metadata_file": str(table_paths["metadata"]),
                "scores_file": str(table_paths["scores"]),
                "wide_file": str(table_paths["wide"]),
                "html_pages": 0,
                "gp_rows": 0,
                "score_rows": 0,
                "server_message": alert_msg,
                "confirmations": confirmations,
                "retrieval_confirmations": confirmations,
                "universe_contract_version": 1,
                "universe_gp_rows": len(universe),
                "universe_contract_status": universe_check["status"],
                "universe_exception_evidence": (
                    no_data_exception["evidence"] if no_data_exception else ""
                ),
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
    all_meta_rows, all_score_rows, all_wide_rows, page_no = await collect_result_pages(
        page,
        year=year,
        state=state,
        district=district,
        block=block,
        base_meta=base_meta,
        block_dir=block_dir,
        html_dir=html_dir,
        html_prefix="",
        result_selector=result_selector,
        layout=layout,
        max_pages_per_block=args.max_pages_per_block,
        logger=logger,
    )
    # A truncated display ID ("27" for 272000) is never a universe code; the full
    # LGD code sits in the scorecard URL, so decode it before any name linking.
    canonicalize_score_gp_codes(all_meta_rows, all_score_rows, all_wide_rows)
    link_missing_gp_codes(
        all_meta_rows,
        all_score_rows,
        all_wide_rows,
        universe,
        reviewed_name_links,
    )
    validate_block_rows(
        all_meta_rows,
        all_score_rows,
        all_wide_rows,
        allowed_null_scores=args.allowed_null_scores,
    )
    live_codes = {str(row["gp_code"]) for row in all_meta_rows}
    try:
        universe_check = validate_gp_universe(
            live_codes, set(universe), exception, allow_subset=allow_subset
        )
    except AssertionError:
        audit_count(
            1,
            live_codes,
            "universe_mismatch",
            missing=set(universe) - live_codes,
            unexpected=live_codes - set(universe),
        )
        raise
    audit_count(
        1,
        live_codes,
        universe_check["status"],
        missing=universe_check["missing"],
        unexpected=universe_check["unexpected"],
    )

    signature = result_signature(all_meta_rows, all_score_rows)
    for confirmation in range(1, args.complete_confirm + 1):
        await ensure_state_district(page, state, district, logger)
        await select_value(page, "#ddl_Block", block["value"], wait_ms=700)
        await reset_page_index(page, logger, result_selector)
        confirmation_alert = await click_search(page, logger, result_selector)
        if confirmation_alert or await page.locator(result_selector).count() == 0:
            audit_count(confirmation + 1, set(), "unstable_confirmation")
            raise RuntimeError(f"{where}: confirmation retrieval returned no data/table")
        confirm_meta, confirm_scores, confirm_wide, _ = await collect_result_pages(
            page,
            year=year,
            state=state,
            district=district,
            block=block,
            base_meta=base_meta,
            block_dir=block_dir,
            html_dir=html_dir,
            html_prefix=f"confirm_{confirmation:02d}_",
            result_selector=result_selector,
            layout=layout,
            max_pages_per_block=args.max_pages_per_block,
            logger=logger,
        )
        canonicalize_score_gp_codes(confirm_meta, confirm_scores, confirm_wide)
        link_missing_gp_codes(
            confirm_meta,
            confirm_scores,
            confirm_wide,
            universe,
            reviewed_name_links,
        )
        validate_block_rows(
            confirm_meta,
            confirm_scores,
            confirm_wide,
            allowed_null_scores=args.allowed_null_scores,
        )
        confirm_codes = {str(row["gp_code"]) for row in confirm_meta}
        try:
            confirm_universe = validate_gp_universe(
                confirm_codes, set(universe), exception, allow_subset=allow_subset
            )
        except AssertionError:
            audit_count(
                confirmation + 1,
                confirm_codes,
                "universe_mismatch",
                missing=set(universe) - confirm_codes,
                unexpected=confirm_codes - set(universe),
            )
            raise
        stable = result_signature(confirm_meta, confirm_scores) == signature
        audit_count(
            confirmation + 1,
            confirm_codes,
            "stable_confirmation" if stable else "unstable_confirmation",
            missing=confirm_universe["missing"],
            unexpected=confirm_universe["unexpected"],
        )
        if not stable:
            raise RuntimeError(
                f"{where}: repeated retrieval changed GP codes or score cells "
                f"({len(all_meta_rows)} vs {len(confirm_meta)} GPs); retry"
            )

    try:
        write_block_tables(block_dir, all_meta_rows, all_score_rows, all_wide_rows)
    except AssertionError as exc:
        raise AssertionError(f"{where}: typed block table rejected: {exc}") from exc

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
        "metadata_file": str(table_paths["metadata"]),
        "scores_file": str(table_paths["scores"]),
        "wide_file": str(table_paths["wide"]),
        "html_pages": page_no,
        "gp_rows": len(all_meta_rows),
        "score_rows": len(all_score_rows),
        "retrieval_confirmations": 1 + args.complete_confirm,
        "baseline_gp_rows": baseline,
        "universe_contract_version": 1,
        "universe_gp_rows": len(universe),
        "universe_contract_status": universe_check["status"],
        "universe_exception_evidence": exception["evidence"] if exception else "",
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
                    "metadata_file": "",
                    "scores_file": "",
                    "wide_file": "",
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
                        "metadata_file": "",
                        "scores_file": "",
                        "wide_file": "",
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
                server_touched = False

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
                                    "metadata_file": result.get("metadata_file", ""),
                                    "scores_file": result.get("scores_file", ""),
                                    "wide_file": result.get("wide_file", ""),
                                    "html_pages": result.get("html_pages", ""),
                                    "gp_rows": result.get("gp_rows", ""),
                                    "score_rows": result.get("score_rows", ""),
                                    "error": result.get("error", ""),
                                }
                            ],
                            BLOCK_MANIFEST_FIELDS,
                        )

                        success_or_skip = True
                        server_touched = not str(result.get("status", "")).startswith("skipped")
                        break

                    except Exception as e:
                        server_touched = True
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
                                "metadata_file": "",
                                "scores_file": "",
                                "wide_file": "",
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

                if server_touched:
                    await page.wait_for_timeout(int(args.delay * 1000))


async def main_async(args) -> None:
    out_dir = Path(args.out)
    logger = setup_logger(out_dir)
    manifest_path = out_dir / "block_manifest.csv"
    if not manifest_header_current(manifest_path):
        raise SystemExit(
            f"{manifest_path} has a header from an older schema; run "
            "scripts/pai_migrate_block_tables.py before scraping"
        )
    archived = archived_years(out_dir, args.years)
    if archived:
        raise SystemExit(
            f"{archived} exist only as compact archives under {out_dir}; "
            "run `make expand YEAR=<year>` before resuming a scrape"
        )
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    args.baseline_gp_counts = (
        load_baseline_gp_counts(Path(args.baseline_counts_from), args.years)
        if args.baseline_counts_from
        else {}
    )
    args.universe_exceptions = load_universe_exceptions(
        Path(args.universe_exceptions) if args.universe_exceptions else None
    )
    args.gp_name_links = load_gp_name_links(
        Path(args.gp_name_links) if args.gp_name_links else None
    )
    args.allowed_null_scores = set(load_score_value_exceptions())
    args.partially_scored_states = load_partially_scored_states(
        Path(args.hierarchy_manifest) if args.hierarchy_manifest else None
    )

    logger.info("=" * 90)
    logger.info("PAI resumable scrape run_id=%s", run_id)
    logger.info("argv=%s", " ".join(sys.argv[1:]))
    logger.info("Output directory=%s", out_dir.resolve())
    logger.info("Years=%s", args.years)
    logger.info("Prior-count diagnostics loaded=%s", len(args.baseline_gp_counts))
    logger.info("Reviewed universe exceptions loaded=%s", len(args.universe_exceptions))
    logger.info("Reviewed GP-name link blocks loaded=%s", len(args.gp_name_links))
    logger.info(
        "Partially scored states (subset contract)=%s", sorted(args.partially_scored_states)
    )
    if not args.hierarchy_manifest or not Path(args.hierarchy_manifest).is_file():
        logger.warning(
            "No hierarchy manifest at %s: every block must match its universe exactly, so "
            "partially scored states (Goa, Meghalaya, most PAI 1.0 states) will fail",
            args.hierarchy_manifest,
        )
    logger.info("=" * 90)

    if args.reset_global_indexes:
        for p in [
            out_dir / "block_manifest.csv",
            out_dir / "block_manifest.parquet",
            out_dir / "dropdown_inventory.csv",
            out_dir / "dropdown_inventory.parquet",
        ]:
            if p.exists():
                logger.info("Removing existing global index: %s", p)
                p.unlink()

    async with async_playwright() as playwright:
        launch_kwargs = {
            "headless": args.headless,
            "slow_mo": args.slow_mo,
        }
        if args.browser_channel:
            launch_kwargs["channel"] = args.browser_channel

        browser = await playwright.chromium.launch(**launch_kwargs)
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
                            "metadata_file": "",
                            "scores_file": "",
                            "wide_file": "",
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
    if not args.skip_rebuild_derived:
        from pai_rebuild_index import build, parse_expected

        logger.info("Building typed global Parquet and enforcing collection contracts ...")
        contract = build(
            out_dir,
            out_dir / "derived",
            parse_expected(args.expected_state_gps),
            args.years,
            args.national_official_controls,
            universe_data_dir=Path(args.universe_data_dir) if args.universe_data_dir else None,
        )
        logger.info(
            "Derived contract passed: gp_rows=%s score_rows=%s -> %s",
            contract["gp_rows"],
            contract["score_rows"],
            (out_dir / "derived").resolve(),
        )


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
        "--baseline-counts-from",
        help="Prior block store used only for old-vs-live count diagnostics",
    )
    parser.add_argument(
        "--universe-exceptions",
        help="Reviewed CSV of intentionally unscored GP codes, each with evidence",
    )
    parser.add_argument(
        "--hierarchy-manifest",
        default="runs/pai_universe/collection_manifest.json",
        help="Universe collection_manifest.json; states scored below their hierarchy "
        "size accept blocks that are a subset of the universe (default: the standalone crawl)",
    )
    parser.add_argument(
        "--gp-name-links",
        help="Reviewed CSV linking ambiguous score GP names to universe GP codes",
    )

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
        default=1,
        help="Times a 'not available' must reproduce on re-search before it is accepted as "
        "genuine no-data (0 = trust the first response; default 1). Across-pass re-verification "
        "(--retry-no-data) adds further, time-independent confirmation.",
    )
    parser.add_argument(
        "--complete-confirm",
        type=int,
        default=1,
        help="Independent repeat retrievals required after a complete result (default 1)",
    )
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--stop-on-error", action="store_true")

    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--browser-channel", default=None, help="Optional, e.g. chrome")
    parser.add_argument("--slow-mo", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--block-third-party", action="store_true")
    parser.add_argument("--allow-year-mismatch", action="store_true")

    parser.add_argument(
        "--reset-global-indexes",
        action="store_true",
        help="Remove the append-only logs, including their compacted Parquet history "
        "(resumption reads DONE.json, never the logs)",
    )
    parser.add_argument(
        "--expected-state-gps",
        action="append",
        default=[],
        metavar="[YEAR:]STATE=N",
        help="Hard official GP count checked when derived Parquet is built; repeat by state",
    )
    parser.add_argument(
        "--skip-rebuild-derived",
        action="store_true",
        help="Do not build typed global Parquet when the scrape finishes",
    )
    parser.add_argument(
        "--national-official-controls",
        action="store_true",
        help="Require all 33 PAI 2.0 state controls and the India total at final rebuild",
    )
    parser.add_argument(
        "--universe-data-dir",
        help="Independent universe crawl (dir or gp_universe.parquet) for the final rebuild; "
        "required with --national-official-controls",
    )

    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))

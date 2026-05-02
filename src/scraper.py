"""
CaseNet scraper.

Drives Missouri CaseNet's Scheduled Hearings → Search by Attorney form via
Playwright (headless Chromium) and returns the raw HTML of the results table
for the parser to consume.

Why Playwright instead of `requests`:
- CaseNet uses ASP.NET ViewState forms with anti-bot heuristics.
- A real browser handles the JS-driven postbacks more reliably.
- We can take screenshots on failure for triage.

Run locally to verify form behavior on first setup:

    python -m src.scraper --debug --bar-number 76645 --county Jackson

That writes the rendered HTML and a screenshot to actions-debug/ so you can
confirm CaseNet's current markup matches what parser.py expects.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import (  # type: ignore
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# These constants are the most likely things you'll need to tweak if CaseNet
# changes their UI. They're text-based on purpose — text is more stable across
# markup refactors than CSS selectors.
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://www.courts.mo.gov/cnet/welcome.do"

# Visible link/button text we click in sequence to get to the attorney search.
NAV_LINK_TEXT_SCHEDULED_HEARINGS = "Scheduled Hearings"
NAV_LINK_TEXT_SEARCH_BY_ATTORNEY = "Search by Attorney"

# Form field labels (we locate inputs by their visible label, not by name).
FIELD_LABEL_BAR_NUMBER_CANDIDATES = [
    "Bar Number",
    "Attorney Bar Number",
    "MO Bar Number",
]
FIELD_LABEL_COUNTY_CANDIDATES = [
    "Court",
    "County",
    "Practicing Court",
    "Circuit",
]
FIELD_LABEL_START_DATE_CANDIDATES = [
    "Begin Date",
    "Start Date",
    "From Date",
    "Hearing Start Date",
]
FIELD_LABEL_END_DATE_CANDIDATES = [
    "End Date",
    "Through Date",
    "To Date",
    "Hearing End Date",
]

SUBMIT_BUTTON_TEXT_CANDIDATES = ["Find", "Search", "Submit"]

# How long to wait for the results page to render before declaring failure.
RESULTS_TIMEOUT_MS = 30_000

# User-agent string — a real recent Chrome on macOS.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Data shape returned to caller
# ---------------------------------------------------------------------------


@dataclass
class ScrapeResult:
    """One scrape per (bar_number, county) — returns the raw HTML for parsing."""

    bar_number: str
    county: str
    fetched_at_utc: str
    html: str
    debug_artifacts_dir: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _click_first_matching(page: Page, candidates: List[str], *, exact: bool = False) -> bool:
    """Click the first link/button whose visible text matches a candidate.

    Returns True if a click happened. Used for nav links where CaseNet may
    rename the link without us noticing.
    """
    for text in candidates:
        try:
            locator = page.get_by_text(text, exact=exact).first
            if locator.is_visible(timeout=1500):
                locator.click()
                return True
        except PlaywrightTimeoutError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.debug("Click attempt for %r raised: %s", text, exc)
    return False


def _fill_first_matching_label(
    page: Page, candidates: List[str], value: str
) -> bool:
    """Fill a form input by trying multiple label texts. Returns True on success."""
    for label in candidates:
        try:
            locator = page.get_by_label(label, exact=False).first
            if locator.is_visible(timeout=1500):
                locator.fill(value)
                return True
        except PlaywrightTimeoutError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.debug("Fill attempt for label %r raised: %s", label, exc)
    return False


def _select_first_matching_label(
    page: Page, candidates: List[str], visible_value: str
) -> bool:
    """Select a <select> option by visible text. CaseNet uses dropdowns for county."""
    for label in candidates:
        try:
            locator = page.get_by_label(label, exact=False).first
            if locator.is_visible(timeout=1500):
                # Try as a <select> first; fall back to type-and-pick.
                try:
                    locator.select_option(label=visible_value)
                    return True
                except Exception:
                    locator.click()
                    page.get_by_text(visible_value, exact=False).first.click()
                    return True
        except PlaywrightTimeoutError:
            continue
        except Exception as exc:  # noqa: BLE001
            log.debug("Select attempt for label %r raised: %s", label, exc)
    return False


def _save_debug(page: Page, out_dir: Path, name: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
    (out_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main scrape entrypoint
# ---------------------------------------------------------------------------


def scrape(
    bar_number: str,
    county: str,
    *,
    forward_days: int = 90,
    headless: bool = True,
    base_url: str = DEFAULT_BASE_URL,
    debug_dir: Optional[Path] = None,
) -> ScrapeResult:
    """Run one scrape for one (bar_number, county). Returns raw HTML.

    Raises ScraperError on any unrecoverable failure. On failure, debug
    artifacts (screenshot + HTML) are written to debug_dir if provided.
    """
    if not bar_number.isdigit():
        raise ValueError(f"bar_number must be all digits, got {bar_number!r}")

    fetched_at = datetime.now(timezone.utc).isoformat()
    warnings: List[str] = []

    today = datetime.now()
    end = today + timedelta(days=forward_days)
    start_str = today.strftime("%m/%d/%Y")
    end_str = end.strftime("%m/%d/%Y")

    log.info(
        "Scraping CaseNet for bar=%s county=%s window=%s..%s",
        bar_number,
        county,
        start_str,
        end_str,
    )

    with sync_playwright() as p:
        browser: Browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="en-US")
            page = context.new_page()

            page.goto(base_url, wait_until="domcontentloaded", timeout=RESULTS_TIMEOUT_MS)

            # Step 1 — Scheduled Hearings link.
            if not _click_first_matching(page, [NAV_LINK_TEXT_SCHEDULED_HEARINGS]):
                if debug_dir:
                    _save_debug(page, debug_dir, "01-no-scheduled-hearings-link")
                raise ScraperError(
                    "Could not find the 'Scheduled Hearings' link on the CaseNet "
                    "landing page. CaseNet may have changed its navigation. See "
                    "actions-debug/01-*.html for the page we landed on."
                )
            page.wait_for_load_state("domcontentloaded", timeout=RESULTS_TIMEOUT_MS)

            # Step 2 — Search by Attorney link/tab.
            if not _click_first_matching(page, [NAV_LINK_TEXT_SEARCH_BY_ATTORNEY]):
                if debug_dir:
                    _save_debug(page, debug_dir, "02-no-search-by-attorney-link")
                raise ScraperError(
                    "Could not find the 'Search by Attorney' tab. CaseNet may "
                    "have changed how this search is presented."
                )
            page.wait_for_load_state("domcontentloaded", timeout=RESULTS_TIMEOUT_MS)

            # Step 3 — fill the form.
            if not _fill_first_matching_label(page, FIELD_LABEL_BAR_NUMBER_CANDIDATES, bar_number):
                if debug_dir:
                    _save_debug(page, debug_dir, "03-no-bar-number-field")
                raise ScraperError(
                    "Could not locate the bar-number input field. Edit "
                    "FIELD_LABEL_BAR_NUMBER_CANDIDATES in src/scraper.py if "
                    "CaseNet has renamed it."
                )

            if not _select_first_matching_label(page, FIELD_LABEL_COUNTY_CANDIDATES, county):
                # Some CaseNet forms accept a typed county name instead of a select.
                if not _fill_first_matching_label(page, FIELD_LABEL_COUNTY_CANDIDATES, county):
                    if debug_dir:
                        _save_debug(page, debug_dir, "04-no-county-field")
                    raise ScraperError(
                        f"Could not set the county to {county!r}. The dropdown "
                        "may use different text — check actions-debug/04-*.html."
                    )

            # Date range — best-effort. CaseNet ignores or caps internally.
            _fill_first_matching_label(page, FIELD_LABEL_START_DATE_CANDIDATES, start_str)
            _fill_first_matching_label(page, FIELD_LABEL_END_DATE_CANDIDATES, end_str)

            # Step 4 — submit.
            submitted = False
            for btn_text in SUBMIT_BUTTON_TEXT_CANDIDATES:
                try:
                    page.get_by_role("button", name=btn_text).first.click(timeout=2000)
                    submitted = True
                    break
                except Exception:
                    continue
            if not submitted:
                # Fallback: press Enter in the bar-number field.
                page.keyboard.press("Enter")

            page.wait_for_load_state("networkidle", timeout=RESULTS_TIMEOUT_MS)

            # Detect "no results" messaging vs. an actual error page.
            html = page.content()
            lower = html.lower()
            if "captcha" in lower or "are you a human" in lower:
                if debug_dir:
                    _save_debug(page, debug_dir, "05-captcha-blocked")
                raise ScraperError(
                    "CaseNet served a CAPTCHA. The workflow cannot proceed "
                    "without manual intervention. Re-run later or update "
                    "scraper to use a paced retry strategy."
                )

            if "no records" in lower or "no results" in lower or "0 records" in lower:
                warnings.append("CaseNet returned no records for this query.")

            return ScrapeResult(
                bar_number=bar_number,
                county=county,
                fetched_at_utc=fetched_at,
                html=html,
                debug_artifacts_dir=debug_dir,
                warnings=warnings,
            )
        finally:
            browser.close()


class ScraperError(RuntimeError):
    """Raised when CaseNet's UI doesn't behave as expected."""


# ---------------------------------------------------------------------------
# CLI for local debugging
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CaseNet attorney-search scraper")
    parser.add_argument("--bar-number", required=True)
    parser.add_argument("--county", required=True)
    parser.add_argument("--forward-days", type=int, default=90)
    parser.add_argument("--debug", action="store_true",
                        help="Save HTML + screenshot to actions-debug/ at every step")
    parser.add_argument("--show-browser", action="store_true",
                        help="Run Chromium with a visible window")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    debug_dir = Path("actions-debug") if args.debug else None

    try:
        result = scrape(
            bar_number=args.bar_number,
            county=args.county,
            forward_days=args.forward_days,
            headless=not args.show_browser,
            debug_dir=debug_dir,
        )
    except ScraperError as exc:
        log.error("Scrape failed: %s", exc)
        return 2

    out_path = Path("actions-debug" if args.debug else ".") / "casenet_results.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.html, encoding="utf-8")
    log.info("Saved %d bytes of HTML to %s", len(result.html), out_path)
    for w in result.warnings:
        log.warning(w)
    return 0


if __name__ == "__main__":
    sys.exit(main())

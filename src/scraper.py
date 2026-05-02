"""
CaseNet scraper — Playwright (headless Chromium) against
calendarSearchResult.do.

We tried plain `requests` first; CaseNet returns HTTP 403 to the GitHub
Actions runner IPs (likely a generic anti-scraper rule). Headless Chromium
with realistic headers gets through cleanly.

The CaseNet form is a plain GET, so we don't need to drive any JS — we just
navigate Chromium to the result URL directly. Form fields:

    courtCode      = "CT16SPACEJAK" for Jackson County (16th Judicial Circuit)
    searchType     = "A" (Attorney) | "J" (Judge)
    searchLength   = "1" (single day) | "7" (seven day)
    mobarNumber    = your bar number
    startDate      = MM/DD/YYYY

CaseNet caps each search to 7 days, so we walk forward in 7-day chunks.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlencode

from playwright.sync_api import (  # type: ignore
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

log = logging.getLogger(__name__)

RESULTS_URL = "https://www.courts.mo.gov/casenet/calendarSearchResult.do"
WARMUP_URL = "https://www.courts.mo.gov/cnet/welcome.do"

# County → CaseNet court code mapping. Add more by inspecting the form's
# <select name="courtCode"> option values.
COURT_CODES = {
    "Jackson": "CT16SPACEJAK",
    "Clay":    "CT07SPACECLY",
    "Platte":  "CT06SPACEPLT",
    "Cass":    "CT17SPACECAS",
    "Johnson": "CT17SPACEJOH",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

PAGE_TIMEOUT_MS = 30_000


@dataclass
class ScrapeResult:
    bar_number: str
    county: str
    fetched_at_utc: str
    html: str
    chunk_count: int
    debug_artifacts_dir: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)


class ScraperError(RuntimeError):
    pass


def scrape(
    bar_number: str,
    county: str,
    *,
    forward_days: int = 60,
    headless: bool = True,
    base_url: str = RESULTS_URL,
    debug_dir: Optional[Path] = None,
) -> ScrapeResult:
    if not bar_number.isdigit():
        raise ValueError(f"bar_number must be all digits, got {bar_number!r}")
    if county not in COURT_CODES:
        raise ScraperError(
            f"Unknown county {county!r}. Add it to COURT_CODES in scraper.py "
            f"with the matching CaseNet court code from the form's "
            f"<select name='courtCode'> options."
        )

    fetched_at = datetime.now(timezone.utc).isoformat()
    chunks: List[str] = []
    warnings: List[str] = []

    today = datetime.now()
    chunk_start = today
    chunk_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()

            # Warm up: hit the welcome page first so any session cookies are
            # established before we hit the result endpoint.
            try:
                page.goto(WARMUP_URL, wait_until="domcontentloaded",
                          timeout=PAGE_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                warnings.append(f"Warmup of {WARMUP_URL} timed out; continuing.")

            while chunk_start <= today + timedelta(days=forward_days):
                params = {
                    "courtCode": COURT_CODES[county],
                    "searchType": "A",
                    "searchLength": "7",
                    "mobarNumber": bar_number,
                    "startDate": chunk_start.strftime("%m/%d/%Y"),
                }
                url = f"{base_url}?{urlencode(params)}"
                log.info("CaseNet GET window=%s (chunk %d)",
                         chunk_start.strftime("%Y-%m-%d"), chunk_count + 1)
                try:
                    response = page.goto(url, wait_until="domcontentloaded",
                                         timeout=PAGE_TIMEOUT_MS)
                except PlaywrightTimeoutError as exc:
                    raise ScraperError(
                        f"Timeout fetching CaseNet for {county} window "
                        f"starting {chunk_start:%Y-%m-%d}: {exc}"
                    ) from exc

                status = response.status if response else 0
                if status != 200:
                    body = page.content()
                    if debug_dir:
                        _save_debug_html(
                            debug_dir, f"chunk-{chunk_count}-status-{status}", body)
                    raise ScraperError(
                        f"CaseNet returned HTTP {status} for {county} window "
                        f"starting {chunk_start:%Y-%m-%d}."
                    )

                body = page.content()
                lower = body.lower()
                if "captcha" in lower or "are you a human" in lower:
                    if debug_dir:
                        _save_debug_html(
                            debug_dir, f"chunk-{chunk_count}-captcha", body)
                    raise ScraperError(
                        "CaseNet served a CAPTCHA. Re-run later or contact "
                        "OSCA if persistent."
                    )

                chunks.append(body)
                chunk_count += 1
                chunk_start += timedelta(days=7)
        finally:
            browser.close()

    if not chunks:
        raise ScraperError("No chunks fetched.")

    html = "\n<!-- CHUNK BOUNDARY -->\n".join(chunks)
    if debug_dir:
        _save_debug_html(debug_dir, "merged-results", html)

    return ScrapeResult(
        bar_number=bar_number,
        county=county,
        fetched_at_utc=fetched_at,
        html=html,
        chunk_count=chunk_count,
        debug_artifacts_dir=debug_dir,
        warnings=warnings,
    )


def _save_debug_html(out_dir: Path, name: str, html: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.html").write_text(html, encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CaseNet attorney-search scraper")
    parser.add_argument("--bar-number", required=True)
    parser.add_argument("--county", required=True)
    parser.add_argument("--forward-days", type=int, default=60)
    parser.add_argument("--debug", action="store_true",
                        help="Save raw HTML chunks to actions-debug/")
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
    log.info("Saved %d bytes (%d chunks) to %s",
             len(result.html), result.chunk_count, out_path)
    for w in result.warnings:
        log.warning(w)
    return 0


if __name__ == "__main__":
    sys.exit(main())

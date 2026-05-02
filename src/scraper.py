"""
CaseNet scraper — direct GET against Missouri CaseNet's
calendarSearchResult.do endpoint.

The form on https://www.courts.mo.gov/casenet/scheduledHearingSearch.do is a
plain GET form, so we skip the form click-through and hit the result URL
directly. Form fields:

    courtCode      = "CT16SPACEJAK" for Jackson County (16th Judicial Circuit)
    searchType     = "A" (Attorney) | "J" (Judge)
    searchLength   = "1" (single day) | "7" (seven day)
    mobarNumber    = your bar number
    startDate      = MM/DD/YYYY

CaseNet caps the search to 7 days at a time, so we walk forward in 7-day
chunks to cover the full window the user asked for.
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

import requests  # type: ignore

log = logging.getLogger(__name__)

RESULTS_URL = "https://www.courts.mo.gov/casenet/calendarSearchResult.do"

# County → CaseNet court code mapping. Add more as needed by inspecting
# <select name="courtCode"> on the search form.
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

REQUEST_TIMEOUT = 30  # seconds per HTTP request


@dataclass
class ScrapeResult:
    bar_number: str
    county: str
    fetched_at_utc: str
    html: str                              # concatenated HTML from all chunks
    chunk_count: int                       # how many 7-day windows we fetched
    debug_artifacts_dir: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)


class ScraperError(RuntimeError):
    """Raised when CaseNet returns something we can't make sense of."""


def scrape(
    bar_number: str,
    county: str,
    *,
    forward_days: int = 60,
    headless: bool = True,           # kept for compatibility; unused now
    base_url: str = RESULTS_URL,     # kept for compatibility
    debug_dir: Optional[Path] = None,
) -> ScrapeResult:
    if not bar_number.isdigit():
        raise ValueError(f"bar_number must be all digits, got {bar_number!r}")
    if county not in COURT_CODES:
        raise ScraperError(
            f"Unknown county {county!r}. Add it to COURT_CODES in scraper.py "
            f"with the matching CaseNet court code (visible in the form's "
            f"<select name='courtCode'> options)."
        )

    fetched_at = datetime.now(timezone.utc).isoformat()
    chunks: List[str] = []
    warnings: List[str] = []

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    today = datetime.now()
    chunk_start = today
    chunk_size_days = 7
    chunk_count = 0

    while chunk_start <= today + timedelta(days=forward_days):
        params = {
            "courtCode": COURT_CODES[county],
            "searchType": "A",
            "searchLength": str(chunk_size_days),
            "mobarNumber": bar_number,
            "startDate": chunk_start.strftime("%m/%d/%Y"),
        }
        url = f"{base_url}?{urlencode(params)}"
        log.info("CaseNet GET window=%s (chunk %d)",
                 chunk_start.strftime("%Y-%m-%d"), chunk_count + 1)
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise ScraperError(
                f"Network error fetching CaseNet for {county} starting "
                f"{chunk_start:%Y-%m-%d}: {exc}"
            ) from exc

        if resp.status_code != 200:
            if debug_dir:
                _save_debug_html(debug_dir, f"chunk-{chunk_count}-status-{resp.status_code}", resp.text)
            raise ScraperError(
                f"CaseNet returned HTTP {resp.status_code} for {county} "
                f"window starting {chunk_start:%Y-%m-%d}."
            )

        body = resp.text
        if "captcha" in body.lower() or "are you a human" in body.lower():
            if debug_dir:
                _save_debug_html(debug_dir, f"chunk-{chunk_count}-captcha", body)
            raise ScraperError(
                "CaseNet served a CAPTCHA. The job cannot proceed without "
                "manual intervention. Re-run later."
            )

        chunks.append(body)
        chunk_count += 1
        chunk_start += timedelta(days=chunk_size_days)

    if not chunks:
        raise ScraperError("No chunks fetched — forward_days must be >= 0.")

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


# ---------------------------------------------------------------------------
# CLI for local debugging
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CaseNet attorney-search scraper")
    parser.add_argument("--bar-number", required=True)
    parser.add_argument("--county", required=True)
    parser.add_argument("--forward-days", type=int, default=60)
    parser.add_argument("--debug", action="store_true",
                        help="Save raw HTML chunks to actions-debug/")
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

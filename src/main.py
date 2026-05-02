"""
Daily orchestrator. Runs in GitHub Actions (or locally for testing).

Pipeline:
  1. Read config.yaml + prep_rules.yaml
  2. For each county in config: scrape → parse
  3. Merge results across counties
  4. Diff against state/docket.json
  5. Apply prep rules → FlaggedHearing list
  6. Notify: email, calendar, dashboard
  7. Save updated state/docket.json

Failure modes:
  - Scraper raises ScraperError → email "manual check required", exit 2
  - Any notifier fails → log it, continue with the others, exit 1 at end
  - All notifiers succeed → exit 0
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List

import yaml  # type: ignore

from . import differ, flags as flagmod, notify_calendar, notify_email, render_dashboard
from .parser import Hearing, merge, parse
from .scraper import ScraperError, scrape

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
RULES_PATH = REPO_ROOT / "prep_rules.yaml"
STATE_PATH = REPO_ROOT / "state" / "docket.json"
DOCS_DIR = REPO_ROOT / "docs"
DEBUG_DIR = REPO_ROOT / "actions-debug"


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    rules = flagmod.load_rules(RULES_PATH)

    bar_number = os.environ.get("MOBAR_NUMBER")
    if not bar_number:
        log.error("MOBAR_NUMBER environment variable is required.")
        return 2

    counties: List[str] = config["attorney"]["counties"]
    forward_days: int = int(config["scraper"].get("forward_days", 90))
    headless: bool = bool(config["scraper"].get("headless", True))

    # ---- 1. Scrape every configured county ----
    all_results = []
    failed_counties = []
    for county in counties:
        try:
            result = scrape(
                bar_number=bar_number,
                county=county,
                forward_days=forward_days,
                headless=headless,
                debug_dir=DEBUG_DIR if config["scraper"].get("save_debug_artifacts_on_failure", True) else None,
            )
            hearings_for_county = parse(
                result.html,
                county=county,
                default_tz=config["attorney"].get("timezone", "America/Chicago"),
            )
            all_results.append(hearings_for_county)
            log.info("County %s: %d hearings parsed", county, len(hearings_for_county))
        except ScraperError as exc:
            log.error("Scraper failed for county=%s: %s", county, exc)
            failed_counties.append((county, str(exc)))

    # Decide which notifiers are actually configured. Each notifier is opt-in:
    # if its required env vars aren't set, we skip it cleanly so the workflow
    # stays green. The dashboard is always on (no secrets needed).
    email_configured = bool(os.environ.get("GMAIL_USER")) and bool(os.environ.get("GMAIL_APP_PASSWORD"))
    calendar_configured = all(
        os.environ.get(k)
        for k in ("GCAL_CLIENT_ID", "GCAL_CLIENT_SECRET", "GCAL_REFRESH_TOKEN", "GCAL_CALENDAR_ID")
    )

    if failed_counties and not all_results:
        # Total failure — best-effort email if configured, else just exit.
        if email_configured:
            try:
                notify_email.send_failure(config=config, errors=failed_counties)
            except Exception as exc:  # noqa: BLE001
                log.exception("Failure email also failed: %s", exc)
        return 2

    # ---- 2. Merge and diff ----
    current: List[Hearing] = merge(all_results)
    previous: List[Hearing] = differ.load_state(STATE_PATH)
    diff_result = differ.diff(previous, current)

    # ---- 3. Apply prep rules ----
    flagged = flagmod.apply(current, rules)

    # ---- 4. Notify ----
    exit_code = 0
    notifiers = []
    if email_configured:
        notifiers.append(("email", lambda: notify_email.send(
            config=config,
            flagged=flagged,
            diff=diff_result,
            failed_counties=failed_counties,
        )))
    else:
        log.info("Notifier email skipped — GMAIL_USER / GMAIL_APP_PASSWORD not set.")

    if calendar_configured:
        notifiers.append(("calendar", lambda: notify_calendar.sync(
            config=config,
            current=current,
            diff=diff_result,
        )))
    else:
        log.info("Notifier calendar skipped — GCAL_* secrets not set.")

    notifiers.append(("dashboard", lambda: render_dashboard.render(
        config=config,
        flagged=flagged,
        diff=diff_result,
        out_dir=DOCS_DIR,
    )))
    for name, fn in notifiers:
        try:
            fn()
            log.info("Notifier %s OK", name)
        except Exception as exc:  # noqa: BLE001
            log.exception("Notifier %s failed: %s", name, exc)
            exit_code = 1

    # ---- 5. Persist state for tomorrow's diff ----
    differ.save_state(STATE_PATH, current)
    log.info("Saved state with %d hearings", len(current))

    return exit_code


if __name__ == "__main__":
    sys.exit(main())

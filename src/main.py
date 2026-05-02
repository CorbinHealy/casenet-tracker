"""
Mac-local daily orchestrator. Triggered by the LaunchAgent at 6 AM.

Pipeline:
  1. Read config.yaml + prep_rules.yaml
  2. For each county: scrape (Playwright) → parse
  3. Merge + diff against state/docket.json
  4. Apply prep rules
  5. Notify: dashboard render, Apple Calendar sync, iMessage
  6. git add/commit/push state + docs (so the public dashboard updates)
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List

import yaml  # type: ignore

from . import (
    differ,
    flags as flagmod,
    notify_apple_calendar,
    notify_imessage,
    render_dashboard,
)
from .parser import Hearing, merge, parse
from .scraper import ScraperError, scrape

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
RULES_PATH = REPO_ROOT / "prep_rules.yaml"
STATE_PATH = REPO_ROOT / "state" / "docket.json"
DOCS_DIR = REPO_ROOT / "docs"
DEBUG_DIR = REPO_ROOT / "actions-debug"
LOG_DIR = REPO_ROOT / "logs"


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / "tracker.log"),
            logging.StreamHandler(),
        ],
    )

    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    rules = flagmod.load_rules(RULES_PATH)

    bar_number = (
        os.environ.get("MOBAR_NUMBER")
        or config.get("attorney", {}).get("mobar_number")
    )
    if not bar_number:
        bar_path = REPO_ROOT / ".bar_number"
        if bar_path.exists():
            bar_number = bar_path.read_text(encoding="utf-8").strip()
    if not bar_number:
        log.error(
            "MOBAR_NUMBER not found. Set environment variable, or write your "
            "bar number to a single-line file at %s, or add "
            "attorney.mobar_number to config.yaml.",
            REPO_ROOT / ".bar_number",
        )
        return 2

    counties: List[str] = config["attorney"]["counties"]
    forward_days: int = int(config["scraper"].get("forward_days", 60))
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
            log.info("County %s: %d hearings parsed (%d chunks fetched)",
                     county, len(hearings_for_county), result.chunk_count)
        except ScraperError as exc:
            log.error("Scraper failed for county=%s: %s", county, exc)
            failed_counties.append((county, str(exc)))

    if failed_counties and not all_results:
        return 2

    # ---- 2. Merge and diff ----
    current: List[Hearing] = merge(all_results)
    previous: List[Hearing] = differ.load_state(STATE_PATH)
    diff_result = differ.diff(previous, current)

    # ---- 3. Apply prep rules ----
    flagged = flagmod.apply(current, rules)

    # ---- 4. Notify (each in try/except so one failure doesn't kill the rest) ----
    exit_code = 0

    notifiers = [
        ("dashboard", lambda: render_dashboard.render(
            config=config,
            flagged=flagged,
            diff=diff_result,
            out_dir=DOCS_DIR,
        )),
    ]
    if config["calendar"].get("enabled", True):
        notifiers.append(("apple_calendar", lambda: notify_apple_calendar.sync(
            config=config,
            current=current,
            diff=diff_result,
        )))
    if config["notify"].get("imessage_to"):
        notifiers.append(("imessage", lambda: notify_imessage.send(
            config=config,
            flagged=flagged,
            diff=diff_result,
        )))

    for name, fn in notifiers:
        try:
            fn()
            log.info("Notifier %s OK", name)
        except Exception as exc:  # noqa: BLE001
            log.exception("Notifier %s failed: %s", name, exc)
            exit_code = 1

    # ---- 5. Persist state ----
    differ.save_state(STATE_PATH, current)
    log.info("Saved state with %d hearings", len(current))

    # ---- 6. Push state + dashboard to GitHub ----
    if config.get("git", {}).get("auto_push", True):
        _git_push_dashboard()

    return exit_code


def _git_push_dashboard() -> None:
    """Commit state/ + docs/ updates and push origin/main.

    Runs as the user's identity (git config in the repo). Auth uses the
    macOS keychain via git-credential-osxkeychain — already populated by
    GitHub Desktop's login. No interaction needed in steady state.
    """
    cmds = [
        ["git", "-C", str(REPO_ROOT), "add", "state/", "docs/"],
        ["git", "-C", str(REPO_ROOT), "diff", "--cached", "--quiet"],
    ]
    # First two are safe; the diff exits 0 if no changes, 1 if there are.
    subprocess.run(cmds[0], check=False)
    diff_check = subprocess.run(cmds[1], capture_output=True)
    if diff_check.returncode == 0:
        log.info("git: no dashboard/state changes to push.")
        return

    from datetime import datetime
    msg = f"daily: {datetime.now().strftime('%Y-%m-%d')} docket update"
    subprocess.run(
        ["git", "-C", str(REPO_ROOT), "commit", "-m", msg],
        check=False, capture_output=True,
    )
    push = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "push", "origin", "main"],
        capture_output=True, text=True,
    )
    if push.returncode != 0:
        log.warning("git push failed: %s", push.stderr.strip())
    else:
        log.info("Pushed dashboard to GitHub.")


if __name__ == "__main__":
    sys.exit(main())

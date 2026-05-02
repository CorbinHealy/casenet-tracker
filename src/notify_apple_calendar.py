"""
Sync hearings to a dedicated Apple Calendar via AppleScript (osascript).

Why AppleScript instead of CalDAV/EventKit:
- AppleScript is the only no-extra-deps way to script Calendar.app from Python.
- iCloud sync is automatic — events created here show up on the iPhone
  Calendar app within ~1 minute.
- No OAuth, no credentials, no cloud setup.

The first run will trigger a macOS permission prompt: "Python wants to control
Calendar." Click OK. You can also pre-grant via System Settings → Privacy &
Security → Automation.

Calendar name comes from config.yaml's `calendar.apple_calendar_name`. If a
calendar by that name doesn't exist, it's created automatically.

Idempotency: each event we create has its uid embedded in the event's URL
field. We look for an existing event with that uid before creating to avoid
duplicates across runs.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import timedelta
from typing import List

from dateutil import parser as dateparser  # type: ignore

from .differ import DiffResult
from .parser import Hearing

log = logging.getLogger(__name__)

DEFAULT_DURATION_HOURS = 1


def sync(*, config: dict, current: List[Hearing], diff: DiffResult) -> None:
    cal_name = config["calendar"].get("apple_calendar_name", "CaseNet")
    party_display = config["calendar"].get("party_display", "full")
    title_prefix = config["calendar"].get("event_title_prefix", "")

    _ensure_calendar_exists(cal_name)

    # Upsert all current hearings.
    for h in current:
        title = _event_title(h, party_display, title_prefix)
        description = _event_description(h)
        start_dt = dateparser.parse(h.datetime_iso)
        end_dt = start_dt + timedelta(hours=DEFAULT_DURATION_HOURS)
        location = h.location or ""
        _upsert_event(
            cal_name=cal_name,
            uid=h.uid,
            title=title,
            description=description,
            start_iso=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_iso=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            location=location,
        )

    # Delete cancelled.
    for h in diff.cancelled:
        _delete_event(cal_name=cal_name, uid=h.uid)


# ---------------------------------------------------------------------------
# Helpers — every interaction with Calendar.app goes through osascript.
# ---------------------------------------------------------------------------


def _run_osascript(script: str) -> str:
    """Run an AppleScript snippet, return stdout, raise on non-zero exit."""
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"osascript failed (rc={proc.returncode}): {proc.stderr.strip()}\n"
            f"Script: {script[:200]}..."
        )
    return proc.stdout.strip()


def _ensure_calendar_exists(cal_name: str) -> None:
    """Create the calendar if it doesn't already exist."""
    safe = cal_name.replace('"', '\\"')
    script = f'''
    tell application "Calendar"
        if not (exists calendar "{safe}") then
            make new calendar with properties {{name:"{safe}"}}
        end if
    end tell
    '''
    _run_osascript(script)
    log.info("Calendar %r ready.", cal_name)


def _upsert_event(
    *,
    cal_name: str,
    uid: str,
    title: str,
    description: str,
    start_iso: str,    # "YYYY-MM-DD HH:MM:SS"
    end_iso: str,
    location: str,
) -> None:
    """Create or update one event identified by uid (stored in event URL)."""
    safe_cal = cal_name.replace('"', '\\"')
    safe_title = title.replace('"', '\\"')
    safe_desc = description.replace('"', '\\"').replace("\n", "\\n")
    safe_loc = location.replace('"', '\\"')
    safe_uid = uid.replace('"', '\\"')

    script = f'''
    tell application "Calendar"
        tell calendar "{safe_cal}"
            set startDt to date "{_to_applescript_date(start_iso)}"
            set endDt to date "{_to_applescript_date(end_iso)}"
            set existing to (every event whose url is "casenet:{safe_uid}")
            if (count of existing) > 0 then
                set theEvent to first item of existing
                set summary of theEvent to "{safe_title}"
                set start date of theEvent to startDt
                set end date of theEvent to endDt
                set description of theEvent to "{safe_desc}"
                set location of theEvent to "{safe_loc}"
            else
                make new event with properties {{summary:"{safe_title}", start date:startDt, end date:endDt, description:"{safe_desc}", location:"{safe_loc}", url:"casenet:{safe_uid}"}}
            end if
        end tell
    end tell
    '''
    _run_osascript(script)
    log.info("Calendar upserted %s (%s)", uid, title[:40])


def _delete_event(*, cal_name: str, uid: str) -> None:
    safe_cal = cal_name.replace('"', '\\"')
    safe_uid = uid.replace('"', '\\"')
    script = f'''
    tell application "Calendar"
        tell calendar "{safe_cal}"
            set existing to (every event whose url is "casenet:{safe_uid}")
            repeat with e in existing
                delete e
            end repeat
        end tell
    end tell
    '''
    _run_osascript(script)
    log.info("Calendar deleted %s", uid)


def _to_applescript_date(iso_local: str) -> str:
    """Convert "YYYY-MM-DD HH:MM:SS" → "Monday, May 4, 2026 at 9:00:00 AM"
    style string AppleScript expects.

    Actually AppleScript can parse "MM/DD/YYYY HH:MM:SS AM/PM" reliably across
    locales. We use that.
    """
    from datetime import datetime
    dt = datetime.strptime(iso_local, "%Y-%m-%d %H:%M:%S")
    return dt.strftime("%m/%d/%Y %I:%M:%S %p")


# ---------------------------------------------------------------------------
# Title + description formatting
# ---------------------------------------------------------------------------


def _event_title(h: Hearing, party_display: str, title_prefix: str) -> str:
    parties = _format_parties(h.style, party_display)
    pieces = [p for p in [parties, h.hearing_type] if p]
    return f"{title_prefix}{' · '.join(pieces)}".strip()


def _format_parties(style: str, mode: str) -> str:
    if mode == "case_only" or not style:
        return ""
    if mode == "initials":
        if " v. " in style.lower() or " v " in style.lower():
            parts = style.split(" V " if " V " in style else " v. ")
            if len(parts) == 2:
                left, right = parts
                initials = "".join(p[0] for p in right.split() if p and p[0].isalpha()).upper()
                initials = ".".join(initials) + "." if initials else ""
                return f"{left.split()[0]} v. {initials}"
        return style[:24]
    return style


def _event_description(h: Hearing) -> str:
    lines = [
        f"Case: {h.case_number}",
        f"Style: {h.style}",
        f"Type: {h.hearing_type}",
        f"Location: {h.location}",
    ]
    if h.judge:
        lines.append(f"Judge: {h.judge}")
    lines.append(f"County: {h.county}")
    lines.append("")
    lines.append("Synced from Missouri CaseNet by casenet-tracker.")
    return "\n".join(lines)

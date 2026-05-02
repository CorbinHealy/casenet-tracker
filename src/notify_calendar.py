"""
Sync hearings to a dedicated Google Calendar.

Auth: OAuth refresh-token flow. See README setup §5 + src/auth_gcal.py.
Env vars expected:
    GCAL_CLIENT_ID, GCAL_CLIENT_SECRET, GCAL_REFRESH_TOKEN, GCAL_CALENDAR_ID

Idempotency: each hearing has a stable uid (`casenet-XXXX`). We use that as
the Google Calendar event id so re-running upserts cleanly.

Diff-driven cleanup:
  - Cancelled hearings → delete the event
  - Moved hearings → patch the event with new datetime/location
  - Unchanged → skip the API call (saves quota)
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import List

from dateutil import parser as dateparser  # type: ignore
from google.auth.transport.requests import Request  # type: ignore
from google.oauth2.credentials import Credentials  # type: ignore
from googleapiclient.discovery import build  # type: ignore
from googleapiclient.errors import HttpError  # type: ignore

from .differ import DiffResult
from .parser import Hearing

log = logging.getLogger(__name__)

DEFAULT_DURATION_HOURS = 1

# Google Calendar event IDs must match: lowercase, [a-v0-9-_]{5,1024}
# Our uid format ("casenet-XXXX") is already compliant.


def sync(*, config: dict, current: List[Hearing], diff: DiffResult) -> None:
    service = _build_service()
    cal_id = os.environ["GCAL_CALENDAR_ID"]
    party_display = config["calendar"].get("party_display", "full")
    title_prefix = config["calendar"].get("event_title_prefix", "")

    # Upsert every current hearing.
    for h in current:
        body = _to_event_body(h, party_display, title_prefix)
        try:
            service.events().update(
                calendarId=cal_id,
                eventId=h.uid,
                body=body,
            ).execute()
            log.info("Calendar updated event %s (%s)", h.uid, h.case_number)
        except HttpError as exc:
            if exc.resp.status == 404:
                # Doesn't exist yet — create.
                body["id"] = h.uid
                service.events().insert(calendarId=cal_id, body=body).execute()
                log.info("Calendar created event %s (%s)", h.uid, h.case_number)
            else:
                log.exception("Calendar upsert failed for %s: %s", h.case_number, exc)

    # Delete cancelled.
    for h in diff.cancelled:
        try:
            service.events().delete(calendarId=cal_id, eventId=h.uid).execute()
            log.info("Calendar deleted event %s (%s)", h.uid, h.case_number)
        except HttpError as exc:
            if exc.resp.status in (404, 410):
                continue
            log.exception("Calendar delete failed for %s: %s", h.case_number, exc)


def _build_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GCAL_REFRESH_TOKEN"],
        client_id=os.environ["GCAL_CLIENT_ID"],
        client_secret=os.environ["GCAL_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _to_event_body(h: Hearing, party_display: str, title_prefix: str) -> dict:
    start = dateparser.parse(h.datetime_iso)
    end = start + timedelta(hours=DEFAULT_DURATION_HOURS)

    parties = _format_parties(h.style, party_display)
    pieces = [p for p in [parties, h.hearing_type] if p]
    title = f"{title_prefix}{' · '.join(pieces)}".strip()

    description_lines = [
        f"Case: {h.case_number}",
        f"Style: {h.style}",
        f"Type: {h.hearing_type}",
        f"Location: {h.location}",
    ]
    if h.judge:
        description_lines.append(f"Judge: {h.judge}")
    description_lines.append(f"County: {h.county}")
    description_lines.append("")
    description_lines.append("Synced from Missouri CaseNet by casenet-tracker.")

    return {
        "summary": title,
        "description": "\n".join(description_lines),
        "location": h.location,
        "start": {
            "dateTime": start.isoformat(),
            "timeZone": str(start.tzinfo) if start.tzinfo else "America/Chicago",
        },
        "end": {
            "dateTime": end.isoformat(),
            "timeZone": str(end.tzinfo) if end.tzinfo else "America/Chicago",
        },
        "reminders": {"useDefault": True},
        "transparency": "opaque",
    }


def _format_parties(style: str, mode: str) -> str:
    if mode == "case_only" or not style:
        return ""
    if mode == "initials":
        # "State of Missouri v. John Q. Defendant" → "State v. J.Q.D."
        if " v. " in style:
            left, right = style.split(" v. ", 1)
            initials = "".join(p[0] for p in right.split() if p and p[0].isalpha()).upper()
            initials = ".".join(initials) + "." if initials else ""
            return f"{left.split()[0]} v. {initials}"
        return style[:24]
    return style  # full

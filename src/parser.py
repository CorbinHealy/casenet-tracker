"""
Parse CaseNet's Search by Attorney results HTML into structured records.

The actual page format from
https://www.courts.mo.gov/casenet/calendarSearchResult.do is a labeled-block
list, not a tabular layout. Each hearing renders as:

    [DAY HEADER, e.g. "MONDAY, MAY 4, 2026"]

    Case Number: 2516-CR02406-01
    Style of Case: ST V MICHAEL L HILL
    Time: 09:00:00
    Day: 1
    Location: Kansas City Criminal/Traffic
    Room: DIVISION 7 CRIMINAL
    Event: Plea Hearing
    Event Text: THIS WILL BE HELD IN PERSON

We parse this by:
  1. Stripping HTML to plain text
  2. Walking through the text, watching for DAY headers and Case Number
     markers, and assembling one Hearing per Case-Number-anchored block

Multi-chunk HTML (the scraper concatenates 7-day chunks) is supported via the
"<!-- CHUNK BOUNDARY -->" marker.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable, List, Optional
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup  # type: ignore
from dateutil import parser as dateparser  # type: ignore

log = logging.getLogger(__name__)


@dataclass
class Hearing:
    uid: str
    case_number: str
    style: str
    hearing_type: str
    datetime_iso: str
    location: str
    judge: str
    county: str
    raw_row_text: str

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

# Day header pattern: "MONDAY, MAY 4, 2026"
DAY_HEADER_RE = re.compile(
    r"^\s*(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY),\s*"
    r"([A-Z]+)\s+(\d{1,2}),\s*(\d{4})\s*$",
    re.IGNORECASE,
)

# Match "Label: value" — value may be empty.
LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z ]*?):\s*(.*?)\s*$")

# Labels we care about (lower-cased keys for matching).
KNOWN_LABELS = {
    "case number": "case_number",
    "style of case": "style",
    "time": "time",
    "day": "day_of_event",
    "location": "location",
    "room": "room",
    "event": "event",
    "event text": "event_text",
    "judge": "judge",
}


def _html_to_text(html: str) -> str:
    """Strip HTML to plain text similar to what a browser shows."""
    soup = BeautifulSoup(html, "lxml")
    # Remove script/style.
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text("\n")
    # Collapse runs of whitespace within lines, but keep blank lines.
    cleaned = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        cleaned.append(line)
    return "\n".join(cleaned)


# ---------------------------------------------------------------------------
# Main parse
# ---------------------------------------------------------------------------


def parse(html: str, *, county: str, default_tz: str = "America/Chicago") -> List[Hearing]:
    """Parse one or more concatenated CaseNet result chunks into Hearing records."""
    tz = ZoneInfo(default_tz)
    text = _html_to_text(html)
    lines = text.splitlines()

    hearings: List[Hearing] = []
    current_date: Optional[datetime] = None
    current_block: dict = {}

    def _flush():
        if not current_block:
            return
        h = _block_to_hearing(current_block, current_date, tz, county)
        if h:
            hearings.append(h)

    for line in lines:
        # Day header?
        m = DAY_HEADER_RE.match(line)
        if m:
            _flush()
            current_block.clear()
            try:
                current_date = dateparser.parse(line, default=datetime(1970, 1, 1)).replace(tzinfo=tz)
            except (ValueError, TypeError):
                current_date = None
            continue

        # Label line?
        m = LABEL_RE.match(line)
        if m:
            label, value = m.group(1).strip().lower(), m.group(2).strip()
            if label in KNOWN_LABELS:
                # A new "Case Number:" starts a new hearing block — flush prior.
                if label == "case number":
                    _flush()
                    current_block = {}
                if value:
                    current_block[KNOWN_LABELS[label]] = value
                continue

        # Otherwise, ignore — page chrome, blank lines, etc.

    _flush()  # last block

    # Dedupe by uid (the same hearing can show up in adjacent 7-day chunks).
    by_uid = {}
    for h in hearings:
        by_uid[h.uid] = h
    deduped = list(by_uid.values())
    log.info("Parsed %d unique hearings for county=%s (raw=%d)",
             len(deduped), county, len(hearings))
    return deduped


def _block_to_hearing(
    block: dict,
    current_date: Optional[datetime],
    tz: ZoneInfo,
    county: str,
) -> Optional[Hearing]:
    case_number = block.get("case_number", "").strip()
    if not case_number:
        return None
    if current_date is None:
        return None

    time_str = block.get("time", "").strip()
    try:
        # Time is "HH:MM:SS" — combine with current_date.
        if time_str:
            t = datetime.strptime(time_str, "%H:%M:%S").time()
        else:
            t = datetime.strptime("00:00:00", "%H:%M:%S").time()
        dt = datetime.combine(current_date.date(), t, tzinfo=tz)
    except ValueError as exc:
        log.warning("Bad time %r for case %s: %s", time_str, case_number, exc)
        return None

    iso = dt.isoformat()
    location_parts = [block.get("location", "").strip(), block.get("room", "").strip()]
    location = " — ".join(p for p in location_parts if p)

    raw = " | ".join(f"{k}={v}" for k, v in block.items())

    event = block.get("event", "").strip()
    return Hearing(
        uid=_uid_for(case_number, iso, event),
        case_number=case_number,
        style=block.get("style", "").strip(),
        hearing_type=event,
        datetime_iso=iso,
        location=location,
        judge=block.get("judge", "").strip(),
        county=county,
        raw_row_text=raw,
    )


def _uid_for(case_number: str, datetime_iso: str, hearing_type: str = "") -> str:
    """Stable id for one hearing. Includes hearing_type so concurrent
    events (e.g. Plea + Jury Trial at the same time) both survive dedup.
    """
    h = hashlib.sha256(f"{case_number}|{datetime_iso}|{hearing_type}".encode()).hexdigest()
    return f"casenet-{h[:16]}"


def merge(hearings_lists: Iterable[List[Hearing]]) -> List[Hearing]:
    by_uid = {}
    for lst in hearings_lists:
        for h in lst:
            by_uid[h.uid] = h
    merged = list(by_uid.values())
    merged.sort(key=lambda h: h.datetime_iso)
    return merged

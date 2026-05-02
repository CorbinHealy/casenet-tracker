"""
Parse CaseNet's Search by Attorney results HTML into structured records.

CaseNet typically returns hearings in an HTML <table>. Each row has:
  - Case Number (linked)
  - Style / Parties
  - Hearing Type
  - Date / Time
  - Location (Division / Courtroom)
  - Judge

Selectors here are intentionally permissive — we look for table-like
structures and extract by column heading, then by positional fallback.

If CaseNet ever changes their markup, the unit tests in tests/test_parser.py
will fail against the saved fixture HTML and you'll know exactly what to fix.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Iterable, List, Optional

from bs4 import BeautifulSoup, Tag  # type: ignore
from dateutil import parser as dateparser  # type: ignore

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shape — one record per scheduled hearing
# ---------------------------------------------------------------------------


@dataclass
class Hearing:
    # Stable id we compute ourselves so calendar sync is idempotent.
    uid: str
    case_number: str
    style: str               # "State of Missouri v. John Q. Defendant"
    hearing_type: str        # raw label from CaseNet, e.g. "Sentencing"
    datetime_iso: str        # ISO 8601 with offset, e.g. "2026-05-12T09:00:00-05:00"
    location: str            # "Division 16" or "Courtroom 7B"
    judge: str
    county: str              # which county query produced this row
    raw_row_text: str        # full text of the row for forensic debugging

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Column-name detection
# ---------------------------------------------------------------------------

COLUMN_ALIASES = {
    "case_number": ["case number", "case #", "case no", "case"],
    "style": ["style", "party", "parties", "title"],
    "hearing_type": ["hearing", "hearing type", "event", "event type", "type"],
    "datetime": ["date/time", "date", "scheduled", "hearing date"],
    "location": ["location", "division", "courtroom", "court"],
    "judge": ["judge", "presiding", "presiding judge"],
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _column_index(headers: List[str], aliases: List[str]) -> Optional[int]:
    normalized = [_norm(h) for h in headers]
    for alias in aliases:
        a = _norm(alias)
        for i, h in enumerate(normalized):
            if a == h or a in h:
                return i
    return None


# ---------------------------------------------------------------------------
# HTML → list[Hearing]
# ---------------------------------------------------------------------------


def parse(html: str, *, county: str, default_tz: str = "America/Chicago") -> List[Hearing]:
    """Parse CaseNet results HTML and return a list of Hearing records.

    Empty list is a valid result (no hearings scheduled). Records are not
    sorted — caller does that after merging multiple counties.
    """
    soup = BeautifulSoup(html, "lxml")
    hearings: List[Hearing] = []

    # CaseNet's results table usually has class "results" or similar.
    # We try every <table> on the page that has a header row containing
    # at least "Case" and "Date", then take the longest one.
    candidate_tables = []
    for table in soup.find_all("table"):
        header_cells = _extract_header_cells(table)
        if not header_cells:
            continue
        joined = " ".join(_norm(c) for c in header_cells)
        if "case" in joined and ("date" in joined or "time" in joined):
            candidate_tables.append((table, header_cells))

    if not candidate_tables:
        log.warning(
            "No CaseNet-shaped results table found in HTML. "
            "Returning 0 hearings. Re-run scraper with --debug to inspect."
        )
        return hearings

    # Pick the table with the most data rows.
    candidate_tables.sort(
        key=lambda pair: len(pair[0].find_all("tr")), reverse=True
    )
    table, headers = candidate_tables[0]

    indices = {
        key: _column_index(headers, aliases)
        for key, aliases in COLUMN_ALIASES.items()
    }
    if indices["case_number"] is None or indices["datetime"] is None:
        log.warning(
            "Could not locate required columns in results table. "
            "headers=%s indices=%s",
            headers, indices,
        )
        return hearings

    body_rows = _extract_body_rows(table)
    for row in body_rows:
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 2 or all(not c for c in cells):
            continue

        def col(key: str) -> str:
            idx = indices.get(key)
            return cells[idx] if idx is not None and idx < len(cells) else ""

        case_number = col("case_number")
        if not case_number:
            continue

        datetime_str = col("datetime")
        try:
            dt = dateparser.parse(datetime_str)
            # If naive, attach default tz.
            if dt.tzinfo is None:
                from zoneinfo import ZoneInfo
                dt = dt.replace(tzinfo=ZoneInfo(default_tz))
            datetime_iso = dt.isoformat()
        except (ValueError, TypeError) as exc:
            log.warning(
                "Could not parse datetime %r for case %s: %s",
                datetime_str, case_number, exc,
            )
            continue

        hearing = Hearing(
            uid=_uid_for(case_number, datetime_iso),
            case_number=case_number,
            style=col("style"),
            hearing_type=col("hearing_type"),
            datetime_iso=datetime_iso,
            location=col("location"),
            judge=col("judge"),
            county=county,
            raw_row_text=" | ".join(cells),
        )
        hearings.append(hearing)

    log.info("Parsed %d hearings for county=%s", len(hearings), county)
    return hearings


def _extract_header_cells(table: Tag) -> List[str]:
    """Find the header row of a table. CaseNet uses <th> in some places, <td> in others."""
    thead = table.find("thead")
    if thead:
        first = thead.find("tr")
        if first:
            return [c.get_text(" ", strip=True) for c in first.find_all(["th", "td"])]
    # No <thead>: assume first <tr> with any <th>, else first <tr>.
    rows = table.find_all("tr")
    if not rows:
        return []
    for r in rows:
        ths = r.find_all("th")
        if ths:
            return [c.get_text(" ", strip=True) for c in ths]
    return [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]


def _extract_body_rows(table: Tag) -> List[Tag]:
    tbody = table.find("tbody")
    rows = tbody.find_all("tr") if tbody else table.find_all("tr")
    # Drop the row we treated as the header (whichever is first non-empty).
    out = []
    seen_header = False
    for r in rows:
        cells = r.find_all(["td", "th"])
        if not cells:
            continue
        if not seen_header:
            seen_header = True
            # Skip if the row is all <th> (definitely a header).
            if all(c.name == "th" for c in cells):
                continue
        out.append(r)
    return out


def _uid_for(case_number: str, datetime_iso: str) -> str:
    """Deterministic id for one hearing. Used as the calendar event id key."""
    h = hashlib.sha256(f"{case_number}|{datetime_iso}".encode()).hexdigest()
    return f"casenet-{h[:16]}"


def merge(hearings_lists: Iterable[List[Hearing]]) -> List[Hearing]:
    """Merge results from multiple county scrapes; dedupe by uid; sort by datetime."""
    by_uid = {}
    for lst in hearings_lists:
        for h in lst:
            by_uid[h.uid] = h
    merged = list(by_uid.values())
    merged.sort(key=lambda h: h.datetime_iso)
    return merged

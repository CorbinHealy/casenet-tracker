"""
Diff today's parsed docket against the last-saved state to tag each hearing
as new / unchanged / moved / cancelled.

State file shape (state/docket.json):
    {
      "schema_version": 1,
      "last_run_utc": "2026-05-02T11:00:03+00:00",
      "hearings": [ <Hearing.as_dict()> ... ]
    }
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .parser import Hearing

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class DiffResult:
    new: List[Hearing] = field(default_factory=list)
    unchanged: List[Hearing] = field(default_factory=list)
    moved: List[tuple] = field(default_factory=list)       # (old, new)
    cancelled: List[Hearing] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"new={len(self.new)} moved={len(self.moved)} "
            f"cancelled={len(self.cancelled)} unchanged={len(self.unchanged)}"
        )


def load_state(state_path: Path) -> List[Hearing]:
    if not state_path.exists():
        return []
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.warning("State file %s is corrupt (%s); treating as empty.", state_path, exc)
        return []
    if data.get("schema_version") != SCHEMA_VERSION:
        log.warning(
            "State schema_version=%s != %s; treating as empty.",
            data.get("schema_version"), SCHEMA_VERSION,
        )
        return []
    return [Hearing(**h) for h in data.get("hearings", [])]


def save_state(state_path: Path, hearings: List[Hearing]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "last_run_utc": datetime.now(timezone.utc).isoformat(),
        "hearings": [h.as_dict() for h in hearings],
    }
    state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def diff(previous: List[Hearing], current: List[Hearing]) -> DiffResult:
    """Tag each current hearing relative to the previous run.

    Identity for "same hearing" is the case_number — we expect any given case
    to have at most one upcoming hearing visible at a time. If the datetime
    differs across runs, we tag it as "moved". If a previous case_number is
    no longer present, we tag it as "cancelled".

    Note: this also catches *completed* hearings as "cancelled", which is
    accurate from the user's standpoint — the hearing is no longer pending.
    """
    prev_by_case: Dict[str, Hearing] = {}
    for h in previous:
        # If prior state had multiple rows for one case, keep the soonest.
        existing = prev_by_case.get(h.case_number)
        if existing is None or h.datetime_iso < existing.datetime_iso:
            prev_by_case[h.case_number] = h

    cur_by_case: Dict[str, Hearing] = {}
    for h in current:
        existing = cur_by_case.get(h.case_number)
        if existing is None or h.datetime_iso < existing.datetime_iso:
            cur_by_case[h.case_number] = h

    result = DiffResult()
    for case, h in cur_by_case.items():
        prior = prev_by_case.get(case)
        if prior is None:
            result.new.append(h)
        elif prior.datetime_iso != h.datetime_iso or prior.location != h.location:
            result.moved.append((prior, h))
        else:
            result.unchanged.append(h)

    for case, h in prev_by_case.items():
        if case not in cur_by_case:
            result.cancelled.append(h)

    log.info("Diff: %s", result.summary())
    return result

"""
Apply prep_rules.yaml to a list of hearings and produce per-hearing flags.

A "flag" is just a small dict:
    {"label": "TRIAL", "days_until": 14, "trigger_at_days": 14}

The dashboard and email digest sort flagged items to the top.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import yaml  # type: ignore
from dateutil import parser as dateparser  # type: ignore

from .parser import Hearing

log = logging.getLogger(__name__)


@dataclass
class Flag:
    label: str
    days_until: int
    trigger_at_days: int  # which prep-rule day-out triggered this

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "days_until": self.days_until,
            "trigger_at_days": self.trigger_at_days,
        }


@dataclass
class FlaggedHearing:
    hearing: Hearing
    flags: List[Flag]
    # The most-imminent prep flag, or None. UI uses this for badges.
    primary_flag: Optional[Flag]
    # True if a rule matched but we're not yet within any flag window.
    has_rule_match: bool

    @property
    def is_today(self) -> bool:
        dt = dateparser.parse(self.hearing.datetime_iso)
        return dt.date() == datetime.now(dt.tzinfo).date()

    def as_dict(self) -> dict:
        return {
            "hearing": self.hearing.as_dict(),
            "flags": [f.as_dict() for f in self.flags],
            "primary_flag": self.primary_flag.as_dict() if self.primary_flag else None,
            "has_rule_match": self.has_rule_match,
        }


def load_rules(rules_path: Path) -> list:
    rules = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
    if not isinstance(rules, list):
        raise ValueError(f"prep_rules.yaml must be a list at top level; got {type(rules)}")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError(f"Each rule must be a dict; got {rule!r}")
        if "match" not in rule:
            raise ValueError(f"Rule missing 'match': {rule!r}")
        if "flag_at_days" not in rule:
            raise ValueError(f"Rule missing 'flag_at_days': {rule!r}")
        # Normalize.
        rule["match"] = [m.lower() for m in rule["match"]]
        rule["flag_at_days"] = sorted(rule["flag_at_days"], reverse=True)
        rule.setdefault("label", rule["match"][0].title())
    return rules


def apply(hearings: List[Hearing], rules: list, *, now: Optional[datetime] = None) -> List[FlaggedHearing]:
    if now is None:
        now = datetime.now(timezone.utc)

    flagged: List[FlaggedHearing] = []
    for h in hearings:
        dt = dateparser.parse(h.datetime_iso)
        # Convert to same tz as `now` for consistent date math.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = dt.date() - now.astimezone(dt.tzinfo).date()
        days_until = delta.days

        rule = _first_matching_rule(h.hearing_type, rules)
        flags: List[Flag] = []
        if rule is not None:
            for trigger in rule["flag_at_days"]:
                if days_until <= trigger and days_until >= 0:
                    flags.append(Flag(
                        label=rule["label"],
                        days_until=days_until,
                        trigger_at_days=trigger,
                    ))

        primary_flag = min(flags, key=lambda f: f.days_until) if flags else None
        flagged.append(FlaggedHearing(
            hearing=h,
            flags=flags,
            primary_flag=primary_flag,
            has_rule_match=rule is not None,
        ))
    return flagged


def _first_matching_rule(hearing_type: str, rules: list) -> Optional[dict]:
    label = (hearing_type or "").lower()
    for rule in rules:
        for m in rule["match"]:
            if m in label:
                return rule
    return None

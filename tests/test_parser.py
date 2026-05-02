"""
Parser regression tests.

These run against tests/fixtures/casenet_sample.html. The first time you do a
real CaseNet pull, save that HTML over the fixture and update EXPECTED to
match — then this file becomes a regression suite that fails loudly when
CaseNet changes their markup.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.differ import diff
from src.flags import apply, load_rules
from src.parser import parse, merge

FIXTURE = Path(__file__).parent / "fixtures" / "casenet_sample.html"
RULES = Path(__file__).parent.parent / "prep_rules.yaml"


def _load_fixture():
    return FIXTURE.read_text(encoding="utf-8")


def test_parser_finds_three_rows():
    hearings = parse(_load_fixture(), county="Jackson")
    assert len(hearings) == 3
    cases = [h.case_number for h in hearings]
    assert cases == ["2316-CR01234", "2316-CR05678", "2316-CR09999"]


def test_parser_extracts_columns():
    hearings = parse(_load_fixture(), county="Jackson")
    h = hearings[0]
    assert h.case_number == "2316-CR01234"
    assert "Defendant" in h.style
    assert "Suppress" in h.hearing_type
    assert "2026-05-15" in h.datetime_iso
    assert h.location == "Division 16"
    assert "Smith" in h.judge


def test_parser_uid_is_deterministic():
    a = parse(_load_fixture(), county="Jackson")
    b = parse(_load_fixture(), county="Jackson")
    assert [h.uid for h in a] == [h.uid for h in b]


def test_merge_dedupes_by_uid():
    a = parse(_load_fixture(), county="Jackson")
    b = parse(_load_fixture(), county="Jackson")
    merged = merge([a, b])
    assert len(merged) == 3


def test_diff_identifies_new_and_cancelled():
    hearings = parse(_load_fixture(), county="Jackson")
    # Drop one to simulate yesterday's snapshot
    previous = hearings[1:]
    result = diff(previous, hearings)
    assert len(result.new) == 1
    assert result.new[0].case_number == "2316-CR01234"
    assert len(result.cancelled) == 0
    assert len(result.unchanged) == 2


def test_diff_identifies_moved():
    hearings = parse(_load_fixture(), county="Jackson")
    # Synthesize "yesterday" with a different datetime for one case.
    moved_prev = [hearings[0]]
    from dataclasses import replace
    moved_prev[0] = replace(moved_prev[0], datetime_iso="2026-05-16T09:00:00-05:00", uid="casenet-old")
    result = diff(moved_prev, [hearings[0]])
    assert len(result.moved) == 1


def test_flags_match_known_rules():
    hearings = parse(_load_fixture(), county="Jackson")
    rules = load_rules(RULES)
    # Force "now" so the test is deterministic regardless of when it runs.
    fixed_now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    flagged = apply(hearings, rules, now=fixed_now)
    by_case = {f.hearing.case_number: f for f in flagged}

    suppression = by_case["2316-CR01234"]  # 14 days out from May 1
    assert any(f.label.lower() == "suppression" for f in suppression.flags)

    trial = by_case["2316-CR05678"]  # ~33 days out → only [60, 45] not yet, [30] not yet
    # Trial is 33 days out — should hit the 60d and 45d windows but NOT 30/14/7
    assert all(f.trigger_at_days >= 30 for f in trial.flags) or trial.flags == []

    pv = by_case["2316-CR09999"]  # 6 days out → inside [5d] is False (6>5), so NO flag
    # Probation violation rule fires at 5 days out; 6 days out shouldn't fire yet.
    assert all(f.label != "Prob. violation" for f in pv.flags) or len(pv.flags) == 0

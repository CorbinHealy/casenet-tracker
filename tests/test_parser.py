"""
Parser regression tests.

Run against tests/fixtures/casenet_sample.html, which mirrors the labeled-block
format CaseNet's calendarSearchResult.do returns. If CaseNet ever changes the
output format, these tests will fail loudly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.differ import diff
from src.flags import apply, load_rules
from src.parser import parse, merge

FIXTURE = Path(__file__).parent / "fixtures" / "casenet_sample.html"
RULES = Path(__file__).parent.parent / "prep_rules.yaml"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parser_finds_all_hearings():
    hearings = parse(_load_fixture(), county="Jackson")
    # 5 events: HILL has Plea + Jury Trial at the same time (both kept since
    # uid includes hearing_type), FOSTER, JOHNSON, ADAMS.
    assert len(hearings) == 5
    cases = sorted({h.case_number for h in hearings})
    assert "2516-CR02406-01" in cases
    assert "2516-CR04034-01" in cases
    assert "2316-CR04724-01" in cases
    assert "2616-CR01176-01" in cases


def test_parser_extracts_columns():
    hearings = parse(_load_fixture(), county="Jackson")
    by_case = {h.case_number: h for h in hearings}
    foster = by_case["2516-CR04034-01"]
    assert foster.style == "ST V HARRY P FOSTER"
    assert "Independence" in foster.location
    assert "DIVISION 50" in foster.location
    assert foster.hearing_type == "Hearing"
    assert foster.datetime_iso.startswith("2026-05-04T10:30:00")


def test_parser_handles_day_header_grouping():
    hearings = parse(_load_fixture(), county="Jackson")
    by_case = {h.case_number: h for h in hearings}
    # Tuesday header should attach the right date to JOHNSON.
    johnson = by_case["2316-CR04724-01"]
    assert johnson.datetime_iso.startswith("2026-05-05T13:00:00")


def test_parser_uid_is_deterministic():
    a = parse(_load_fixture(), county="Jackson")
    b = parse(_load_fixture(), county="Jackson")
    assert sorted(h.uid for h in a) == sorted(h.uid for h in b)


def test_merge_dedupes_by_uid():
    a = parse(_load_fixture(), county="Jackson")
    b = parse(_load_fixture(), county="Jackson")
    merged = merge([a, b])
    assert len(merged) == len(a)


def test_diff_identifies_new_and_cancelled():
    hearings = parse(_load_fixture(), county="Jackson")
    previous = hearings[1:]
    result = diff(previous, hearings)
    assert any(h.uid == hearings[0].uid for h in result.new)
    assert len(result.cancelled) == 0


def test_flags_match_known_rules():
    hearings = parse(_load_fixture(), county="Jackson")
    rules = load_rules(RULES)
    fixed_now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    flagged = apply(hearings, rules, now=fixed_now)
    by_case = {f.hearing.case_number: f for f in flagged}

    # Pre-trial Conference for ADAMS, 4 days out → inside the [7, 3] window
    # at the 7-day trigger only.
    adams = by_case["2616-CR01176-01"]
    assert any("pre-trial" in f.label.lower() or "ptc" in f.label.lower() for f in adams.flags) \
        or any(f.trigger_at_days == 7 for f in adams.flags)

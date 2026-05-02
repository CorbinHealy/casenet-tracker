"""
Daily HTML email digest via Gmail SMTP.

Auth: a Gmail App Password (not your real password). See README setup §4.
Env vars expected:
    GMAIL_USER, GMAIL_APP_PASSWORD

The digest is structured for phone reading — short subject, key items at top.
"""
from __future__ import annotations

import logging
import os
import smtplib
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import List, Optional

from dateutil import parser as dateparser  # type: ignore

from .differ import DiffResult
from .flags import FlaggedHearing
from .parser import Hearing

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def send(
    *,
    config: dict,
    flagged: List[FlaggedHearing],
    diff: DiffResult,
    failed_counties: Optional[list] = None,
) -> None:
    failed_counties = failed_counties or []
    if config["notify"].get("skip_email_on_empty_days") and not flagged and not failed_counties:
        log.info("Skipping email: nothing to report.")
        return

    msg = EmailMessage()
    msg["Subject"] = _subject(flagged, diff, failed_counties, config)
    msg["From"] = os.environ["GMAIL_USER"]
    msg["To"] = config["notify"]["email_to"]

    msg.set_content(_render_text(flagged, diff, failed_counties, config))
    msg.add_alternative(_render_html(flagged, diff, failed_counties, config), subtype="html")

    _send_smtp(msg)


def send_failure(*, config: dict, errors: list) -> None:
    """Send a 'manual CaseNet check required' email when scraping completely fails."""
    msg = EmailMessage()
    msg["Subject"] = f"{config['notify']['email_subject_prefix']} Manual CaseNet check required"
    msg["From"] = os.environ["GMAIL_USER"]
    msg["To"] = config["notify"]["email_to"]
    body = (
        "The CaseNet scraper failed to retrieve hearings today.\n\n"
        "Errors:\n"
    )
    for county, err in errors:
        body += f"  • {county}: {err}\n"
    body += "\nLog into CaseNet manually and check the docket for today and tomorrow.\n"
    msg.set_content(body)
    _send_smtp(msg)


# ---------------------------------------------------------------------------
# Subject line
# ---------------------------------------------------------------------------


def _subject(
    flagged: List[FlaggedHearing],
    diff: DiffResult,
    failed_counties: list,
    config: dict,
) -> str:
    prefix = config["notify"]["email_subject_prefix"]
    today = datetime.now().strftime("%a %b %d")

    today_count = sum(1 for f in flagged if f.is_today)
    flagged_count = sum(1 for f in flagged if f.flags)
    new_count = len(diff.new)
    moved_count = len(diff.moved)
    cancelled_count = len(diff.cancelled)

    bits = []
    if today_count:
        bits.append(f"{today_count} today")
    if flagged_count:
        bits.append(f"{flagged_count} flagged")
    if new_count:
        bits.append(f"{new_count} new")
    if moved_count:
        bits.append(f"{moved_count} moved")
    if cancelled_count:
        bits.append(f"{cancelled_count} off")
    if failed_counties:
        bits.append("⚠ partial")

    summary = " · ".join(bits) if bits else "all clear"
    return f"{prefix} {today} — {summary}"


# ---------------------------------------------------------------------------
# Plain-text body
# ---------------------------------------------------------------------------


def _render_text(
    flagged: List[FlaggedHearing],
    diff: DiffResult,
    failed_counties: list,
    config: dict,
) -> str:
    lines: List[str] = []

    if failed_counties:
        lines.append("⚠ Some counties failed today. See below.\n")
        for county, err in failed_counties:
            lines.append(f"  • {county}: {err}")
        lines.append("")

    today_items = sorted([f for f in flagged if f.is_today], key=lambda f: f.hearing.datetime_iso)
    if today_items:
        lines.append("=== TODAY ===")
        for f in today_items:
            lines.append(_text_line(f))
        lines.append("")

    # Flagged items inside any prep window, excluding today.
    flag_items = sorted(
        [f for f in flagged if f.flags and not f.is_today],
        key=lambda f: (f.primary_flag.days_until if f.primary_flag else 999, f.hearing.datetime_iso),
    )
    if flag_items:
        lines.append("=== PREP WINDOW ===")
        for f in flag_items:
            lines.append(_text_line(f))
        lines.append("")

    # This week (next 7 days), not already shown.
    shown_uids = {f.hearing.uid for f in today_items} | {f.hearing.uid for f in flag_items}
    this_week = [
        f for f in flagged
        if f.hearing.uid not in shown_uids and _within_days(f.hearing, 7)
    ]
    this_week.sort(key=lambda f: f.hearing.datetime_iso)
    if this_week:
        lines.append("=== THIS WEEK ===")
        for f in this_week:
            lines.append(_text_line(f))
        lines.append("")

    # Diff section.
    if diff.new:
        lines.append("=== ADDED SINCE LAST RUN ===")
        for h in diff.new:
            lines.append(f"  + {h.case_number}  {h.hearing_type}  {_fmt_dt(h.datetime_iso)}  {h.location}")
        lines.append("")
    if diff.moved:
        lines.append("=== MOVED SINCE LAST RUN ===")
        for old, new in diff.moved:
            lines.append(
                f"  ≠ {new.case_number}  {new.hearing_type}  "
                f"{_fmt_dt(old.datetime_iso)} → {_fmt_dt(new.datetime_iso)}"
            )
        lines.append("")
    if diff.cancelled:
        lines.append("=== REMOVED SINCE LAST RUN ===")
        for h in diff.cancelled:
            lines.append(f"  − {h.case_number}  {h.hearing_type}  was {_fmt_dt(h.datetime_iso)}")
        lines.append("")

    if not lines:
        lines.append("No hearings on the docket.")

    return "\n".join(lines)


def _text_line(f: FlaggedHearing) -> str:
    flag = f"[{f.primary_flag.label} · {f.primary_flag.days_until}d]" if f.primary_flag else ""
    return (
        f"  {_fmt_dt(f.hearing.datetime_iso)}  "
        f"{f.hearing.case_number}  "
        f"{f.hearing.hearing_type}  "
        f"{f.hearing.location}  "
        f"{flag}"
    )


# ---------------------------------------------------------------------------
# HTML body
# ---------------------------------------------------------------------------


def _render_html(
    flagged: List[FlaggedHearing],
    diff: DiffResult,
    failed_counties: list,
    config: dict,
) -> str:
    today_items = sorted([f for f in flagged if f.is_today], key=lambda f: f.hearing.datetime_iso)
    flag_items = sorted(
        [f for f in flagged if f.flags and not f.is_today],
        key=lambda f: (f.primary_flag.days_until if f.primary_flag else 999, f.hearing.datetime_iso),
    )
    shown_uids = {f.hearing.uid for f in today_items} | {f.hearing.uid for f in flag_items}
    this_week = [
        f for f in flagged
        if f.hearing.uid not in shown_uids and _within_days(f.hearing, 7)
    ]
    this_week.sort(key=lambda f: f.hearing.datetime_iso)

    upcoming = [
        f for f in flagged
        if f.hearing.uid not in shown_uids
        and not _within_days(f.hearing, 7)
        and _within_days(f.hearing, config["dashboard"].get("upcoming_window_days", 30))
    ]
    upcoming.sort(key=lambda f: f.hearing.datetime_iso)

    parts: List[str] = []
    parts.append(_html_header())

    if failed_counties:
        parts.append("<div class='warn'><b>⚠ Partial failure today</b><ul>")
        for county, err in failed_counties:
            parts.append(f"<li><b>{county}:</b> {_esc(err)}</li>")
        parts.append("</ul></div>")

    parts.append(_html_section("Today", today_items, badge="today"))
    parts.append(_html_section("Prep window", flag_items, badge="prep"))
    parts.append(_html_section("This week", this_week))
    parts.append(_html_section("Upcoming (30 days)", upcoming))

    if diff.new or diff.moved or diff.cancelled:
        parts.append("<h3>Changes since last run</h3><ul>")
        for h in diff.new:
            parts.append(f"<li>➕ <b>{_esc(h.case_number)}</b> — {_esc(h.hearing_type)} on {_fmt_dt(h.datetime_iso)} ({_esc(h.location)})</li>")
        for old, new in diff.moved:
            parts.append(
                f"<li>↔ <b>{_esc(new.case_number)}</b> moved: "
                f"{_fmt_dt(old.datetime_iso)} → <b>{_fmt_dt(new.datetime_iso)}</b></li>"
            )
        for h in diff.cancelled:
            parts.append(f"<li>➖ <b>{_esc(h.case_number)}</b> removed (was {_esc(h.hearing_type)} on {_fmt_dt(h.datetime_iso)})</li>")
        parts.append("</ul>")

    parts.append(_html_footer())
    return "\n".join(parts)


def _html_header() -> str:
    return """<!doctype html>
<html><head><meta charset="utf-8">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; color: #1d1d1f; max-width: 720px; margin: 0 auto; padding: 16px; }
  h2 { margin: 24px 0 8px; padding-bottom: 4px; border-bottom: 1px solid #e5e5ea; font-size: 16px; }
  h3 { margin: 24px 0 8px; font-size: 14px; color: #6e6e73; }
  ul { padding-left: 16px; }
  li { margin: 4px 0; }
  table { border-collapse: collapse; width: 100%; margin-top: 4px; font-size: 14px; }
  td { padding: 6px 8px; border-bottom: 1px solid #f0f0f5; vertical-align: top; }
  td.dt { white-space: nowrap; color: #1d1d1f; font-variant-numeric: tabular-nums; }
  td.case { color: #007aff; font-family: ui-monospace, monospace; font-size: 13px; }
  td.type { font-weight: 600; }
  td.loc { color: #6e6e73; }
  .badge { display: inline-block; font-size: 11px; padding: 2px 6px; border-radius: 4px; margin-left: 6px; }
  .badge-today { background: #ff3b30; color: white; }
  .badge-prep { background: #ff9500; color: white; }
  .badge-default { background: #e5e5ea; color: #1d1d1f; }
  .warn { background: #fff5e5; border-left: 4px solid #ff9500; padding: 8px 12px; margin: 12px 0; }
</style></head><body>"""


def _html_section(title: str, items: List[FlaggedHearing], *, badge: str = "default") -> str:
    if not items:
        return ""
    out = [f"<h2>{_esc(title)}</h2><table>"]
    for f in items:
        h = f.hearing
        flag_html = ""
        if f.primary_flag:
            cls = f"badge-{badge if badge != 'default' else 'prep'}"
            flag_html = f"<span class='badge {cls}'>{_esc(f.primary_flag.label)} · {f.primary_flag.days_until}d</span>"
        out.append(
            "<tr>"
            f"<td class='dt'>{_fmt_dt(h.datetime_iso)}</td>"
            f"<td class='case'>{_esc(h.case_number)}</td>"
            f"<td class='type'>{_esc(h.hearing_type)}{flag_html}</td>"
            f"<td>{_esc(h.style)}</td>"
            f"<td class='loc'>{_esc(h.location)}{(' · ' + _esc(h.judge)) if h.judge else ''}</td>"
            "</tr>"
        )
    out.append("</table>")
    return "\n".join(out)


def _html_footer() -> str:
    return f"""<p style='color:#8e8e93;font-size:11px;margin-top:32px'>
    Generated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")} by casenet-tracker.
    </p></body></html>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _within_days(h: Hearing, days: int) -> bool:
    dt = dateparser.parse(h.datetime_iso)
    delta = dt.date() - datetime.now(dt.tzinfo).date()
    return 0 <= delta.days <= days


def _fmt_dt(iso: str) -> str:
    dt = dateparser.parse(iso)
    return dt.strftime("%a %b %-d, %-I:%M %p")


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _send_smtp(msg: EmailMessage) -> None:
    user = os.environ["GMAIL_USER"]
    pw = os.environ["GMAIL_APP_PASSWORD"]
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(user, pw)
        server.send_message(msg)
    log.info("Email sent to %s", msg["To"])

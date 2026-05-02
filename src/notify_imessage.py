"""
Send the morning summary as an iMessage to yourself via Messages.app.

Recipient is read from config.yaml -> notify.imessage_to. Format is either
+15555551234 (phone), an Apple ID email, or a contact name.

The first run will trigger a permission prompt to control Messages.
"""
from __future__ import annotations

import logging
import subprocess
from typing import List, Optional

from dateutil import parser as dateparser  # type: ignore

from .differ import DiffResult
from .flags import FlaggedHearing

log = logging.getLogger(__name__)

MAX_HEADLINES = 4


def send(
    *,
    config: dict,
    flagged: List[FlaggedHearing],
    diff: Optional[DiffResult] = None,
) -> None:
    recipient = config["notify"].get("imessage_to")
    if not recipient:
        log.info("iMessage skipped — notify.imessage_to not set in config.yaml.")
        return

    body = _compose(flagged, diff)
    if not body.strip():
        log.info("iMessage skipped — no content to send.")
        return

    _send_via_applescript(recipient, body)
    log.info("iMessage sent to %s (%d chars)", recipient, len(body))


def _compose(flagged: List[FlaggedHearing], diff: Optional[DiffResult]) -> str:
    today_items = [f for f in flagged if f.is_today]
    flag_items = [f for f in flagged if f.flags and not f.is_today]

    today_count = len(today_items)
    flag_count = len(flag_items)

    if not today_count and not flag_count:
        return "CaseNet: clear today."

    summary_bits = []
    if today_count:
        summary_bits.append(f"{today_count} today")
    if flag_count:
        summary_bits.append(f"{flag_count} flagged")
    summary = "CaseNet: " + ", ".join(summary_bits) + "."

    headlines = []
    items = sorted(today_items + flag_items,
                   key=lambda f: f.hearing.datetime_iso)
    for f in items[:MAX_HEADLINES]:
        h = f.hearing
        dt = dateparser.parse(h.datetime_iso)
        time_str = dt.strftime("%a %-I:%M%p").lower()
        flag_tag = ""
        if f.primary_flag:
            flag_tag = f" [{f.primary_flag.label} {f.primary_flag.days_until}d]"
        headlines.append(f"  {time_str}  {h.case_number}  {h.hearing_type}{flag_tag}")

    extra_count = len(items) - MAX_HEADLINES
    if extra_count > 0:
        headlines.append(f"  +{extra_count} more — see dashboard")

    return summary + "\n" + "\n".join(headlines)


def _send_via_applescript(recipient: str, body: str) -> None:
    safe_recipient = recipient.replace('"', '\\"')
    safe_body = body.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
    script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{safe_recipient}" of targetService
        send "{safe_body}" to targetBuddy
    end tell
    '''
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"iMessage send failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

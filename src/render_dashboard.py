"""
Render docs/index.html and docs/digest.json for GitHub Pages.

The dashboard is a single static page — no server, no build step, no auth.
The HTML embeds the data inline so it works offline once loaded.

digest.json is what the Mac iMessage agent fetches each morning.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

from dateutil import parser as dateparser  # type: ignore

from .differ import DiffResult
from .flags import FlaggedHearing

log = logging.getLogger(__name__)


def render(
    *,
    config: dict,
    flagged: List[FlaggedHearing],
    diff: DiffResult,
    out_dir: Path,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = _build_payload(flagged, diff, config)
    (out_dir / "digest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    html = _render_html(payload, config)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    log.info("Dashboard written to %s", out_dir)


def _build_payload(flagged: List[FlaggedHearing], diff: DiffResult, config: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "generated_at_utc": now,
        "title": config["dashboard"]["title"],
        "hearings": [f.as_dict() for f in flagged],
        "diff": {
            "new": [h.as_dict() for h in diff.new],
            "moved": [{"old": old.as_dict(), "new": new.as_dict()} for old, new in diff.moved],
            "cancelled": [h.as_dict() for h in diff.cancelled],
        },
        "imessage_summary": _imessage_summary(flagged),
    }


def _imessage_summary(flagged: List[FlaggedHearing]) -> str:
    """One-line summary for the Mac iMessage agent."""
    today = sum(1 for f in flagged if f.is_today)
    flagged_n = sum(1 for f in flagged if f.flags and not f.is_today)
    if not today and not flagged_n:
        return "CaseNet: clear today."
    bits = []
    if today:
        bits.append(f"{today} today")
    if flagged_n:
        bits.append(f"{flagged_n} flagged")
    return f"CaseNet: {', '.join(bits)}."


def _render_html(payload: dict, config: dict) -> str:
    title = payload["title"]
    data_json = json.dumps(payload).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>
    :root {{
      --bg: #fafafa; --fg: #1d1d1f; --muted: #6e6e73; --line: #e5e5ea;
      --accent: #007aff; --warn: #ff9500; --danger: #ff3b30; --ok: #34c759;
    }}
    @media (prefers-color-scheme: dark) {{
      :root {{ --bg: #1c1c1e; --fg: #f2f2f7; --muted: #8e8e93; --line: #2c2c2e; }}
    }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: var(--bg); color: var(--fg); margin: 0; padding: 16px;
            max-width: 900px; margin-left: auto; margin-right: auto; }}
    header {{ display: flex; justify-content: space-between; align-items: baseline;
              border-bottom: 1px solid var(--line); padding-bottom: 12px; margin-bottom: 16px; }}
    h1 {{ font-size: 20px; margin: 0; }}
    .ts {{ color: var(--muted); font-size: 12px; }}
    .filters {{ display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }}
    .filters input, .filters select {{
      font: inherit; padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px;
      background: var(--bg); color: var(--fg); }}
    h2 {{ font-size: 14px; color: var(--muted); text-transform: uppercase;
           letter-spacing: 0.06em; margin: 24px 0 6px; font-weight: 600; }}
    .card {{ border: 1px solid var(--line); border-radius: 10px; padding: 12px;
             margin: 6px 0; display: grid; grid-template-columns: 110px 1fr auto; gap: 10px;
             align-items: center; }}
    .card .dt {{ font-variant-numeric: tabular-nums; color: var(--fg); font-weight: 600; font-size: 13px; }}
    .card .meta {{ overflow: hidden; }}
    .card .case {{ font-family: ui-monospace, monospace; color: var(--accent); font-size: 12px; }}
    .card .style {{ font-size: 14px; margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .card .type {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
    .card .loc {{ text-align: right; font-size: 12px; color: var(--muted); white-space: nowrap; }}
    .badge {{ display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 11px;
              margin-left: 6px; vertical-align: 1px; }}
    .badge-today {{ background: var(--danger); color: white; }}
    .badge-prep {{ background: var(--warn); color: white; }}
    .badge-soon {{ background: var(--accent); color: white; }}
    .empty {{ color: var(--muted); padding: 12px 0; font-size: 14px; }}
    .diff {{ margin-top: 24px; padding: 12px; background: rgba(255,149,0,0.08);
              border-left: 4px solid var(--warn); border-radius: 4px; }}
    .diff h3 {{ margin: 0 0 6px; font-size: 13px; color: var(--warn); }}
    .diff li {{ font-size: 13px; }}
    @media (max-width: 600px) {{
      .card {{ grid-template-columns: 90px 1fr; }}
      .card .loc {{ grid-column: 1 / -1; text-align: left; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{_esc(title)}</h1>
    <div class="ts" id="ts"></div>
  </header>

  <div class="filters">
    <input id="q" placeholder="Filter by case # or party" />
    <select id="window">
      <option value="all">All upcoming</option>
      <option value="1">Today only</option>
      <option value="7" selected>Next 7 days</option>
      <option value="30">Next 30 days</option>
    </select>
    <select id="flagOnly">
      <option value="all">All hearings</option>
      <option value="flagged">Flagged only</option>
    </select>
  </div>

  <div id="root"></div>

  <script id="data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('data').textContent);
    document.getElementById('ts').textContent =
      'Last updated ' + new Date(data.generated_at_utc).toLocaleString();

    function fmt(iso) {{
      const d = new Date(iso);
      return d.toLocaleString(undefined, {{
        weekday: 'short', month: 'short', day: 'numeric',
        hour: 'numeric', minute: '2-digit'
      }});
    }}

    function dayDelta(iso) {{
      const d = new Date(iso);
      const now = new Date();
      const a = new Date(d.getFullYear(), d.getMonth(), d.getDate());
      const b = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      return Math.round((a - b) / 86400000);
    }}

    function render() {{
      const q = document.getElementById('q').value.toLowerCase();
      const win = document.getElementById('window').value;
      const flagOnly = document.getElementById('flagOnly').value === 'flagged';

      const items = data.hearings.filter(f => {{
        const h = f.hearing;
        const dd = dayDelta(h.datetime_iso);
        if (dd < 0) return false;
        if (win !== 'all' && dd > parseInt(win, 10) - (win === '1' ? 1 : 0)) return false;
        if (win === '1' && dd !== 0) return false;
        if (flagOnly && !f.flags.length) return false;
        if (q) {{
          const blob = (h.case_number + ' ' + h.style + ' ' + h.hearing_type + ' ' + h.location + ' ' + h.judge).toLowerCase();
          if (!blob.includes(q)) return false;
        }}
        return true;
      }});

      const today = items.filter(f => dayDelta(f.hearing.datetime_iso) === 0);
      const flagged = items.filter(f => f.primary_flag && dayDelta(f.hearing.datetime_iso) !== 0);
      const shown = new Set([...today, ...flagged].map(f => f.hearing.uid));
      const rest = items.filter(f => !shown.has(f.hearing.uid));

      const root = document.getElementById('root');
      root.innerHTML = '';

      function section(title, list, badgeKind) {{
        if (!list.length) return;
        const h2 = document.createElement('h2');
        h2.textContent = title;
        root.appendChild(h2);
        list.forEach(f => root.appendChild(card(f, badgeKind)));
      }}

      section('Today', today, 'today');
      section('Prep window', flagged, 'prep');
      section('Upcoming', rest, 'soon');

      if (!items.length) {{
        const empty = document.createElement('div');
        empty.className = 'empty';
        empty.textContent = 'No hearings match your filters.';
        root.appendChild(empty);
      }}

      // Render diff banner.
      const d = data.diff;
      if (d.new.length || d.moved.length || d.cancelled.length) {{
        const wrap = document.createElement('div');
        wrap.className = 'diff';
        wrap.innerHTML = '<h3>Changes since last run</h3><ul></ul>';
        const ul = wrap.querySelector('ul');
        d.new.forEach(h => ul.insertAdjacentHTML('beforeend', `<li>+ ${{h.case_number}} — ${{h.hearing_type}} on ${{fmt(h.datetime_iso)}}</li>`));
        d.moved.forEach(p => ul.insertAdjacentHTML('beforeend', `<li>↔ ${{p.new.case_number}} moved: ${{fmt(p.old.datetime_iso)}} → ${{fmt(p.new.datetime_iso)}}</li>`));
        d.cancelled.forEach(h => ul.insertAdjacentHTML('beforeend', `<li>− ${{h.case_number}} removed (was ${{fmt(h.datetime_iso)}})</li>`));
        root.appendChild(wrap);
      }}
    }}

    function card(f, badgeKind) {{
      const h = f.hearing;
      const dd = dayDelta(h.datetime_iso);
      const div = document.createElement('div');
      div.className = 'card';
      const flagBadge = f.primary_flag
        ? `<span class="badge badge-${{badgeKind || 'prep'}}">${{f.primary_flag.label}} · ${{f.primary_flag.days_until}}d</span>`
        : '';
      div.innerHTML = `
        <div class="dt">${{fmt(h.datetime_iso)}}</div>
        <div class="meta">
          <div class="case">${{h.case_number}}</div>
          <div class="style">${{h.style || ''}}</div>
          <div class="type">${{h.hearing_type}}${{flagBadge}}</div>
        </div>
        <div class="loc">${{h.location}}${{h.judge ? ' · ' + h.judge : ''}}</div>
      `;
      return div;
    }}

    document.getElementById('q').addEventListener('input', render);
    document.getElementById('window').addEventListener('change', render);
    document.getElementById('flagOnly').addEventListener('change', render);
    render();
  </script>
</body></html>
"""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

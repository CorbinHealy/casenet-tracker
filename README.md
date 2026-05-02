# CaseNet Tracker

Daily pull of Missouri CaseNet's Scheduled Hearings & Trials by Attorney for
one MOBAR number, with prep-flag rules and three delivery channels:

- **Apple Calendar** — events sync to your iPhone via iCloud
- **iMessage** — one-line summary at 6 AM
- **Static dashboard** — published to GitHub Pages, bookmarkable

The job runs **locally on your Mac at 6:00 AM** via a `launchd` agent. Why
not the cloud: CaseNet blocks GitHub Actions / cloud IP ranges with HTTP 403.
Your home/personal IP gets through cleanly.

---

## How it runs

```
Your Mac (LaunchAgent at 6:00 AM)
  └─ run.sh
       └─ python -m src.main
            ├─ scraper.py     → Playwright Chromium → CaseNet (7-day chunks)
            ├─ parser.py      → labeled-block text → Hearing records
            ├─ differ.py      → compare to state/docket.json
            ├─ flags.py       → apply prep_rules.yaml
            ├─ render_dashboard.py  → docs/index.html + docs/digest.json
            ├─ notify_apple_calendar.py  → osascript → Calendar.app → iCloud
            ├─ notify_imessage.py        → osascript → Messages.app
            └─ git push       → updates the GH Pages dashboard
```

Wake-from-sleep: the Mac wakes briefly at 5:55 AM (via `pmset repeat`),
runs the job by 6:00 AM, then goes back to sleep. Display stays off.

---

## One-time setup

Estimate: 10–15 minutes.

### 1. Install Python (if you don't have it already)

Open Terminal and run:
```
python3 --version
```
If that prints `Python 3.x.y` (any 3.10+), you're set. If "command not found",
install with:
```
brew install python
```
(install Homebrew first from https://brew.sh if needed).

### 2. Run the setup script

From the repo folder:
```
cd ~/Desktop/casenet-tracker
bash mac-agent/setup.sh
```

It prompts for your bar number and iMessage recipient, creates a Python venv,
installs dependencies + Playwright Chromium, and installs the LaunchAgent.

### 3. Enable wake-from-sleep at 5:55 AM (Mon–Fri)

```
sudo pmset repeat wake MTWRF 05:55:00
```
This is what makes the daily run survive your Mac being asleep. Only works
while the Mac is plugged in. Add Sat/Sun by using `MTWRFSU`.

To remove later: `sudo pmset repeat cancel`.

### 4. Test run

```
bash run.sh
```

The first run triggers two macOS permission prompts:
- **"Python wants to control Calendar"** → click OK
- **"Python wants to control Messages"** → click OK

After that the script runs quietly. Logs land in `logs/tracker.log`. If it
succeeds, you should see hearings on:
- The dashboard URL: `https://corbinhealy.github.io/casenet-tracker/`
- A new "CaseNet" calendar in Calendar.app (auto-syncs to your iPhone)
- An iMessage from yourself with a one-line summary

---

## Day-to-day use

Nothing — the LaunchAgent runs every weekday morning. You just check the
iMessage when it lands or open the dashboard.

To **change prep rules**: edit `prep_rules.yaml`, save, `bash run.sh` to test.

To **add another county**: add it to `config.yaml` under `attorney.counties`.
Codes for Clay, Platte, Cass, Johnson are already in `src/scraper.py`; for
others, look at the `<select name="courtCode">` options on the search form.

To **change the wake time** (e.g. 5:30 AM):
```
sudo pmset repeat cancel
sudo pmset repeat wake MTWRF 05:25:00
```
And in `~/Library/LaunchAgents/com.casenet.tracker.plist`, change the `Hour`
and `Minute` of `StartCalendarInterval`. Then:
```
launchctl unload ~/Library/LaunchAgents/com.casenet.tracker.plist
launchctl load   ~/Library/LaunchAgents/com.casenet.tracker.plist
```

---

## Troubleshooting

**Nothing happened at 6 AM.** Was the Mac actually awake? Check:
```
log show --predicate 'subsystem == "com.apple.xpc.launchd"' --last 2h | grep casenet
```
If the LaunchAgent didn't fire, the Mac was likely off or unplugged.

**iMessage didn't arrive but everything else worked.** Most often Full Disk
Access not granted to `osascript`. System Settings → Privacy & Security →
Full Disk Access → add `/usr/bin/osascript`.

**Calendar events aren't on my iPhone.** Calendar.app → check that the
"CaseNet" calendar is part of your iCloud account (not "On My Mac"). If it
ended up under On My Mac, drag it into iCloud in Calendar.app's sidebar.

**The dashboard URL shows a 404.** GitHub Pages takes ~1 minute after the
first push. If still 404 after 5 minutes: repo Settings → Pages → confirm
Source = "Deploy from a branch", Branch = `main`, folder = `/docs`.

**Email is missing on the dashboard or iMessage didn't fire today.** Check
`logs/tracker.log` for stack traces. The orchestrator runs each notifier in
its own try/except, so one failure doesn't kill the others.

**A hearing on my paper docket isn't showing up.** Confidential cases —
juvenile, sealed, certain DV ex-parte matters — are not visible on CaseNet's
public attorney search. This system can't see them.

---

## Files

- `src/` — Python source. Each module is single-responsibility.
- `tests/` — Parser regression tests against a labeled-block fixture.
- `state/docket.json` — Last successful pull. Auto-committed.
- `docs/` — Static dashboard published via GitHub Pages.
- `mac-agent/` — `setup.sh`, the LaunchAgent template, and helpers.
- `run.sh` — Wrapper the LaunchAgent calls each morning.
- `config.yaml` — User config (county, calendar name, iMessage recipient).
- `prep_rules.yaml` — Hearing-type → days-out flags.
- `.bar_number` — Your MOBAR number, gitignored.

---

## Legal note

CaseNet data is public record under Missouri Court Operating Rule 2.
Automated retrieval of public records is generally permissible; this tool
intentionally throttles to one query per 7-day window per day, runs from a
residential IP, and uses a normal browser user-agent. If you ever receive a
notice from OSCA asking you to stop, stop.

# CaseNet Tracker

Automated daily pull of Missouri CaseNet's "Scheduled Hearings → Search by Attorney"
form for one MOBAR number, with prep-flag rules, and four delivery channels:

- Daily email digest (Gmail SMTP)
- Google Calendar event sync (dedicated calendar)
- Static dashboard (GitHub Pages)
- iMessage summary at 6:05 AM (Mac LaunchAgent)

The job runs via GitHub Actions at 11:00 UTC daily (6:00 AM Central) and writes
state and dashboard back to the repo.

---

## One-time setup

You will do this once. Estimate: 25–40 minutes.

### 1. Create the GitHub repo

1. Create a **private** repo on GitHub (any name, e.g. `casenet-tracker`).
2. From the folder containing this README, push:
   ```bash
   git init
   git add .
   git commit -m "initial scaffold"
   git branch -M main
   git remote add origin git@github.com:YOUR_USERNAME/casenet-tracker.git
   git push -u origin main
   ```

### 2. Edit `config.yaml`

Open `config.yaml` and confirm:
- `attorney.name` matches what CaseNet shows for your bar number
- `attorney.county` is `Jackson` (or change as needed)
- `attorney.timezone` is `America/Chicago`
- `notify.email_to` is your delivery email

### 3. Tune `prep_rules.yaml`

Edit hearing-type → days-out lead times. The system uses case-insensitive
substring matching against CaseNet's hearing-type label, so `"jury trial"`
will match `"Jury Trial Setting"`, `"Jury Trial — Day 1"`, etc.

### 4. Generate a Gmail App Password

1. https://myaccount.google.com → Security → 2-Step Verification (must be ON)
2. App passwords → create one named `CaseNet Tracker`
3. Copy the 16-character password (no spaces). You'll paste it as a secret in step 6.

### 5. Generate Google Calendar OAuth credentials

This authorizes the workflow to create/update events on a dedicated CaseNet calendar.

1. Go to https://console.cloud.google.com → create project `casenet-tracker`
2. APIs & Services → Library → enable **Google Calendar API**
3. APIs & Services → OAuth consent screen → External, fill required fields, add
   your Gmail address as a test user
4. Credentials → Create Credentials → OAuth client ID → **Desktop app**
5. Download the JSON, save it locally as `client_secret.json` (do **not** commit)
6. In a terminal on your Mac:
   ```bash
   pip install -r requirements.txt
   python -m src.auth_gcal --client-secret /path/to/client_secret.json
   ```
   This opens a browser for you to authorize, then prints a refresh token.
   Copy that token — you'll paste it as `GCAL_REFRESH_TOKEN` in the next step.

7. In Google Calendar (web), create a new calendar called `CaseNet`. Open its
   settings → "Integrate calendar" → copy the **Calendar ID** (looks like
   `abc123@group.calendar.google.com`). You'll paste it as `GCAL_CALENDAR_ID`.

### 6. Add GitHub Secrets

Repo → Settings → Secrets and variables → Actions → New repository secret. Add:

| Name                      | Value                                                       |
|---------------------------|-------------------------------------------------------------|
| `MOBAR_NUMBER`            | Your Missouri bar number (digits only)                      |
| `GMAIL_USER`              | The Gmail address sending the digest (e.g. yours)           |
| `GMAIL_APP_PASSWORD`      | The 16-char app password from step 4                        |
| `GCAL_CLIENT_ID`          | From `client_secret.json` (step 5)                          |
| `GCAL_CLIENT_SECRET`      | From `client_secret.json` (step 5)                          |
| `GCAL_REFRESH_TOKEN`      | From step 5's `auth_gcal.py` output                         |
| `GCAL_CALENDAR_ID`        | The CaseNet calendar ID from step 5                         |

### 7. Enable GitHub Pages

Repo → Settings → Pages → Source: **Deploy from a branch** → Branch: `main`,
folder: `/docs`. Save. Your dashboard will be live at
`https://YOUR_USERNAME.github.io/casenet-tracker/` within a minute or two.

Bookmark this URL on your phone and laptop. There is no login — security is
through obscurity. If that's not acceptable, see "Locking down the dashboard"
below.

### 8. First run

Repo → Actions → "Daily CaseNet pull" → Run workflow. Watch the run. If green:
expect an email digest to arrive within a minute, calendar events to populate,
and the dashboard to update on next page refresh.

If red: open the failed step's log. Common first-run failures and fixes are in
the "Troubleshooting" section.

### 9. (Optional) Install the iMessage agent on your Mac

This wakes at 6:05 AM, fetches the dashboard's `digest.json`, and iMessages
yourself a one-line summary. Skip if you're fine with email push.

```bash
# From this folder, on your Mac:
cp mac-agent/com.casenet.tracker.plist ~/Library/LaunchAgents/
# Edit the plist to set DASHBOARD_URL to your GH Pages URL
launchctl load ~/Library/LaunchAgents/com.casenet.tracker.plist
# Test:
launchctl start com.casenet.tracker
```

You'll get an iMessage permission prompt the first time it runs. Approve.

---

## Day-to-day use

- The workflow runs every day at 6 AM Central. You don't need to do anything.
- To **change prep rules**: edit `prep_rules.yaml`, commit, push. Next run picks it up.
- To **manually re-run** (e.g., after CaseNet edits): repo → Actions → Run workflow.
- To **add another county** (e.g., Clay): add to `config.yaml` under
  `attorney.counties:` as a list, no other changes needed.

---

## Architecture

```
GitHub Actions (cron 11:00 UTC daily)
  ├─ src/scraper.py     → Playwright → CaseNet attorney search
  ├─ src/parser.py      → HTML rows → structured records
  ├─ src/differ.py      → compare to state/docket.json → tag new/moved/cancelled
  ├─ src/flags.py       → apply prep_rules.yaml
  └─ src/main.py        → orchestrate, then fan out:
        ├─ notify_email.py        → Gmail SMTP digest
        ├─ notify_calendar.py     → Google Calendar API event sync
        └─ render_dashboard.py    → writes docs/index.html + docs/digest.json
                                  → committed back to repo → GH Pages publishes

Your Mac (LaunchAgent at 6:05am)
  └─ send_imessage.applescript    → fetches docs/digest.json → iMessages you
```

State lives in `state/docket.json`, committed back at the end of each run. Git
history gives you a full timeline of every docket change.

---

## Troubleshooting

**Email arrives empty or with weird hearing-type labels.**
CaseNet's labels for the same hearing aren't always consistent across divisions.
Add the variant to `prep_rules.yaml`. Run with `--debug` locally to see raw
labels:
```bash
python -m src.scraper --debug --bar-number 76645 --county Jackson
```

**The Actions run says "Manual CaseNet check required."**
The scraper hit either a CAPTCHA or unrecognized markup. Logs include a
screenshot artifact. Most often this is CaseNet performing a brief outage or
markup change. Re-run manually 30 minutes later. If it persists, open the
HTML artifact in `actions-debug/` and update `src/parser.py` selectors.

**Calendar events duplicating.**
Each event has a deterministic ID (`casenet-<case>-<datetime>`). Duplicates
mean a hearing time changed without keeping the same case number — usually a
CaseNet quirk. Manually delete the orphan; the next run won't re-create it.

**iMessage agent isn't firing.**
```bash
launchctl list | grep casenet
log show --predicate 'subsystem == "com.apple.xpc.launchd"' --last 1h | grep casenet
```
Most common cause: Full Disk Access not granted to `osascript` (System
Settings → Privacy → Full Disk Access).

**You're seeing a hearing on your physical calendar that didn't make it into the digest.**
That's the confidential-case blind spot — juvenile, sealed, certain DV ex-parte
matters are not visible on CaseNet's public attorney search. This system can't
fix that. Cross-reference your office's case management system for those.

---

## Locking down the dashboard

Default setup is a public-but-unguessable GH Pages URL. If you want stronger
controls, options in increasing order of effort:

1. **Cloudflare Access in front of GH Pages** — free for personal use, adds
   email-based gate. Requires moving the dashboard to a custom domain.
2. **Move dashboard to private hosting** — e.g., Vercel with password
   protection. Requires changing where `render_dashboard.py` writes to.
3. **Encrypt the dashboard payload** — write `digest.json` AES-encrypted, have
   the dashboard ask for a passphrase to decrypt client-side. Most work, no
   external dependency.

V1 ships with option 0 (unguessable URL). Open an issue in the repo if you
want one of the above implemented.

---

## Files

- `src/` — Python source. Each module is a single responsibility.
- `tests/` — Parser regression tests against a saved CaseNet HTML fixture.
- `state/docket.json` — Last successful pull. Auto-committed by the workflow.
- `docs/` — Static dashboard published via GitHub Pages.
- `mac-agent/` — Optional iMessage delivery agent.
- `.github/workflows/daily.yml` — The cron + run.
- `config.yaml` — User config (county, calendar id, recipient).
- `prep_rules.yaml` — Hearing-type → days-out flags.

---

## Legal note

CaseNet data is public record under Missouri Court Operating Rule 2.
Automated retrieval of public-facing records is generally permissible, but
this tool intentionally throttles its requests (one query per day) and uses
a normal browser user-agent. If you ever receive a notice from OSCA asking
you to stop, stop. The tool is designed to stay well within reasonable use.

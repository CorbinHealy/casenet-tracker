#!/usr/bin/env bash
# casenet-tracker — one-shot setup for Mac-local execution.
#
# Run this once from the repo root:
#   bash mac-agent/setup.sh
#
# It will:
#   1. Create a Python venv inside .venv/
#   2. Install Python deps + Playwright Chromium
#   3. Prompt for your bar number and write it to .bar_number
#   4. Prompt for your phone number / Apple ID for iMessage
#   5. Install the LaunchAgent that runs daily at 6 AM
#   6. Print the pmset command to enable wake-from-sleep at 5:55 AM
#   7. Print next steps (calendar/messages permission grants)

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Repo: $REPO_DIR"
echo

# --- Python + venv -----------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
  cat <<EOF
Python 3 isn't installed. Install it first:
    brew install python
or download from https://www.python.org/downloads/macos/
Then re-run this script.
EOF
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "==> Creating venv at .venv/"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing Python dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

echo "==> Installing Playwright Chromium (~150 MB, one time)"
python -m playwright install chromium

# --- Bar number --------------------------------------------------------------

if [ ! -f .bar_number ]; then
  echo
  read -r -p "Enter your Missouri bar number (digits only): " BAR
  if [[ ! "$BAR" =~ ^[0-9]+$ ]]; then
    echo "Bar number must be all digits — got '$BAR'. Re-run setup.sh."
    exit 1
  fi
  echo "$BAR" > .bar_number
  chmod 600 .bar_number
  echo "==> Wrote .bar_number"
fi

# --- iMessage recipient ------------------------------------------------------

if grep -q '^  imessage_to: ""' config.yaml; then
  echo
  read -r -p "Phone number or Apple ID email for the morning iMessage (blank to skip): " IMSG
  if [ -n "$IMSG" ]; then
    # macOS sed needs an empty backup arg with -i.
    sed -i '' "s|^  imessage_to: \"\"|  imessage_to: \"$IMSG\"|" config.yaml
    echo "==> Updated config.yaml notify.imessage_to"
  fi
fi

# --- LaunchAgent install -----------------------------------------------------

PLIST_TEMPLATE="$REPO_DIR/mac-agent/com.casenet.tracker.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.casenet.tracker.plist"

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__REPO_DIR__|$REPO_DIR|g" "$PLIST_TEMPLATE" > "$PLIST_DEST"

# Reload (unload first in case it was already there).
launchctl unload "$PLIST_DEST" 2>/dev/null || true
launchctl load "$PLIST_DEST"
echo "==> LaunchAgent installed and loaded ($PLIST_DEST)"

# --- Wake schedule -----------------------------------------------------------

cat <<EOF

==> Almost done. Two things you still need to do manually:

1. WAKE-FROM-SLEEP SCHEDULE
   So the script runs even when your Mac is asleep:

       sudo pmset repeat wake MTWRF 05:55:00

   That tells macOS to wake your laptop at 5:55 AM Mon–Fri (5 minutes
   before the LaunchAgent fires at 6:00). It only works while the Mac is
   plugged in. If you want weekends too, replace MTWRF with MTWRFSU.

   (To remove later: sudo pmset repeat cancel)

2. PERMISSIONS
   The first run will trigger two permission prompts:
     - "Python wants to control Calendar"  → click OK
     - "Python wants to control Messages"  → click OK
   To pre-grant: System Settings → Privacy & Security → Automation.

==> Optional — kick off a test run right now:

       bash run.sh

   That runs the full pipeline once and writes logs to logs/tracker.log.

==> Done.
EOF

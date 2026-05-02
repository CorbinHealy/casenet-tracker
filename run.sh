#!/usr/bin/env bash
# Wrapper invoked by the LaunchAgent. Activates the venv, runs the daily
# scrape + notify pipeline, and writes its output to logs/launchd.out / .err.
#
# Edit nothing here directly — config lives in config.yaml.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

mkdir -p logs

# Activate the venv if present, else fall back to python3 on PATH.
if [ -d ".venv" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
fi

# Keep the Mac awake just enough to finish the run; -m means "don't keep
# display awake," only CPU/disk/network.
exec /usr/bin/caffeinate -m python3 -m src.main

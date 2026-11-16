#!/usr/bin/env bash
# Pull the latest OneVoice Tracker code from GitHub and restart it.
# Runs nightly on its own; run it by hand to update right away:
#   bash ~/onevoice-tracker/deploy/update.sh
set -euo pipefail
cd "$(dirname "$0")/.."
before="$(git rev-parse HEAD)"
git pull --ff-only -q
after="$(git rev-parse HEAD)"
if [ "$before" = "$after" ]; then
  [ "${1:-}" = "--quiet" ] || echo "Already up to date."
  exit 0
fi
.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart onevoice-tracker
echo "Updated to $(git log -1 --format='%h %s') and restarted."

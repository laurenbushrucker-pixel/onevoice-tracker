#!/usr/bin/env bash
# Turn on AI summaries using Claude on Google Cloud (Vertex AI), billed to this
# Google Cloud account (free-trial credit first). No Anthropic key needed.
#   bash ~/onevoice-tracker/deploy/enable-vertex.sh            (Claude Sonnet 5.5)
#   bash ~/onevoice-tracker/deploy/enable-vertex.sh claude-opus-5-5
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=/etc/onevoice-tracker.env
SERVICE=onevoice-tracker
MODEL="${1:-claude-sonnet-5-5}"
[[ "$MODEL" =~ ^claude-[a-z0-9-]+$ ]] || { echo "That doesn't look like a Claude model name: $MODEL"; exit 1; }
sudo test -f "$ENV_FILE" || { echo "Run deploy/gcp-setup.sh first."; exit 1; }
command -v gcloud >/dev/null || { echo "The Google Cloud command-line tool (gcloud) isn't on this server."; exit 1; }

PROJECT="$(curl -fs -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id || true)"
[ -n "$PROJECT" ] || PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
[ -n "$PROJECT" ] || { echo "Couldn't find this server's Google Cloud project."; exit 1; }
echo "Google Cloud project: $PROJECT"

cat <<'MSG'

Step 1 of 2: sign the tracker in to Google Cloud.
  - A long link will appear below. Copy it, open it in a new browser tab,
    and sign in with the Google account that owns this project.
  - Click Allow. Google shows a code: copy it, paste it back here, press Enter.

MSG
gcloud auth application-default login --no-launch-browser \
  --scopes=https://www.googleapis.com/auth/cloud-platform
gcloud auth application-default set-quota-project "$PROJECT" >/dev/null 2>&1 || true

echo
echo "Step 2 of 2: installing and testing (about a minute)..."
.venv/bin/pip install -q -r requirements.txt
OUT="$(BILLTRACKER_AI=vertex GOOGLE_CLOUD_PROJECT="$PROJECT" VERTEX_REGION=global BILLTRACKER_SUMMARY_MODEL="$MODEL" \
  .venv/bin/python -c "
import summarize as sm
try:
    print('OK:', sm.summarize('Test bill', 'Roads; requiring the Department of Transportation to report on bridge repairs; effective date.',
        'Introduced', 'SECTION 1. The Department of Transportation shall publish an annual report on the condition and repair schedule of state bridges. SECTION 2. This act shall become effective November 1, 2027.')[0])
except sm.SummaryUnavailable as e:
    print('FAIL:', e)
" 2>&1 | tail -3)"
echo "$OUT"
if ! grep -q '^OK:' <<<"$OUT"; then
  echo
  echo "The test summary didn't work, so nothing was changed. Most common fixes:"
  echo "  - In Google Cloud, open Vertex AI > Model Garden, find $MODEL, and finish enabling it"
  echo "    (it can take a few minutes after you submit the form)."
  echo "  - Make sure the 'Vertex AI API' is enabled: console.cloud.google.com/apis/library/aiplatform.googleapis.com"
  echo "Then run this script again."
  exit 1
fi

for kv in "BILLTRACKER_AI=vertex" "GOOGLE_CLOUD_PROJECT=$PROJECT" "VERTEX_REGION=global" "BILLTRACKER_SUMMARY_MODEL=$MODEL"; do
  key="${kv%%=*}"; val="${kv#*=}"
  if sudo grep -q "^$key=" "$ENV_FILE"; then
    sudo sed -i "s|^$key=.*|$key=\"$val\"|" "$ENV_FILE"
  else
    echo "$key=\"$val\"" | sudo tee -a "$ENV_FILE" >/dev/null
  fi
done
sudo systemctl restart $SERVICE
echo
echo "Done! AI summaries are on, using $MODEL on Google Cloud."
echo "New OneVoice bills get one right away; older ones fill in about 10 every 15 minutes."
echo "Costs show on your Google Cloud bill (free-trial credit is used first)."

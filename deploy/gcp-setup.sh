#!/usr/bin/env bash
# OneVoice Tracker: set up on a Google Cloud free-tier e2-micro server (Debian 12).
#
# Run from the cloned repository:   bash ~/onevoice-tracker/deploy/gcp-setup.sh
# Safe to run again: it keeps your settings unless you choose to change them,
# and it updates the web address if the server's IP address has changed.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=/etc/onevoice-tracker.env
SERVICE=onevoice-tracker
RUN_USER="$(id -un)"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
ask()  { local reply; read -r -p "$1${2:+ [$2]}: " reply </dev/tty; printf '%s' "${reply:-${2:-}}"; }
ask_secret() { local reply; read -r -s -p "$1: " reply </dev/tty; echo >&2; printf '%s' "$reply"; }
# Quote a value for a systemd EnvironmentFile.
q() { local v="${1//\\/\\\\}"; v="${v//\"/\\\"}"; printf '"%s"' "$v"; }

if [ "$(id -u)" = 0 ]; then
  echo "Run this as your normal user (not root); it uses sudo where needed."; exit 1
fi

# ------------------------------------------------------------------ packages
say "Installing system packages (a few minutes on a small server)"
# The free server has 1 GB of memory; a swap file keeps installs from failing.
# (swapon lives in /sbin, which isn't on a normal user's PATH, so read /proc/swaps.)
if ! grep -q '^/swapfile ' /proc/swaps; then
  if [ ! -f /swapfile ]; then
    sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile
    sudo /sbin/mkswap /swapfile >/dev/null
  fi
  sudo /sbin/swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv git curl gnupg \
  debian-keyring debian-archive-keyring apt-transport-https >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq caddy >/dev/null
fi

say "Installing the tracker's Python packages"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
mkdir -p "$APP_DIR/data"

# ------------------------------------------------------------------ address
IP="$(curl -fs -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip || true)"
[ -n "$IP" ] || IP="$(curl -fs https://api.ipify.org || true)"
if [ -z "$IP" ]; then
  echo "Couldn't find this server's public IP address. Check that it has an external IP in Google Cloud."; exit 1
fi
DEFAULT_DOMAIN="${IP//./-}.sslip.io"

# ----------------------------------------------------------------- settings
write_settings=yes
if sudo test -f "$ENV_FILE"; then
  keep="$(ask "Settings already exist. Keep them? (y/n)" y)"
  [[ "$keep" =~ ^[Yy] ]] && write_settings=no
fi

if [ "$write_settings" = yes ]; then
  say "Settings (press Enter to skip anything optional)"
  while :; do
    PW="$(ask_secret "Team password for signing in (required)")"
    [ -n "$PW" ] || { warn "A password is required."; continue; }
    PW2="$(ask_secret "Type the password again")"
    [ "$PW" = "$PW2" ] && break
    warn "The two passwords didn't match. Try again."
  done
  ALERT_TO="$(ask "Email address(es) for alerts, comma separated")"
  DOMAIN="$(ask "Web address to use (Enter for the free one)" "$DEFAULT_DOMAIN")"

  echo
  echo "Email sending (from your IT team, or a Gmail account with an app password)."
  SMTP_HOST="$(ask "Mail server, e.g. smtp.office365.com or smtp.gmail.com (Enter to set up later)")"
  SMTP_PORT=587; SMTP_SECURITY=starttls; SMTP_USER=""; SMTP_PASSWORD=""; SMTP_FROM=""
  if [ -n "$SMTP_HOST" ]; then
    SMTP_PORT="$(ask "Port" 587)"
    SMTP_SECURITY="$(ask "Security: starttls, ssl or none" starttls)"
    SMTP_USER="$(ask "Mail username")"
    SMTP_PASSWORD="$(ask_secret "Mail password or app password")"
    SMTP_FROM="$(ask "Send from" "$SMTP_USER")"
  fi
  echo
  ANTHROPIC_API_KEY="$(ask_secret "Anthropic API key for AI summaries (Enter to skip)")"

  TMP="$(mktemp)"
  {
    echo "# OneVoice Tracker settings. Edit with: sudo nano $ENV_FILE"
    echo "# then restart with: sudo systemctl restart $SERVICE"
    echo "BILLTRACKER_PASSWORD=$(q "$PW")"
    echo "BILLTRACKER_ALERT_TO=$(q "$ALERT_TO")"
    echo "BILLTRACKER_URL=$(q "https://$DOMAIN/")"
    echo "BILLTRACKER_DOMAIN=$(q "$DOMAIN")"
    echo "BILLTRACKER_SESSION=\"2700\""
    echo "SMTP_HOST=$(q "$SMTP_HOST")"
    echo "SMTP_PORT=$(q "$SMTP_PORT")"
    echo "SMTP_SECURITY=$(q "$SMTP_SECURITY")"
    echo "SMTP_USER=$(q "$SMTP_USER")"
    echo "SMTP_PASSWORD=$(q "$SMTP_PASSWORD")"
    echo "SMTP_FROM=$(q "$SMTP_FROM")"
    echo "ANTHROPIC_API_KEY=$(q "$ANTHROPIC_API_KEY")"
  } >"$TMP"
  sudo install -m 600 -o root -g root "$TMP" "$ENV_FILE"
  rm -f "$TMP"
else
  DOMAIN="$(sudo sed -n 's/^BILLTRACKER_DOMAIN="\(.*\)"$/\1/p' "$ENV_FILE")"
  # Free sslip.io addresses contain the IP; follow it if the IP changed.
  if [[ -z "$DOMAIN" || "$DOMAIN" == *.sslip.io ]]; then
    DOMAIN="$DEFAULT_DOMAIN"
    sudo sed -i "s|^BILLTRACKER_DOMAIN=.*|BILLTRACKER_DOMAIN=\"$DOMAIN\"|; s|^BILLTRACKER_URL=.*|BILLTRACKER_URL=\"https://$DOMAIN/\"|" "$ENV_FILE"
  fi
fi

# ------------------------------------------------------------------ services
say "Starting the tracker"
sudo tee /etc/systemd/system/$SERVICE.service >/dev/null <<EOF
[Unit]
Description=OneVoice Tracker
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=HOST=127.0.0.1 PORT=5000 BILLTRACKER_REQUIRE_PASSWORD=true BILLTRACKER_DB=$APP_DIR/data/tracker.db
ExecStart=$APP_DIR/.venv/bin/python app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Nightly update from GitHub (anything pushed to main goes live the next morning).
sudo tee /etc/systemd/system/$SERVICE-update.service >/dev/null <<EOF
[Unit]
Description=Update OneVoice Tracker from GitHub

[Service]
Type=oneshot
User=$RUN_USER
ExecStart=/bin/bash $APP_DIR/deploy/update.sh --quiet
EOF
sudo tee /etc/systemd/system/$SERVICE-update.timer >/dev/null <<EOF
[Unit]
Description=Nightly OneVoice Tracker update

[Timer]
OnCalendar=*-*-* 04:30:00 America/Chicago
Persistent=true

[Install]
WantedBy=timers.target
EOF
# Let the update job restart the tracker without a password prompt.
echo "$RUN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart $SERVICE" \
  | sudo tee /etc/sudoers.d/$SERVICE >/dev/null
sudo chmod 440 /etc/sudoers.d/$SERVICE

sudo tee /etc/caddy/Caddyfile >/dev/null <<EOF
$DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:5000
}
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now $SERVICE >/dev/null 2>&1
sudo systemctl restart $SERVICE
sudo systemctl enable --now $SERVICE-update.timer >/dev/null 2>&1
sudo systemctl enable caddy >/dev/null 2>&1
sudo systemctl restart caddy

say "Checking that it works"
for _ in $(seq 1 30); do
  curl -fs http://127.0.0.1:5000/healthz >/dev/null && break
  sleep 2
done
if ! curl -fs http://127.0.0.1:5000/healthz >/dev/null; then
  warn "The tracker didn't start. Recent log lines:"
  sudo journalctl -u $SERVICE -n 30 --no-pager
  exit 1
fi
echo "Tracker is running."

echo "Getting a free HTTPS certificate for $DOMAIN (up to 2 minutes)..."
ok=no
for _ in $(seq 1 24); do
  if curl -fs -o /dev/null "https://$DOMAIN/healthz"; then ok=yes; break; fi
  sleep 5
done

if [ "$ok" = yes ]; then
  say "Done! Your OneVoice Tracker is live at:"
  printf '\n    \033[1mhttps://%s/\033[0m\n\n' "$DOMAIN"
  echo "Sign in with your team password, then open Alerts & settings and click 'Send a test email'."
else
  warn "The tracker is running, but https://$DOMAIN/ isn't reachable yet."
  echo "Most often this means web traffic isn't allowed through Google's firewall:"
  echo "  Google Cloud console > Compute Engine > VM instances > click this server > Edit >"
  echo "  tick 'Allow HTTP traffic' and 'Allow HTTPS traffic' > Save. Then run this script again."
  echo "Caddy's recent log lines:"
  sudo journalctl -u caddy -n 15 --no-pager
fi

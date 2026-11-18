#!/usr/bin/env bash
# Move the tracker to your own web address, e.g. after IT adds a DNS record:
#   bash ~/onevoice-tracker/deploy/set-domain.sh onevoice.tulsachamber.com
# The free sslip.io address keeps working too, as a backup.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE=/etc/onevoice-tracker.env
SERVICE=onevoice-tracker
DOMAIN="$(echo "${1:-}" | tr 'A-Z' 'a-z' | sed 's|^https\?://||; s|/.*$||')"
if ! [[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; then
  echo "Usage: bash deploy/set-domain.sh onevoice.tulsachamber.com"; exit 1
fi
sudo test -f "$ENV_FILE" || { echo "Run deploy/gcp-setup.sh first."; exit 1; }

IP="$(curl -fs -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip || true)"
[ -n "$IP" ] || IP="$(curl -fs https://api.ipify.org || true)"
FREE="${IP//./-}.sslip.io"

echo "Checking that $DOMAIN points to this server ($IP)..."
FOUND="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
if [ -z "$FOUND" ]; then
  echo "  $DOMAIN doesn't point anywhere yet. DNS changes can take up to an hour; try again later."; exit 1
elif [ -n "$IP" ] && [ "$FOUND" != "$IP" ]; then
  echo "  $DOMAIN points to $FOUND, but this server is $IP. Ask IT to point the A record to $IP."; exit 1
fi
echo "  Yes."

# Save the address in the tracker's settings (used for links in emails and the calendar link).
for key in BILLTRACKER_DOMAIN BILLTRACKER_URL; do
  sudo grep -q "^$key=" "$ENV_FILE" || echo "$key=\"\"" | sudo tee -a "$ENV_FILE" >/dev/null
done
sudo sed -i "s|^BILLTRACKER_DOMAIN=.*|BILLTRACKER_DOMAIN=\"$DOMAIN\"|; s|^BILLTRACKER_URL=.*|BILLTRACKER_URL=\"https://$DOMAIN/\"|" "$ENV_FILE"

# Serve both addresses; Caddy gets a free HTTPS certificate for the new one.
if [ -n "$IP" ] && [ "$DOMAIN" != "$FREE" ]; then SITES="$DOMAIN, $FREE"; else SITES="$DOMAIN"; fi
sudo tee /etc/caddy/Caddyfile >/dev/null <<CADDY
$SITES {
	encode gzip
	reverse_proxy 127.0.0.1:5000
}
CADDY
sudo systemctl restart caddy
sudo systemctl restart $SERVICE

echo "Getting a free HTTPS certificate for $DOMAIN (up to 2 minutes)..."
for _ in $(seq 1 24); do
  if curl -fs -o /dev/null "https://$DOMAIN/healthz"; then
    printf '\nDone! The tracker now lives at:\n\n    \033[1mhttps://%s/\033[0m\n\n' "$DOMAIN"
    [ "$SITES" != "$DOMAIN" ] && echo "The old address (https://$FREE/) still works as a backup."
    exit 0
  fi
  sleep 5
done
echo "The certificate isn't ready yet. Wait a few minutes and open https://$DOMAIN/ in a browser."
echo "Caddy's recent log lines:"
sudo journalctl -u caddy -n 15 --no-pager

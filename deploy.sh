#!/bin/sh
# Push the source into the deals CT, refresh its env, restart poller + API.
# The CT and the addresses it needs come from the dashboard's /etc/dash/config.json (DASH_CT overrides the CT).
set -e
DIR=$(dirname "$(readlink -f "$0")")
CONF=/etc/dash/config.json
c() { jq -r "($1) // empty" "$CONF"; }
CT=${DASH_CT:-$(c .cts.deals.id)}
[ -n "$CT" ] || { echo "no cts.deals in $CONF" >&2; exit 1; }
AI=$(c .cts.ai.ip)

echo "-> syncing code (CT $CT)"
tar -C "$DIR" --exclude=__pycache__ -cf - app systemd requirements.txt | pct exec "$CT" -- tar -C /opt/deals -xf -
pct exec "$CT" -- sh -c '/opt/deals/venv/bin/pip install -q -r /opt/deals/requirements.txt'

echo "-> env"
# the API answers the dashboard CT and the host only
pct exec "$CT" -- sh -c "umask 077; f=/etc/deals/deals.env; touch \$f
  for k in DEALS_API_TOKEN DEALS_OLLAMA_URL DEALS_SEARX_URL DEALS_API_ALLOW; do sed -i \"/^\$k=/d\" \$f; done
  printf 'DEALS_API_TOKEN=%s\nDEALS_OLLAMA_URL=http://%s:11434\nDEALS_SEARX_URL=http://%s:8888\nDEALS_API_ALLOW=%s,%s,127.0.0.1\n' \
    '$(cat "$DIR/token")' '$AI' '$AI' '$(c .cts.dash.ip)' '$(c .lan.host_ip)' >> \$f"

echo "-> units"
pct exec "$CT" -- sh -c 'install -m644 /opt/deals/systemd/deals-*.service /etc/systemd/system/ \
  && install -m755 /opt/deals/systemd/deals-wrapper.sh /usr/local/bin/deals \
  && systemctl daemon-reload && systemctl enable -q deals-poller deals-api'

echo "-> restarting"
pct exec "$CT" -- systemctl restart deals-poller deals-api
sleep 2
pct exec "$CT" -- sh -c 'systemctl is-active deals-poller deals-api | tr "\n" " "; echo'

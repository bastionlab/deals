#!/bin/sh
# Push the source into CT 107, refresh secrets, restart poller + API.
set -e
CT=107
DIR=$(dirname "$(readlink -f "$0")")

echo "-> syncing code"
tar -C "$DIR" -cf - app systemd | pct exec $CT -- tar -C /opt/deals -xf -

echo "-> secrets"
pct exec $CT -- sh -c "umask 077; sed -i '/^DEALS_API_TOKEN=/d' /etc/deals/deals.env; printf 'DEALS_API_TOKEN=%s\n' '$(cat "$DIR/token")' >> /etc/deals/deals.env"

echo "-> units"
pct exec $CT -- sh -c 'install -m644 /opt/deals/systemd/deals-*.service /etc/systemd/system/ \
  && install -m755 /opt/deals/systemd/deals-wrapper.sh /usr/local/bin/deals \
  && systemctl daemon-reload && systemctl enable -q deals-poller deals-api'

echo "-> restarting"
pct exec $CT -- systemctl restart deals-poller deals-api
sleep 2
pct exec $CT -- sh -c 'systemctl is-active deals-poller deals-api | tr "\n" " "; echo'

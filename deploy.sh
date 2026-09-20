#!/bin/sh
# Push the source into CT 107 and restart the poller.
set -e
CT=107
echo "-> syncing code"
tar -C "$(dirname "$0")" -cf - app systemd | pct exec $CT -- tar -C /opt/deals -xf -
echo "-> installing units"
pct exec $CT -- sh -c 'install -m644 /opt/deals/systemd/deals-poller.service /etc/systemd/system/ && systemctl daemon-reload'
echo "-> restarting"
pct exec $CT -- systemctl restart deals-poller
sleep 2
pct exec $CT -- systemctl is-active deals-poller

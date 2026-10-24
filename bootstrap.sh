#!/bin/sh
# First-time setup of a fresh Debian CT for the deals backend (Deals, Flats, Jobs). Run once by the dashboard's
# backend-install (DASH_CT = the new CT), then deploy.sh does the rest. Safe to run again.
set -e
DIR=$(dirname "$(readlink -f "$0")")
CT=${DASH_CT:?DASH_CT not set}
[ -s "$DIR/token" ] || (umask 077; openssl rand -hex 32 > "$DIR/token")
pct exec "$CT" -- sh -c 'export DEBIAN_FRONTEND=noninteractive
  apt-get update -q && apt-get install -y -q python3 python3-venv ca-certificates >/dev/null
  mkdir -p /opt/deals /var/lib/deals /etc/deals && chmod 700 /etc/deals
  [ -x /opt/deals/venv/bin/pip ] || python3 -m venv /opt/deals/venv
  f=/etc/deals/deals.env; [ -f $f ] || printf "DEALS_DB=/var/lib/deals/deals.db\nDEALS_FETCH_INTERVAL=8\n" > $f; chmod 600 $f'
echo "deals CT $CT ready for deploy.sh"

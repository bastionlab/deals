#!/bin/sh
set -a; . /etc/deals/deals.env; set +a
cd /opt/deals
exec /opt/deals/venv/bin/python -m app.cli "$@"

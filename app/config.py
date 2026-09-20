"""Runtime configuration. Secrets come from the environment (systemd EnvironmentFile)."""
import os
from pathlib import Path

DB_PATH = Path(os.environ.get("DEALS_DB", "/var/lib/deals/deals.db"))

# Kleinanzeigen is polled from the house IP. Keep it polite: one request every
# FETCH_MIN_INTERVAL seconds globally, jittered, no matter how many searches exist.
FETCH_MIN_INTERVAL = float(os.environ.get("DEALS_FETCH_INTERVAL", "8"))
FETCH_JITTER = 0.4          # +/- 40 %
HTTP_TIMEOUT = 20.0

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Plenty of nearby sellers tick "Versand moeglich" as well. Below this distance
# you would just go and get it, so a listing only counts as shipped beyond it.
NEARBY_KM = float(os.environ.get("DEALS_NEARBY_KM", "15"))

# A fetch that returns HTTP 200 but no ads means the markup changed under us.
# After this many in a row we alert instead of failing silently.
EMPTY_PARSE_ALERT_AFTER = 3

"""Poller script: on a STATS_UPDATED or MANUAL event, fetches and prints every stats
row for the event's scenario. Diagnostic/debugging script -- it has no side effects
on the scenario itself.
"""

import datetime
import json
import logging
import os
import requests

API_BASE = "http://api.local.futurebeeing.eu"
# The poller sets FUTUREBEEING_API_KEY automatically for every run - only set it yourself if you're
# running this script outside the poller (e.g. testing locally). Never hardcode it here.
with open("fubee_key.txt", "r") as k:
    fubee_key = k.read()
API_KEY = fubee_key
HEADERS = {"Authorization": API_KEY}
OEP_BASE_URL = "https://openenergyplatform.org/api/v0"

# Import the OEP token
with open("oep_token.txt", "r") as f:
    token = f.read()
OEP_TOKEN = token

# Import necessary list and variables for import from the OEP
with open("import_variables.json", "r") as f:
     IMPORT_VARIABLES = json.load(f)

# Import the settings necessary for different legends
with open("plot_settings.json", "r") as f:
    plot_settings = json.load(f)

# Matches the backend's own log format, so a script's captured output ("Recent runs" in
# /settings/scripts) reads the same as the rest of the app. LOG_LEVEL works the same way too.
class _UtcFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

logging.addLevelName(logging.WARNING, "WARN")
_handler = logging.StreamHandler()
_handler.setFormatter(_UtcFormatter("%(asctime)s %(levelname)s - %(message)s"))
_LOG_LEVELS = {"ERROR": logging.ERROR, "WARN": logging.WARNING, "INFO": logging.INFO, "DEBUG": logging.DEBUG}
# Root stays at WARNING so third-party libraries don't spam DEBUG/INFO noise of their own.
logging.basicConfig(level=logging.WARNING, handlers=[_handler], force=True)
log = logging.getLogger(__name__)
log.setLevel(_LOG_LEVELS.get(os.environ.get("LOG_LEVEL", "INFO"), logging.INFO))

def fetch_stats(scenario_id):
    """Every stats row (including hidden stat_type: 'metadata' legend rows) for the scenario."""
    log.info(f"Fetching stats for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/stats", headers=HEADERS, params={"scenarioId": scenario_id})
    resp.raise_for_status()
    stats = resp.json()
    log.info(f"Fetched {len(stats)} stats for scenario {scenario_id}")
    return stats

if __name__ == "__main__":
    with open("event.json") as f:
        event = json.load(f)
    event_type = event["event_type"]
    payload = event["payload"]
    log.info(f"Handling {event_type} event")

    if event_type == "STATS_UPDATED":
        scenario_id = payload["scenario_id"]
    elif event_type == "MANUAL":
        scenario_id = payload["scenario_id"]
    else:
        scenario_id = None

    
    stats = fetch_stats(scenario_id)
    print(stats)

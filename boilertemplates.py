import datetime
import json
import logging
import os
import requests

API_BASE = os.environ.get("FUTUREBEEING_API_BASE", "https://<your-domain>")
# The poller sets FUTUREBEEING_API_KEY automatically for every run - only set it yourself if you're
# running this script outside the poller (e.g. testing locally). Never hardcode it here.
API_KEY = os.environ["FUTUREBEEING_API_KEY"]
HEADERS = {"Authorization": API_KEY}

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

def fetch_scenarios():
    """Every scenario this key's municipalities cover, with id/name/bbox."""
    log.info("Fetching scenarios")
    resp = requests.get(f"{API_BASE}/v1/scenarios", headers=HEADERS)
    resp.raise_for_status()
    scenarios = resp.json()
    log.info(f"Fetched {len(scenarios)} scenarios")
    return scenarios

def fetch_buildings(scenario_id):
    """Every building/area GeoJSON Feature in the scenario - raw hstore tags are in
    feature["properties"]["tags"], which can be None (not just missing) for features with no tags."""
    log.info(f"Fetching buildings for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/geojson/{scenario_id}", headers=HEADERS)
    resp.raise_for_status()
    features = resp.json()
    log.info(f"Fetched {len(features)} features for scenario {scenario_id}")
    return features

def create_buildings(scenario_id, features):
    """features: list of {"geometry": {"type": ..., "coordinates": ...}, "properties": {tag: value, ...}}.
    Returns the list of new feature ids, in the same order as features. Recorded as a changeset
    attributed to this script - visible (and revertible) from the scenario's History tab, same as
    a person's own edits."""
    log.info(f"Creating {len(features)} features for scenario {scenario_id}")
    resp = requests.post(f"{API_BASE}/v1/geojson/{scenario_id}", headers=HEADERS, json=features)
    resp.raise_for_status()
    ids = resp.json()
    log.info(f"Created {len(ids)} features for scenario {scenario_id}")
    return ids

def update_buildings(patches):
    """patches: list of {"id": feature_id, "properties": {"tags": {...}}} (or any of the other
    allowed OSM properties instead of/alongside tags). Only the fields you include are changed.
    Recorded as a changeset attributed to this script - visible (and revertible) from the
    scenario's History tab, same as a person's own edits."""
    log.info(f"Updating {len(patches)} features")
    resp = requests.patch(f"{API_BASE}/v1/geojson", headers=HEADERS, json=patches)
    resp.raise_for_status()

def delete_buildings(feature_ids):
    """Deletes the given building/area features by id. Recorded as a changeset attributed to this
    script - visible (and revertible) from the scenario's History tab, same as a person's own
    edits."""
    log.info(f"Deleting {len(feature_ids)} features")
    resp = requests.delete(f"{API_BASE}/v1/geojson", headers=HEADERS, json=feature_ids)
    resp.raise_for_status()

def fetch_pois(scenario_id):
    """Every point of interest on the scenario, each with name/description/urgency (1-4)/category/
    feature (a GeoJSON Point in EPSG:3857 - x/y in metres, not the lon/lat the geojson endpoints
    use) and its comments. feature and feature_id can be None for a POI with no geometry attached."""
    log.info(f"Fetching points of interest for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/pois/{scenario_id}", headers=HEADERS)
    resp.raise_for_status()
    pois = resp.json()
    log.info(f"Fetched {len(pois)} points of interest for scenario {scenario_id}")
    return pois

def create_pois(scenario_id, pois):
    """pois: list of {"name", "urgency" (1-4), "category", "feature": {"type": "Point",
    "coordinates": [x, y]}} plus optional "description" and "feature_id" (id of a linked
    building/area feature). category is one of "safety", "social", "pollution", "ecology",
    "transportation", "infrastructure". coordinates are EPSG:3857 metres (the frontend map's
    projection), NOT the lon/lat the geojson endpoints use. Returns the new POI ids, in the same
    order as pois."""
    log.info(f"Creating {len(pois)} points of interest for scenario {scenario_id}")
    resp = requests.post(f"{API_BASE}/v1/pois/{scenario_id}", headers=HEADERS, json=pois)
    resp.raise_for_status()
    ids = resp.json()
    log.info(f"Created {len(ids)} points of interest for scenario {scenario_id}")
    return ids

def update_pois(patches):
    """patches: list of {"id": poi_id, ...fields to change} (name/description/urgency/category/
    feature/feature_id - same shapes as "Create points of interest"). Only the fields you include
    are changed."""
    log.info(f"Updating {len(patches)} points of interest")
    resp = requests.patch(f"{API_BASE}/v1/pois", headers=HEADERS, json=patches)
    resp.raise_for_status()

def delete_pois(poi_ids):
    """Soft-deletes the given points of interest by id - they stop appearing but the rows are kept."""
    log.info(f"Deleting {len(poi_ids)} points of interest")
    resp = requests.delete(f"{API_BASE}/v1/pois", headers=HEADERS, json=poi_ids)
    resp.raise_for_status()

def fetch_stats(scenario_id):
    """Every stats row for the scenario."""
    log.info(f"Fetching stats for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/stats", headers=HEADERS, params={"scenarioId": scenario_id})
    resp.raise_for_status()
    stats = resp.json()
    log.info(f"Fetched {len(stats)} stats for scenario {scenario_id}")
    return stats

def create_stats(stats):
    """stats: list of {"name", "scenarioId", "startValue": {"quantative": ...},
    "scenarioValue": {"quantative": ...}, "statType"} plus optional "visible" (default True),
    "source", "category" ("GENERAL"/"ENERGY"/"ECOLOGY"/"SOCIALINFRA"), "subcategory", and the
    impact-summary card styling: "icon" (a Lucide/Tabler name from the app's curated set - e.g.
    "leaf", "zap", "droplets", "bus", "chart-column"), "color" and "secondaryColor" (each a
    "#rgb"/"#rrggbb" hex - primary = icon + progress bar, secondary = icon-badge background).
    Any styling field left out falls back to the stat's category. Returns the new stat ids, in
    the same order as stats."""
    log.info(f"Creating {len(stats)} stats")
    resp = requests.post(f"{API_BASE}/v1/stats", headers=HEADERS, json=stats)
    resp.raise_for_status()
    ids = resp.json()
    log.info(f"Created {len(ids)} stats")
    return ids

def declare_legend(definitions):
    """Declares (or updates) this script's own dynamic map-legend parameters - the colour buckets
    shown under the legend's Energy / Ecology / Social & Infra tabs. Safe to call every run
    (upserts by key); a parameter here always belongs to this script alone, and is global to the
    script (not per-scenario). definitions: list of
    {"key", "label", "category", "unit" (optional), "subcategory" (optional), "buckets"}
    where category is one of "ENERGY", "ECOLOGY", "SOCIALINFRA" and buckets is either
    [{"kind": "range", "min", "max", "label", "colour"}, ...]
    or [{"kind": "match", "match": [[column, value], ...], "label", "colour"}, ...].
    unit is free text shown next to the parameter header - omit/None for no unit."""
    log.info(f"Declaring {len(definitions)} legend parameter definitions")
    resp = requests.post(f"{API_BASE}/v1/legend/definitions", headers=HEADERS, json={"definitions": definitions})
    resp.raise_for_status()
    return resp.json()

def update_stats(patches):
    """patches: list of {"id": stat_id, ...fields to change} (name/statType/visible/source/
    category/subcategory/startValue/scenarioValue, plus the card styling icon/color/
    secondaryColor - see "Create stats"). Only the fields you include are changed."""
    log.info(f"Updating {len(patches)} stats")
    resp = requests.patch(f"{API_BASE}/v1/stats", headers=HEADERS, json=patches)
    resp.raise_for_status()

def delete_stats(stat_ids):
    """Deletes the given stat rows by id."""
    log.info(f"Deleting {len(stat_ids)} stats")
    resp = requests.delete(f"{API_BASE}/v1/stats", headers=HEADERS, json=stat_ids)
    resp.raise_for_status()

def declare_inputs(definitions):
    """Declares (or updates) this script's own user-adjustable scenario inputs - shown as
    sliders/toggles in the scenario editor's Inputs tab, grouped by category. Safe to call every
    run (upserts by key); a definition here always belongs to this script alone, another script's
    definitions are never affected. definitions: list of either
    {"key", "label", "category", "type": "slider", "min", "max", "default",
     "step" (optional, default 1), "unit" (optional), "subcategory" (optional)}
    or {"key", "label", "category", "type": "toggle", "default": True/False,
        "unit" (optional), "subcategory" (optional)}.
    category is one of "GENERAL", "ENERGY", "ECOLOGY", "SOCIALINFRA". unit is free text
    (e.g. "kWh", "m2", "€") shown next to the value - omit/None for no unit. subcategory groups
    inputs under their own header within a category tab (e.g. multiple inputs sharing
    subcategory="Heating" show together under a "HEATING" header) - omit/None for a standalone
    card with no header."""
    log.info(f"Declaring {len(definitions)} scenario input definitions")
    resp = requests.post(f"{API_BASE}/v1/scenario-inputs/definitions", headers=HEADERS, json={"definitions": definitions})
    resp.raise_for_status()

def fetch_inputs(scenario_id):
    """Current value of every input this script has declared, for this scenario - {key: value},
    already falling back to that input's own declared default for anything the user hasn't set."""
    log.info(f"Fetching scenario inputs for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/scenario-inputs", headers=HEADERS, params={"scenario_id": scenario_id})
    resp.raise_for_status()
    return resp.json()

if __name__ == "__main__":
    with open("event.json") as f:
        event = json.load(f)
    event_type = event["event_type"]
    payload = event["payload"]
    log.info(f"Handling {event_type} event")

    if event_type == "SCENARIO_CREATED":
        scenario_id = payload["id"]
    elif event_type == "SCENARIO_CHANGED":
        scenario_id = payload["scenario_id"]
    elif event_type == "STATS_UPDATED":
        scenario_id = payload["scenario_id"]
    elif event_type == "MANUAL":
        scenario_id = payload["scenario_id"]
    else:
        scenario_id = None

    # TODO: call your selected functions above, e.g.:
    create_pois(scenario_id, [
        {"name": "Unsafe crossing", "urgency": 3, "category": "safety",
         "description": "Pedestrians frequently cross without using the crosswalk.",
         "feature": {"type": "Point", "coordinates": [730979.44, 7023684.55]}},
    ])
    create_stats([
        {"name": "Tree canopy", "scenarioId": scenario_id,
         "startValue": {"quantative": 1200}, "scenarioValue": {"quantative": 1850},
         "statType": "m2", "category": "ECOLOGY", "subcategory": "Green",
         "icon": "trees", "color": "#2e7d32", "secondaryColor": "#e8f5e9"},
    ])
    declare_legend([
        {"key": "heat_demand", "label": "Heat demand", "category": "ENERGY", "unit": "kWh", "buckets": [
            {"kind": "range", "min": 0, "max": 2000, "label": "0-2,000", "colour": "#fee08b"},
            {"kind": "range", "min": 2000, "max": 4000, "label": "2,000-4,000", "colour": "#fdae61"},
        ]},
        {"key": "cycling_infra", "label": "Cycling infrastructure", "category": "SOCIALINFRA", "buckets": [
            {"kind": "match", "match": [["highway", "cycleway"], ["bicycle", "designated"]],
             "label": "Cycle path", "colour": "#3288bd"},
        ]},
    ])
    declare_inputs([
        {"key": "example_slider", "label": "Example slider", "category": "GENERAL", "type": "slider",
         "min": 0, "max": 100, "default": 50, "unit": "m2", "subcategory": "Example group"},
        {"key": "example_toggle", "label": "Example toggle", "category": "GENERAL", "type": "toggle", "default": False},
    ])
    inputs = fetch_inputs(scenario_id)
    example_value = inputs["example_slider"]  # matches the key declared above

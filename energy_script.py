"""Poller-run variant of the new-scenario import pipeline: on a SCENARIO_CREATED or
MANUAL event, loads OEP building data (BAG) for the scenario's bounding box,
spatially joins it to the scenario's OSM buildings, computes heating/PV energy
statistics via OEP system imports, declares the initial scenario inputs, and
publishes the building tags, legends and energy stats. Reads its API key and
OEP/legend/stats config from the poller's environment and the /v1/scripts/files
endpoint rather than local files -- see data_import.py for the local/standalone
variant.
"""

import datetime
import json
import os
import logging
import requests
import geopandas as gpd
import pandas as pd
from shapely.geometry import shape, Polygon
import numpy as np
import matplotlib as mpl
import matplotlib.colors as mcolors
import time

API_BASE = os.environ.get("API_BASE_URL", "https://<your-domain>")
# The poller sets FUTUREBEEING_API_KEY automatically for every run - only set it yourself if you're
# running this script outside the poller (e.g. testing locally). Never hardcode it here.
API_KEY = os.environ["FUTUREBEEING_API_KEY"]
HEADERS = {"Authorization": API_KEY}

OEP_BASE_URL = "https://openenergyplatform.org/api/v0"

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

resp  = requests.get(f"{API_BASE}/v1/scripts/files", headers=HEADERS)
available_files = resp.json()

# Import necessary list and variables for import from the OEP
current_file = next((file for file in available_files if file["name"] == "energy_import_variables.json"), None)
resp  = requests.get(f"{API_BASE}/v1/scripts/files/{current_file['id']}", headers=HEADERS)
IMPORT_VARIABLES = resp.json()

# Impoert the stats initialization pattern 
current_file = next((file for file in available_files if file["name"] == "energy_stats_init.json"), None)
resp  = requests.get(f"{API_BASE}/v1/scripts/files/{current_file['id']}", headers=HEADERS)
STATS_INIT = resp.json()

# Import the settings necessary for different legends
current_file = next((file for file in available_files if file["name"] == "energy_legend_settings.json"), None)
resp  = requests.get(f"{API_BASE}/v1/scripts/files/{current_file['id']}", headers=HEADERS)
legend_settings = resp.json()

# Import the initialization file for the energy inputs
current_file = next((file for file in available_files if file["name"] == "energy_input_init.json"), None)
resp  = requests.get(f"{API_BASE}/v1/scripts/files/{current_file['id']}", headers=HEADERS)
INPUT_INIT = resp.json()

# Set the header for the OEP queries
OEP_HEADERS = {
    "Content-Type": "application/json"
}

# Extract the initiation variables for the legends
REFURB_STATE_KEY = legend_settings["refurbishment_state"]["key"]
LEGEND_REFURB_STATE_SOURCE = legend_settings["refurbishment_state"]["source"]

TECH_KEY = legend_settings["tech"]["key"]   # must exactly match the tag key in build_building_tags
LEGEND_TECH_SOURCE = legend_settings["tech"]["source"]

PV_KEY = legend_settings["pv_activated"]["key"]   # must exactly match the tag key in build_building_tags
LEGEND_PV_SOURCE = legend_settings["pv_activated"]["source"]

DH_KEY = legend_settings["dh_potential"]["key"]   # must exactly match the tag key in build_building_tags
LEGEND_DH_SOURCE = legend_settings["dh_potential"]["source"]

# Building Data fields from the OEP DB that should stay invisible
INVISIBLE_FIELDS_OEP = legend_settings["invisible_fields_oep"]


#----------------------------------------------------------------------------
# API-Endpunkte: FutureBeeing Backend
#----------------------------------------------------------------------------

def parse_profile(value):
    """Normalizes a profile tag value into something np.array(..., dtype=float) accepts.

    The OEP sometimes returns array columns already parsed into a list, and sometimes as
    a single comma-separated string -- this handles both.
    """
    if isinstance(value, str):
        return value.split(",")
    return value


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

def fetch_scenarios():
    """Every scenario this key's municipalities cover, with id/name/bbox."""
    log.info("Fetching scenarios")
    resp = requests.get(f"{API_BASE}/v1/scenarios", headers=HEADERS)
    resp.raise_for_status()
    scenarios = resp.json()
    log.info(f"Fetched {len(scenarios)} scenarios")
    return scenarios

def fetch_features(scenario_id):
    """Every building/area and heat-network-line GeoJSON Feature in the scenario - raw
    hstore tags are in feature["properties"]["tags"], which can be None (not just missing)
    for features with no tags."""
    log.info(f"Fetching buildings for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/geojson/{scenario_id}", headers=HEADERS)
    resp.raise_for_status()
    features = resp.json()
    log.info(f"Fetched {len(features)} features for scenario {scenario_id}")
    line_features = []
    building_features = []

    for feature in features:
        if feature["geometry"]["type"] in ["Polygon", "MultiPolygon"] and (
            feature["properties"]["building"] in ["house", "apartments", "bungalow", "detached", "residential", "terrace", "semidetached_house", "farm", "annexe"]
            or "ref:bag" in feature["properties"]["tags"]
        ):
            building_features.append(feature)
        elif feature["geometry"]["type"] in ["LineString", "MultiLineString"] and feature["properties"]["highway"] in ["secondary", "tertiary", "residential", "unclassified", "service"]:
            line_features.append(feature)
    log.info(f"Kept {len(building_features)} features with polygons.")
    
    log.info(f"Kept {len(line_features)} features with linestrings.")
    return building_features, line_features

def fetch_tagged_buildings(scenario_id):
    """Every building/area GeoJSON Feature in the scenario - raw hstore tags are in
    feature["properties"]["tags"], which can be None (not just missing) for features with no tags."""
    log.info(f"Fetching buildings for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/geojson/{scenario_id}", headers=HEADERS)
    resp.raise_for_status()
    features = resp.json()
    log.info(f"Fetched {len(features)} features for scenario {scenario_id}")
    features = [feature for feature in features if "heat_cluster" in feature["properties"]["tags"]]
    log.info(f"Kept {len(features)} features with polygons.")
    return features

def update_buildings(patches):
    """patches: list of {"id": feature_id, "properties": {"tags": {...}}} (or any of the other
    allowed OSM properties instead of/alongside tags). Only the fields you include are changed.
    Recorded as a changeset attributed to this script - visible (and revertible) from the
    scenario's History tab, same as a person's own edits."""
    log.info(f"Updating {len(patches)} features")
    resp = requests.patch(f"{API_BASE}/v1/geojson", headers=HEADERS, json=patches)
    resp.raise_for_status()

def fetch_stats(scenario_id):
    """Every stats row for the scenario."""
    log.info(f"Fetching stats for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/stats", headers=HEADERS, params={"scenarioId": scenario_id})
    resp.raise_for_status()
    stats = resp.json()
    log.info(f"Fetched {len(stats)} stats for scenario {scenario_id}")
    return stats

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

def publish_energy_stats(patches):
    """Publishes a new stat for the energy calculations.

    Sends the stats one at a time (instead of as a batch) so that a single invalid stat
    doesn't block the whole publish -- the failing entry is logged (including the full
    response) and the exception is then re-raised.

    Args:
        patches: List of new stat dicts (name, scenarioId, startValue, scenarioValue,
            statType, visible, source) to publish via POST.
    """
    log.info(f"Publishing {len(patches)} stats.")
    if len(patches) > 0:
        for stat in patches:
            resp = requests.post(f"{API_BASE}/v1/stats", headers=HEADERS, json=[stat])
            if not resp.ok:
                log.info(f"FEHLER bei: {stat['name']}")
                log.info(resp.text)
                log.info(stat)
                log.error(f"POST /v1/stats failed with {resp.status_code}: {resp.text[:1000]}")
                resp.raise_for_status()

def update_stats(patches):
    """patches: list of {"id": stat_id, ...fields to change} (name/statType/visible/source/
    startValue/scenarioValue). Only the fields you include are changed."""
    log.info(f"Updating {len(patches)} stats.")
    if len(patches) > 0:
        resp = requests.patch(f"{API_BASE}/v1/stats", headers=HEADERS, json=patches)
        resp.raise_for_status()

def fetch_inputs(scenario_id):
    """Current value of every input this script has declared, for this scenario - {key: value},
    already falling back to that input's own declared default for anything the user hasn't set."""
    log.info(f"Fetching scenario inputs for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/scenario-inputs", headers=HEADERS, params={"scenario_id": scenario_id})
    resp.raise_for_status()
    return resp.json()

#----------------------------------------------------------------------------
# API-Endpunkte: OpenEnergyPlatform (OEP)
#----------------------------------------------------------------------------

def _post_oep_advanced_search(query, max_attempts=3):
    """POSTs an OEP /advanced/search query, retrying on failure.

    The endpoint intermittently answers a fraction of otherwise-identical, valid
    requests with 400 {"reason": "Invalid request"} -- confirmed to be unrelated to
    query content (reproduced with a trivial dummy query against multiple tables).
    Retries paper over that flakiness instead of failing the whole pipeline run.
    """
    last_response = None
    for attempt in range(1, max_attempts + 1):
        response = requests.post(f"{OEP_BASE_URL}/advanced/search", headers=OEP_HEADERS, json=query)
        if response.ok:
            return response
        last_response = response
        log.warning(
            f"OEP advanced search failed (attempt {attempt}/{max_attempts}): "
            f"{response.status_code} {response.text[:500]}"
        )
    last_response.raise_for_status()
    return last_response


def import_oep_bbox_data(bbox):
    """Loads every building from the OEP table supply.nl_mosaiq_phase_1 whose geometry
    intersects the given bounding box.

    Builds an advanced-search query against the OEP (ST_Intersects with ST_MakeEnvelope
    built from the bbox), converts the returned GeoJSON geometry column into real
    geometry objects via shapely.shape, and returns the result as a GeoDataFrame.

    Args:
        bbox: Bounding box as [min_lon, min_lat, max_lon, max_lat] (order matches how
            bbox[0..3] are used when building the envelope).

    Returns:
        GeoDataFrame (CRS EPSG:4326) with every column in COLUMNS plus "geometry".
    """
    # TODO
    # No API key required
    SCHEMA = "supply"
    TABLE = "nl_mosaiq_phase_1"   # see table_nl_phase1 in 10_oep_pipeline_phase1.ipynb

    # All columns from table_schema_phase_1 (10_oep_pipeline_phase1.ipynb), except "geometry":
    # die kommt als eigenes "geojson"-Label via ST_AsGeoJSON, weil rohe PostGIS-Geometrie
    # nicht direkt JSON-serialisierbar ist.
    COLUMNS = [
        "id", "size_class", "tabula_key", "construction_year",
        "refurbishment_state", "refurbishment_state_src", "living_area",
        "heat_demand_1", "heat_demand_2", "heat_demand_3", "heat_demand",
        "heat_cluster_1", "heat_cluster_2", "heat_cluster_3", "heat_cluster",
        "heat_technology", "elec_demand", "elec_cluster",
        "roof_type", "roof_orientation", "roof_area_south", "roof_area_eastwest",
        "roof_cluster_south", "roof_cluster_eastwest", "nearest_city"
    ]

    query = {
        "query": {
            "fields": [
                *[{"type": "column", "column": col} for col in COLUMNS],
                {"type": "label", "label": "geojson",
                "element": {"type": "function", "function": "ST_AsGeoJSON",
                            "operands": [{"type": "column", "column": "geometry"}]}}
            ],
            "from": {"type": "table", "schema": SCHEMA, "table": TABLE},
            "where": [
                {"type": "function", "function": "ST_Intersects",
                    "operands": [
                        {"type": "column", "column": "geometry"},
                        {"type": "function", "function": "ST_MakeEnvelope",
                        "operands": [{"type":"value","value":bbox[0]}, {"type":"value","value":bbox[3]},
                                    {"type":"value","value":bbox[2]}, {"type":"value","value":bbox[1]},
                                    {"type":"value","value":4326}]}
                    ]}
            ]
        }
    }

    response = _post_oep_advanced_search(query)

    result = response.json()
    # Response format per OEP docs: {"data": [[row1_col1, ...], [row2_col1, ...], ...]}
    buildings = pd.DataFrame(result["data"], columns=COLUMNS + ["geojson"])
    buildings["geometry"] = buildings["geojson"].apply(lambda g: shape(json.loads(g)))
    buildings.drop(columns="geojson", inplace=True)

    # A whole-number value (e.g. roof_cluster_south == 0 for a building with no
    # south-facing roof) comes back from JSON as an int, and if every row in one of these
    # columns happens to be whole, the pandas column stays int64. str(0) != str(0.0), and
    # create_system_id relies on str() -- so an int here silently breaks the system_id
    # match against the OEP system tables (built from floats). Force these to float64.
    FLOAT_COLUMNS = [
        "living_area", "heat_demand_1", "heat_demand_2", "heat_demand_3", "heat_demand",
        "heat_cluster_1", "heat_cluster_2", "heat_cluster_3", "heat_cluster",
        "elec_demand", "elec_cluster", "roof_area_south", "roof_area_eastwest",
        "roof_cluster_south", "roof_cluster_eastwest",
    ]
    buildings[FLOAT_COLUMNS] = buildings[FLOAT_COLUMNS].astype(float)

    buildings_data = gpd.GeoDataFrame(buildings, geometry="geometry", crs="EPSG:4326")

    return buildings_data

def fetch_multiple_system_ids_advanced(system_ids, table_name, column_names):
    """Queries the OEP advanced-search API for every row of a table whose system_id is in
    the given set (system_id = sid1 OR system_id = sid2 OR ...), restricted to the given
    columns.

    Args:
        system_ids: Iterable of system_id strings to search for.
        table_name: Name of the target table on the OEP (e.g. "hoogeveen_gas_only").
        column_names: Ordered list of column names to request (typically
            IMPORT_VARIABLES["column_names"][case_name]) -- only these columns are
            fetched, not every column of the table.

    Returns:
        Tuple (rows, column_names): rows is the list of raw data rows (result["data"])
        from the OEP response; column_names is the table's actual column names, in the
        same order as each row's values, taken from the query's own live
        content.description (a psycopg2-style cursor description) rather than any
        separately maintained config -- the query has no explicit "fields" list, so the
        row layout depends entirely on the live table schema, which drifts over time.
    """
    or_conditions = [
        {
            "type": "operator",
            "operator": "=",
            "operands": [
                {"type": "column", "column": "system_id"},
                sid,
            ],
        }
        for sid in system_ids
    ]

    query = {
        "query": {
            "fields": [{"type": "column", "column": col} for col in column_names],
            "from": {"type": "table", "table": table_name},
            "where": {
                "type": "operator",
                "operator": "OR",
                "operands": or_conditions,
            },
        }
    }

    res = _post_oep_advanced_search(query)
    result = res.json()

    column_names = [col[0] for col in result.get("content", {}).get("description", [])]

    rowcount = result.get("content", {}).get("rowcount")
    if rowcount == 0:
        return [], column_names

    if "data" not in result:
        raise RuntimeError(
            f"OEP advanced search for table '{table_name}' returned no 'data' key "
            f"(rowcount={rowcount}); full response: {result}"
        )

    return result["data"], column_names

#----------------------------------------------------------------------------
# Allgemeine Funktionen (Utilities & von beiden Pipelines genutzt)
#----------------------------------------------------------------------------

def sample_colours_from_cmap(cmap_name, n):
    """Sample n evenly spaced colours from a Matplotlib colormap.

    Args:
        cmap_name: Name of the Matplotlib colormap (e.g. "YlOrRd").
        n: Number of colours to sample.

    Returns:
        List of n hex colour strings, e.g. ["#ffffcc", "#fd8d3c", ...].
    """
    cmap = mpl.colormaps[cmap_name].resampled(n)
    return [mcolors.to_hex(cmap(i)) for i in range(n)]

def format_like_hstore(v):
    """Mirrors how Python floats get serialised when written to hstore -- integer-valued
    floats lose their ".0" suffix, real decimals are kept as-is.
    """
    if float(v).is_integer():
        return str(int(v))
    return str(v)

def build_demand_legend(values, name, key, unit="kWh/a", n_max_buckets=4, cmap_name="YlOrRd"):
    """Builds a legend definition from a set of values, either as exact-match buckets or
    as evenly sized range buckets.

    Args:
        values: Array (or array-like) of raw values to build the legend from (e.g. all
            heat_cluster or elec_cluster values of a scenario).
        name: Display name of the legend (title-cased and used as "label").
        key: Legend definition key passed through to declare_legend.
        unit: Unit shown in the bucket labels.
        n_max_buckets: Threshold at or below which exact-match buckets are built instead
            of evenly sized range buckets.
        cmap_name: Name of the Matplotlib colormap used for the bucket colours.

    Returns:
        Legend definition dict (key, label, category, unit, buckets) ready for
        declare_legend, or None if no values remain after removing NaNs.
    """
    float_values = values.astype(float)
    clean_values = float_values[~np.isnan(float_values)]
    unique_values = np.unique(clean_values)
    n_unique = len(unique_values)

    if n_unique == 0:
        log.warning(f"No values available to build '{name}' legend -- skipping")
        return None

    if n_unique < n_max_buckets:
        colours = sample_colours_from_cmap(cmap_name, n_unique)
        # Bucket boundaries meet at the midpoint between neighbouring values instead of a
        # fixed +/-1 padding, so closely spaced values (e.g. cluster centroids) never
        # produce overlapping ranges.
        edges = (
            [unique_values[0] - 1]
            + [(unique_values[i] + unique_values[i + 1]) / 2 for i in range(n_unique - 1)]
            + [unique_values[-1] + 1]
        )
        buckets = [
            {
                "kind": "range",
                "min": edges[i],
                "max": edges[i + 1],
                "label": f"{unique_values[i]:.2f} {unit}",
                "colour": colours[i],
            }
            for i in range(n_unique)
        ]
    else:
        k = n_max_buckets
        max_value = unique_values.max()
        min_value = unique_values.min()
        difference = max_value - min_value
        bucket_size = difference / 4
        
        edges = [min_value, min_value + bucket_size, min_value + 2 * bucket_size, min_value + 3 * bucket_size, max_value + 1]
        colours = sample_colours_from_cmap(cmap_name, k)
        buckets = [
            {
                "kind": "range",
                "min": edges[i],
                "max": edges[i + 1],
                "label": f"{edges[i]:.0f}\u2013{edges[i+1]:.0f} {unit}",
                "colour": colours[i],
            }
            for i in range(k)
        ]

    return {
        "key": key,
        "label": f"{name.replace('_', ' ').title()}",
        "category": "ENERGY",
        "unit": unit,
        "buckets": buckets,
    }

def build_heatline_legend(values, name, key, unit="MWh/a/m", cmap_name="coolwarm"):
    """Builds a legend definition for heat-line density values, always as the same four
    fixed range buckets (0-500, 500-1500, 1500-2500, 2500+), regardless of the scenario's
    actual data range.

    Args:
        values: Array (or array-like) of raw values to build the legend from (e.g. every
            pipe's base_heatline density in a scenario).
        name: Display name of the legend (title-cased and used as "label").
        key: Legend definition key passed through to declare_legend.
        unit: Unit shown in the bucket labels.
        cmap_name: Name of the Matplotlib colormap used for the bucket colours.

    Returns:
        Legend definition dict (key, label, category, unit, buckets) ready for
        declare_legend, or None if no values remain after removing NaNs.
    """
    float_values = values.astype(float)
    clean_values = float_values[~np.isnan(float_values)]
    unique_values = np.unique(clean_values)
    n_unique = len(unique_values)

    if n_unique == 0:
        log.warning(f"No values available to build '{name}' legend -- skipping")
        return None

    max_value = unique_values.max()

    # Fixed thresholds for the first three buckets; the top edge (2500+) is pushed past
    # whatever the scenario's actual max is, so every value has a bucket and the range
    # never goes min > max, while still always showing all four levels in the legend.
    thresholds = [0, 500, 1500, 2500]
    edges = thresholds + [max(10000, max_value + 1)]
    n_buckets = len(edges) - 1
    colours = sample_colours_from_cmap(cmap_name, n_buckets)
    buckets = [
        {
            "kind": "range",
            "min": edges[i],
            "max": edges[i + 1],
            "label": (
                f"{edges[i]:.0f}–{edges[i+1]:.0f} {unit}"
                if i < n_buckets - 1
                else f"{edges[i]:.0f}+ {unit}"
            ),
            "colour": colours[i],
        }
        for i in range(n_buckets)
    ]

    return {
        "key": key,
        "label": f"{name.replace('_', ' ').title()}",
        "category": "ENERGY",
        "unit": unit,
        "buckets": buckets,
    }

def _log_step(step_name, start_time, **extra_info):
    """Logs the duration and optional extra info (e.g. row count) for a pipeline step."""
    duration = time.perf_counter() - start_time
    extra_str = " ".join(f"{k}={v}" for k, v in extra_info.items())
    log.info(f"STEP DONE: {step_name} ({duration:.2f}s) {extra_str}".rstrip())

# Cluster/measurement static_cols whose OEP system_id segment is always a float string,
# even when the value is exactly 0. Tags read back from the backend's hstore storage lose
# the ".0" for whole numbers (see format_like_hstore), so str(feature_tags[col]) alone
# gives "0" instead of "0.0" for e.g. a building with no south-facing roof, which then
# never matches the OEP table's system_id. size_class/nearest_city/roof_type are
# categorical strings and refurbishment_state is a plain int -- left as-is.
FLOAT_SYSTEM_ID_COLS = {"elec_cluster", "heat_cluster", "roof_cluster_eastwest", "roof_cluster_south"}

# Find the and write the necessary system_ids to pull from OEP
def create_system_id(tagged_features, static_cols):
    """Builds a unique, hyphen-separated system_id for each feature from its static_cols
    values and writes it straight into the feature's tag dict.

    Args:
        tagged_features: List of GeoJSON features with populated properties.tags.
        static_cols: List of tag keys the system_id is composed from (order determines the
            order within the system_id), e.g. from IMPORT_VARIABLES["static_cols"][case_name].

    Returns:
        Set of every (unique) system_id string generated across all features.
    """
    combinations = set()
    for feature in tagged_features:
        feature_tags = feature["properties"]["tags"]
        combination = []
        for col in static_cols:
            value = feature_tags[col]
            if col in FLOAT_SYSTEM_ID_COLS:
                value = float(value)
            combination.append(str(value))
        combination = "-".join(combination)
        feature_tags["system_id"] = combination
        combinations.add(combination)
    return combinations

def patch_system_data(tagged_features, response_data, summed_values):
    """Enriches each feature with the technical system data matching its system_id, and
    accumulates the technical values into summed_values in place.

    Features whose system_id has no entry in response_data are skipped (logged to the
    console, without aborting).

    Args:
        tagged_features: List of features whose tags already contain a "system_id" (see
            create_system_id).
        response_data: Dict {system_id: {column: value, ...}}, as returned by
            convert_response_data.
        summed_values: Dict of running scenario totals (see calc_systems), mutated in
            place with every feature's contribution.

    Returns:
        The subset of tagged_features whose system_id had a matching OEP entry, with
        their tag dicts extended in place with the matching system data and the
        "visible:" flags. Features with no match are dropped, not just left unpatched --
        callers rely on every returned feature actually carrying the imported fields.
    """
    patched_features = []
    for feature in tagged_features:
        feature_tags = feature["properties"]["tags"]
        feature_system_id = feature_tags["system_id"]

        if feature_system_id not in response_data:
            log.warning(f"No OEP entry for system_id '{feature_system_id}' (feature {feature.get('id')}) -- skipping")
            continue

        tech_data = response_data[feature_system_id]
        tag_techs = {}
        for attribute, value in tech_data.items():
            if isinstance(value, list):
                if f"summed_{attribute}" in summed_values:
                    summed_values[f"summed_{attribute}"] += np.array(parse_profile(value), dtype=float)

            else:
                value_name = f"total_{attribute}"
                if value_name in summed_values:
                    # Costs should only be applied, when the heat_technology is transformed. Status quo can't cost anything.
                    # "heat_technology_data == gas" allein reicht nicht: ein unveraendertes Gas-Gebaeude hat
                    # heat_technology_data == heat_technology == "gas" auch. Nur anrechnen, wenn sich die Technik
                    # tatsaechlich unterscheidet (gas -> hp/ashp_gas/district_heat). Bei ashp_gas (Hybrid) wird
                    # auch die neue Gaskomponente mit angerechnet.
                    if attribute == "ashp_cost_invest":
                        if feature_tags["heat_technology_data"] == "gas" and feature_tags["heat_technology"] != feature_tags["heat_technology_data"]:
                            summed_values[value_name] += value
                    elif attribute == "gas_heating_cost_invest":
                        if feature_tags["heat_technology_data"] == "gas" and feature_tags["heat_technology"] != feature_tags["heat_technology_data"]:
                            summed_values[value_name] += value
                    elif attribute == "hn_cost_invest":
                        if feature_tags["heat_technology_data"] == "gas" and feature_tags["heat_technology"] != feature_tags["heat_technology_data"]:
                            summed_values[value_name] += value
                    else:
                        summed_values[value_name] += value
                
                tag_techs[attribute] = value

        feature_tags.update(tag_techs)

        # The OEP only stores photovoltaic cap_invest/cost_invest/costs_om/cost_periodical
        # split by roof orientation (south/east/west) -- add the combined total here so it's
        # available under the same key naming as the already-combined production/profits,
        # and feed that combined total into summed_values same as the other technologies.
        for pv_metric in ["cap_invest", "cost_invest", "costs_om", "cost_periodical"]:
            south_key = f"photovoltaic_south_{pv_metric}"
            if south_key in feature_tags:
                feature_tags[f"photovoltaic_{pv_metric}"] = (
                    float(feature_tags[south_key])
                    + float(feature_tags[f"photovoltaic_east_{pv_metric}"])
                    + float(feature_tags[f"photovoltaic_west_{pv_metric}"])
                )
                total_key = f"total_photovoltaic_{pv_metric}"
                if total_key in summed_values:
                    summed_values[total_key] += feature_tags[f"photovoltaic_{pv_metric}"]

        for field in INVISIBLE_FIELDS_OEP["systems"]:
            feature_tags[f"visible:{field}"] = "false"

        patched_features.append(feature)

    return patched_features

def convert_response_data(response, column_names):
    """Converts the raw, column-less OEP rows (lists of values) into a dict of named
    columns per system_id.

    Assumes each row in response carries the system_id at index 1 and the actual data
    from index 2 onward (index 0 is presumably the table's own id column).

    Args:
        response: List of raw data rows (lists), as returned by
            fetch_multiple_system_ids_advanced.
        column_names: Full column name list of the source table (including id and
            system_id at position 0/1), in the same order as each row's values -- the
            live column_names returned by fetch_multiple_system_ids_advanced, not a
            separately maintained config. Only the part from index 2 onward is used for
            the mapping.

    Returns:
        Dict {system_id: {column_name: value, ...}} for fast lookup in patch_system_data.
    """
    response_data = {}
    data_column_names = column_names[2:]
    for row in response: 
        row_system_id = row[1]
        data_columns = row[2:]
        system_data = {}
        for index, column_name in enumerate(data_column_names):
            system_data[column_name] = data_columns[index]
        response_data[row_system_id] = system_data
    return response_data


def import_systems(case_features, summed_values, case_name, country):
    """Imports the matching technical system data from the OEP for one technology/PV case
    group (e.g. "gas_only", "hp_pv") and patches it into the corresponding features.

    Builds the system_ids of the given features (from the static_cols configured for
    case_name), queries the matching OEP table and enriches the features with the
    technical data found.

    Args:
        case_features: List of features in this technology/PV case group.
        case_name: Name of the case group (e.g. "gas_only", "gas_pv", "hp_only", "hp_pv",
            "ashp_gas_only", "ashp_gas_pv") -- must be a key in IMPORT_VARIABLES.
        area: Area name the table name is built from (the exact naming scheme differs
            between scripts).

    Returns:
        The subset of case_features that had a matching OEP entry, with their tag dicts
        extended with the matching system data (see patch_system_data). summed_values is
        mutated in place with this case group's contribution -- there's nothing to return.
    """
    static_cols = IMPORT_VARIABLES["static_cols"][case_name]
    import_column_names = IMPORT_VARIABLES["column_names"][case_name]
    combinations = create_system_id(case_features, static_cols)

    table_name = country + f"_{case_name}"

    # Import and merge system data
    response, column_names = fetch_multiple_system_ids_advanced(combinations, table_name, import_column_names)
    response_data = convert_response_data(response, column_names)

    # Patch the tags with the system data
    case_features = patch_system_data(case_features, response_data, summed_values)

    return case_features


def check_patches(patches, existing_stats):
    """Splits patches into those that need to be published as new stats and those that
    update an existing one.
    """
    to_publish = []
    to_patch = []
    existing_names = {stat["name"]: stat["id"] for stat in existing_stats}

    for patch in patches:
        name = patch["name"]
        if name in existing_names:
            patch["id"] = existing_names[name]
            to_patch.append(patch)
        else:
            to_publish.append(patch)

    return to_publish, to_patch

#----------------------------------------------------------------------------
# Funktionen nur fuer die Szenario-Erstellung (process_new_scenario)
#----------------------------------------------------------------------------

def get_scenario_bbox():
    """Determines the bounding box of the most recently created scenario.

    Fetches every scenario for this API key, finds the one with the newest "created_at"
    (smallest difference from "now"), and returns its bbox.

    Returns:
        The bbox of the newest scenario, in the format supplied by the API (a
        [min_lon, min_lat, max_lon, max_lat]-style list/tuple).
    """
    scenario_list = fetch_scenarios()
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    time_list = [datetime.datetime.fromisoformat(scenario["created_at"]) for scenario in scenario_list]
    difference_list = [now - creation_date for creation_date in time_list]
    last_index =  min(range(len(difference_list)), key=difference_list.__getitem__)
    bbox = scenario_list[last_index]["bbox"]
    return bbox

def get_osm_features(scenario_id):
    """Loads a scenario's OSM buildings/areas and heat-network lines and turns them into
    GeoDataFrames.

    Args:
        scenario_id: ID of the scenario whose OSM features should be loaded.

    Returns:
        Tuple (building_gdf, line_gdf, building_features, line_features):
            building_gdf: GeoDataFrame (CRS EPSG:4326) with the OSM features as polygon
                geometries.
            line_gdf: GeoDataFrame (CRS EPSG:4326) with the OSM heat-network line
                geometries, or None if the scenario has none.
            building_features/line_features: The underlying raw GeoJSON feature lists
                (as returned by fetch_features), e.g. for later id-based patches.
    """
    building_features, line_features = fetch_features(scenario_id)
    building_gdf = gpd.GeoDataFrame.from_dict(building_features)
    building_gdf["geometry"] = building_gdf["geometry"].apply(shape)
    building_gdf.set_geometry("geometry", inplace=True)
    building_gdf.set_crs("EPSG:4326", inplace=True)

    if line_features:
        line_gdf = gpd.GeoDataFrame.from_dict(line_features)
        line_gdf["geometry"] = line_gdf["geometry"].apply(shape)
        line_gdf.set_geometry("geometry", inplace=True)
        line_gdf.set_crs("EPSG:4326", inplace=True)
    else:
        log.warning("No line features (highway in secondary/tertiary/residential/unclassified) found -- skipping heat network gdf")
        line_gdf = None

    return building_gdf, line_gdf, building_features, line_features


def calc_line_length(line_gdf, country):
    if country == "nl":
        line_gdf_meters = line_gdf.to_crs("EPSG:28992")  # Amersfoort / RD New (NL, meters)
    elif country == "de":
        line_gdf_meters = line_gdf.to_crs("EPSG:25832")  # Amersfoort / RD New (NL, meters)
    line_gdf["length"] = line_gdf_meters["geometry"].length

    return line_gdf

def build_building_tags(row):
    """Builds the tag dict for a single building.

    Args:
        row: A namedtuple row (from GeoDataFrame.itertuples()) with the OEP building
            columns, as produced by the join in create_insert_tags.

    Returns:
        Dict of building tags ready to merge into a feature's properties.tags, with the
        OEP-only fields also marked invisible via "visible:<field>": "false".
    """
    tags =  {
        "size_class": row.size_class,
        "tabula_key": row.tabula_key,
        "construction_year": row.construction_year,
        "refurbishment_state_data": row.refurbishment_state,
        "refurbishment_state": row.refurbishment_state,
        "refurbishment_state_src": row.refurbishment_state_src,
        "nearest_city": row.nearest_city,
        "living_area": row.living_area,
        "heat_demand_1": row.heat_demand_1,
        "heat_demand_2": row.heat_demand_2,
        "heat_demand_3": row.heat_demand_3,
        "heat_demand": row.heat_demand,
        "heat_cluster_1": row.heat_cluster_1,
        "heat_cluster_2": row.heat_cluster_2,
        "heat_cluster_3": row.heat_cluster_3,
        "heat_cluster": row.heat_cluster,
        "heat_technology_data": row.heat_technology,
        "heat_technology": row.heat_technology,
        "elec_demand": row.elec_demand,
        "elec_cluster": row.elec_cluster,
        "roof_type": row.roof_type,
        "roof_orientation": row.roof_orientation,
        "roof_area_south": row.roof_area_south,
        "roof_area_eastwest": row.roof_area_eastwest,
        "roof_cluster_south": row.roof_cluster_south,
        "roof_cluster_eastwest": row.roof_cluster_eastwest,
        "pv_activated": row.pv_activated,
        "pv_activated_data": False
    }

    for field in INVISIBLE_FIELDS_OEP["buildings"]:
        tags[f"visible:{field}"] = "false"

    return tags

def create_insert_tags(buildings_data, osm_gdf, feature_list):
    """Spatially joins OEP building data (BAG) to OSM features and builds the resulting
    tag patches for update_buildings.

    Performs a spatial inner join (sjoin, predicate="intersects") between the OEP BAG
    buildings and the OSM features. Where one OSM feature matches multiple BAG buildings,
    only the first match is kept (logged as a warning). For every remaining match,
    build_building_tags builds the tags and merges them with the OSM feature's existing
    tags.

    Args:
        buildings_data: GeoDataFrame of the OEP BAG buildings (from import_oep_bbox_data).
        osm_gdf: GeoDataFrame of the OSM building features (from get_osm_features).
        feature_list: The raw OSM building GeoJSON feature list (from get_osm_features),
            used as the basis for the final patches.

    Returns:
        Tuple (patches, scenario_features_data):
            patches: List of {"id": feature_id, "geometry": {...}, "properties": {"tags": {...}}}
                for update_buildings. geometry is carried along (unchanged) so downstream
                steps like calc_heatline_density can still compute building centroids.
            scenario_features_data: The (deduplicated) join result as a GeoDataFrame, used
                e.g. later in calc_systems/import_systems.
    """
    scenario_features_data = gpd.sjoin(
        buildings_data, osm_gdf, how="inner", predicate="intersects", lsuffix="bag"
    )
    # scenario_features_data.drop(columns=["index_bag"], inplace=True)
    scenario_features_data.rename(columns={"id_right": "id"}, inplace=True)

    # If an OSM feature ever matches more than once, only the first hit counts --
    # sichtbar im Log, damit es nicht still passiert.
    duplicated = scenario_features_data["id"].duplicated(keep=False)
    if duplicated.any():
        n_dupes = scenario_features_data.loc[duplicated, "id"].nunique()
        log.warning(f"{n_dupes} OSM features matched more than one BAG building -- using first match only")

    scenario_features_data = scenario_features_data.drop_duplicates(subset="id", keep="first")
    scenario_features_data["pv_activated"] = "false"

    # Create the tags for the matched buildings
    tags_by_id = {}
    for row in scenario_features_data.itertuples():
        tags_by_id[row.id] = build_building_tags(row)

    # Create the patches using the created tags
    patches = []
    for feature in feature_list:
        feature_id = feature.get("id", None)
        if feature_id is None or feature_id not in tags_by_id:
            continue
        merged_tags = {**feature["properties"].get("tags", {}), **tags_by_id[feature_id]}
        patches.append({"id": feature["id"], "geometry": feature["geometry"], "properties": {"tags": merged_tags}})

    return patches, scenario_features_data

def create_energy_patches(energy_stats, scenario_id):
    """Builds the patch list for the stats endpoint from the STATS_INIT template and the
    computed energy_stats.

    Iterates over the global STATS_INIT template (the fixed order/structure of every
    possible stat) and, for every entry whose "name" appears in energy_stats, overwrites
    startValue and scenarioValue with the computed value (as int).

    Args:
        energy_stats: Dict {stat_name: value}, as returned by calc_systems().
        scenario_id: ID of the scenario the stats belong to.

    Returns:
        List of the (mutated) stat dicts from STATS_INIT, each with scenarioId set and,
        where available, updated values.
    """
    init_json = STATS_INIT
    energy_stats_keys = energy_stats.keys()
    for stat in init_json:
        name = stat["name"]
        stat["scenarioId"] = scenario_id
        if name in energy_stats_keys:
            stat["startValue"]["quantative"] = int(energy_stats[name])
            stat["scenarioValue"]["quantative"] = int(energy_stats[name])
    return init_json 

def calc_heatline_density(line_gdf, tagged_features, case):
    """Nearest-neighbour-assigns each building's heat demand to a heat network pipe
    segment, sums it into that pipe's base_heatline density (heat demand / pipe length),
    and tags each building with whether that density exceeds the district-heating
    potential threshold.

    Args:
        line_gdf: GeoDataFrame of heat network pipe segments (needs a "length" column,
            see calc_line_length).
        tagged_features: List of building GeoJSON features with populated
            properties.tags (needs "heat_cluster").
        case: Name of the scenario case (currently unused, kept for parity with future
            case-specific heat network runs).

    Returns:
        line_gdf, with a "base_heatline" column added/updated in place.
    """
    heat_line_dict = {}
    sidx_pipes = line_gdf.sindex
    feature_pipe_idx = []

    for feature in tagged_features:
        feature_centroid = shape(feature["geometry"]).centroid
        pipe_idx = sidx_pipes.nearest(feature_centroid, return_all=False)[1][0]
        feature_pipe_idx.append(pipe_idx)
        if pipe_idx not in heat_line_dict:
            heat_line_dict[pipe_idx] = {"heat_demand_sum": float(feature["properties"]["tags"]["heat_cluster"])}
        else:
            heat_line_dict[pipe_idx]["heat_demand_sum"] += float(feature["properties"]["tags"]["heat_cluster"])

    line_gdf["base_heatline"] = 0.0

    for pipe_idx in heat_line_dict:
        heat_demand = heat_line_dict[pipe_idx]["heat_demand_sum"]
        pipe_length = line_gdf.iloc[pipe_idx]["length"]
        heat_line_density = heat_demand / pipe_length
        heat_line_dict[pipe_idx]["base_heatline"] = heat_line_density
        line_gdf.iloc[pipe_idx, line_gdf.columns.get_loc("base_heatline")] = heat_line_density

    # Tag each building with the base_heatline density of its nearest pipe
    for feature, pipe_idx in zip(tagged_features, feature_pipe_idx):
        if heat_line_dict[pipe_idx]["base_heatline"] > 1500:
            feature["properties"]["tags"]["dh_potential"] = "true"
        else:
            feature["properties"]["tags"]["dh_potential"] = "false"

    return line_gdf


def patch_line_features(line_gdf, line_features):
    """Copies each pipe's computed base_heatline density from line_gdf back into the
    matching raw GeoJSON line feature's tags, ready for update_buildings."""
    id_list = list(line_gdf["id"])
    for feature in line_features:
        feature_id = feature["id"]
        if feature_id in id_list:
            heat_line_density = line_gdf.loc[line_gdf["id"] == feature_id, "base_heatline"].iloc[0]
            feature["properties"]["tags"]["base_heatline"] = heat_line_density

    return line_features


def setup_heat_network(tagged_features, line_features, line_gdf):
    if line_gdf is not None:
        line_gdf = calc_heatline_density(line_gdf, tagged_features, "base_case")
        line_features = patch_line_features(line_gdf, line_features)
    else:
        log.warning("Skipping heat network pipe creation -- no line gdf available")

    return tagged_features, line_features


def calc_systems(tagged_features, country):
    """Computes energy statistics from tagged_features and calls import_systems for the
    required heating/PV combinations.

    Splits the features by heat_technology ("hp", "gas", "ashp_gas", "district_heat") and
    then by pv_activated into up to eight case groups, imports the matching system data
    for every non-empty group via import_systems, and sums the resulting costs, emissions,
    production values and load profiles into the final scenario statistics.

    Used both for the initial (status-quo) scenario computation and for recomputing after
    a scenario change -- invest costs only ever apply once a building's heat_technology
    has actually moved away from its status-quo gas heating (see patch_system_data's
    GATED_COST_INVEST_ATTRS), so both cases share this one function.

    Args:
        tagged_features: List of all building features of the scenario with populated
            properties.tags (heat_cluster, elec_cluster, heat_technology,
            heat_technology_data, pv_activated, roof_cluster_south/eastwest, ...).
        country: "nl" or "de" -- selects the OEP table prefix passed to import_systems.

    Returns:
        Tuple (energy_stats, tagged_features):
            energy_stats: Dict of all computed scenario metrics (total_heat_demand,
                total_electricity_demand, self_sufficiency, total_emission,
                total_costs_om, transformation_cost, ...).
            tagged_features: New, merged list of the features from the case groups that
                actually had system data imported.
    """
    energy_stats = {}

    hp_only_cases = []
    hp_pv_cases = []
    gas_only_cases = []
    gas_pv_cases = []
    ashp_gas_only_cases = []
    ashp_gas_pv_cases = []
    dh_only_cases = []
    dh_pv_cases = []

    hp_cases = []
    gas_cases = []
    ashp_gas_cases = []
    dh_cases = []

    summed_values = {
        "total_heat_cluster": 0.0, "total_elec_cluster": 0.0,
        "total_roof_cluster_south": 0.0, "total_roof_cluster_eastwest": 0.0,

        "total_building_electricity_cost": 0.0, "total_building_electricity_emissions": 0.0,

        "total_ashp_cap_invest": 0.0, "total_ashp_cost_invest": 0.0, "total_ashp_costs_om": 0.0,
        "total_ashp_energy_import": 0.0, "total_ashp_import_cost": 0.0,
        "total_ashp_production": 0.0, "total_ashp_emissions": 0.0,

        "total_gas_heating_cap_invest": 0.0, "total_gas_heating_cost_invest": 0.0, "total_gas_heating_costs_om": 0.0,
        "total_gas_heating_energy_import": 0.0, "total_gas_heating_import_cost": 0.0,
        "total_gas_heating_production": 0.0, "total_gas_heating_emissions": 0.0,

        "total_hn_cap_invest": 0.0, "total_hn_cost_invest": 0.0, "total_hn_costs_om": 0.0,
        "total_hn_energy_import": 0.0, "total_hn_import_cost": 0.0,
        "total_hn_production": 0.0, "total_hn_emissions": 0.0,

        "total_thermal_storage_cap_invest": 0.0, "total_thermal_storage_cost_invest": 0.0, "total_thermal_storage_costs_om": 0.0,

        "total_battery_cap_invest": 0.0, "total_battery_cost_invest": 0.0, "total_battery_costs_om": 0.0,

        "total_photovoltaic_cap_invest": 0.0, "total_photovoltaic_cost_invest": 0.0, "total_photovoltaic_costs_om": 0.0,
        "total_photovoltaic_production": 0.0, "total_photovoltaic_profits": 0.0,

        "summed_electricity_demand_profile": np.zeros(8760), "summed_pv_generation_profile": np.zeros(8760),
        "summed_electricity_import_profile": np.zeros(8760), "summed_electricity_export_profile": np.zeros(8760),
    }

    for feature in tagged_features:
        tags = feature["properties"]["tags"]

        summed_values["total_heat_cluster"] += float(tags["heat_cluster"])
        summed_values["total_elec_cluster"] += float(tags["elec_cluster"])
        summed_values["total_roof_cluster_south"] += float(tags["roof_cluster_south"])
        summed_values["total_roof_cluster_eastwest"] += float(tags["roof_cluster_eastwest"])

        if tags["heat_technology"] == "hp":
            hp_cases.append(feature)
        elif tags["heat_technology"] == "ashp_gas":
            ashp_gas_cases.append(feature)
        elif tags["heat_technology"] == "gas":
            gas_cases.append(feature)
        elif tags["heat_technology"] == "district_heat":
            dh_cases.append(feature)

    # Load the required combinations from the MOSAIQ DB for the HP cases
    if len(hp_cases) > 0:

        hp_only_cases = [f for f in hp_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        hp_pv_cases = [f for f in hp_cases if f["properties"]["tags"]["pv_activated"] == "true"]

        if len(hp_only_cases) > 0:
            hp_only_cases = import_systems(hp_only_cases, summed_values, "hp_only", country)
        if len(hp_pv_cases) > 0:
            hp_pv_cases = import_systems(hp_pv_cases, summed_values, "hp_pv", country)

    # Load the required combinations from the MOSAIQ DB for the gas cases
    if len(gas_cases) > 0:
        gas_only_cases = [f for f in gas_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        gas_pv_cases = [f for f in gas_cases if f["properties"]["tags"]["pv_activated"] == "true"]

        if len(gas_only_cases) > 0:
            gas_only_cases = import_systems(gas_only_cases, summed_values, "gas_only", country)
        if len(gas_pv_cases) > 0:
            gas_pv_cases = import_systems(gas_pv_cases, summed_values, "gas_pv", country)

    # Load the required combinations for ASHP+gas hybrid heat pumps.
    # Beide Komponenten sind gleichzeitig installiert, daher fliessen die Werte
    # in BEIDE bestehenden Technologie-Totals (ashp_* UND gas_*) gleichzeitig ein.
    if len(ashp_gas_cases) > 0:

        ashp_gas_only_cases = [f for f in ashp_gas_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        ashp_gas_pv_cases = [f for f in ashp_gas_cases if f["properties"]["tags"]["pv_activated"] == "true"]

        if len(ashp_gas_only_cases) > 0:
            ashp_gas_only_cases = import_systems(ashp_gas_only_cases, summed_values, "ashp_gas_only", country)
        if len(ashp_gas_pv_cases) > 0:
            ashp_gas_pv_cases = import_systems(ashp_gas_pv_cases, summed_values, "ashp_gas_pv", country)

    # Load the required combinations for district heat systems.
    if len(dh_cases) > 0:

        dh_only_cases = [f for f in dh_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        dh_pv_cases = [f for f in dh_cases if f["properties"]["tags"]["pv_activated"] == "true"]

        if len(dh_only_cases) > 0:
            dh_only_cases = import_systems(dh_only_cases, summed_values, "dh_only", country)
        if len(dh_pv_cases) > 0:
            dh_pv_cases = import_systems(dh_pv_cases, summed_values, "dh_pv", country)

    # Calculate the sums for electricity in and exports
    total_electricity_import = sum(summed_values["summed_electricity_import_profile"])
    total_electricity_export = sum(summed_values["summed_electricity_export_profile"])

    # total_pv_production kommt direkt aus dem annualen "photovoltaic_production"-Tag,
    # NOT from the profile array (the profile is kept separately for time-series purposes)
    total_emission = (
        summed_values["total_gas_heating_emissions"]
        + summed_values["total_ashp_emissions"]
        + summed_values["total_hn_emissions"]
        + summed_values["total_building_electricity_emissions"]
    )

    total_costs_om = (
        summed_values["total_gas_heating_costs_om"]
        + summed_values["total_ashp_costs_om"]
        + summed_values["total_hn_costs_om"]
        + summed_values["total_thermal_storage_costs_om"]
        + summed_values["total_photovoltaic_costs_om"]
        + summed_values["total_battery_costs_om"]
    )

    total_heat_produced = (
        summed_values["total_ashp_production"]
        + summed_values["total_gas_heating_production"]
        + summed_values["total_hn_production"]
    )

    energy_stats["total_heat_demand"] = summed_values["total_heat_cluster"]
    energy_stats["total_electricity_demand"] = summed_values["total_elec_cluster"]
    self_sufficiency = 1 - total_electricity_import / summed_values["total_elec_cluster"]

    # How much of the import could be avoided through local sharing (simultaneous export)
    # Only the minimum of import/export counts per timestep.
    sharable_per_timestep = np.minimum(summed_values["summed_electricity_import_profile"], summed_values["summed_electricity_export_profile"])
    reducible_import = sum(sharable_per_timestep)    # kWh that could be avoided through sharing
    remaining_import = total_electricity_import - reducible_import  # kWh that would still have to come from the grid
    energy_sharing_potential = reducible_import / total_electricity_import if total_electricity_import > 0 else 0.0

    # Electricity stats
    energy_stats["self_sufficiency"] = self_sufficiency * 100
    energy_stats["total_electricity_import"] = total_electricity_import
    energy_stats["total_electricity_export"] = total_electricity_export
    energy_stats["total_electricity_cost"] = summed_values["total_building_electricity_cost"]  # + summed_values["total_ashp_import_cost"]
    energy_stats["reducible_import_kwh"] = reducible_import
    energy_stats["remaining_import_kwh"] = remaining_import
    energy_stats["energy_sharing_potential"] = energy_sharing_potential * 100

    # Production & emissions (combined across all technologies)
    energy_stats["total_pv_production"] = summed_values["total_photovoltaic_production"]
    energy_stats["total_heat_production"] = total_heat_produced
    # /1e6: g -> t CO2, keeps the published stat within the API's integer range
    energy_stats["total_emission"] = total_emission / 1_000_000

    # Operation & maintenance costs, combined across all technologies
    energy_stats["total_costs_om"] = total_costs_om

    # Gas stats
    energy_stats["total_gas_import"] = summed_values["total_gas_heating_energy_import"]
    energy_stats["total_gas_cost"] = summed_values["total_gas_heating_import_cost"]

    # Capacity installed & invest costs, per technology (matches the stat names in
    # energy_stats_init.json -- "ashp_heating"/"dh_heating" there, "ashp"/"hn" internally)
    energy_stats["total_gas_heating_cap_invest"] = summed_values["total_gas_heating_cap_invest"]
    energy_stats["total_gas_heating_cost_invest"] = summed_values["total_gas_heating_cost_invest"]
    energy_stats["total_ashp_heating_cap_invest"] = summed_values["total_ashp_cap_invest"]
    energy_stats["total_ashp_heating_cost_invest"] = summed_values["total_ashp_cost_invest"]
    energy_stats["total_dh_heating_cap_invest"] = summed_values["total_hn_cap_invest"]
    energy_stats["total_dh_heating_cost_invest"] = summed_values["total_hn_cost_invest"]
    energy_stats["total_thermal_storage_cap_invest"] = summed_values["total_thermal_storage_cap_invest"]
    energy_stats["total_thermal_storage_cost_invest"] = summed_values["total_thermal_storage_cost_invest"]
    energy_stats["total_photovoltaic_cap_invest"] = summed_values["total_photovoltaic_cap_invest"]
    energy_stats["total_photovoltaic_cost_invest"] = summed_values["total_photovoltaic_cost_invest"]

    # Total costs
    total_transformation_cost = (
        summed_values["total_gas_heating_cost_invest"]
        + summed_values["total_ashp_cost_invest"]
        + summed_values["total_hn_cost_invest"]
        + summed_values["total_photovoltaic_cost_invest"]
        + summed_values["total_battery_cost_invest"]
        + summed_values["total_thermal_storage_cost_invest"]
    )
    energy_stats["transformation_cost"] = total_transformation_cost

    tagged_features = gas_only_cases + gas_pv_cases + hp_only_cases + hp_pv_cases + ashp_gas_only_cases + ashp_gas_pv_cases + dh_only_cases + dh_pv_cases

    return energy_stats, tagged_features

def process_new_scenario(scenario_id, country):
    """Runs the full import/computation/publish pipeline for a new scenario.

    Flow: determine the newest scenario's bounding box -> load OEP building data for that
    bbox -> load the scenario's OSM buildings and heat network lines -> spatially join
    buildings and build tags -> compute and import energy systems/statistics -> compute
    the heat network's line density and district-heating potential -> patch the building
    and line tags -> fetch existing stats -> publish the legends (refurbishment state,
    heating technology, heat/electricity cluster, heat line density) -> publish the energy
    statistics as new or updated stats.

    Args:
        scenario_id: ID of the scenario to process.
        country: "nl" or "de" -- selects the OEP table prefix and the metric CRS used for
            heat network pipe lengths.

    Returns:
        None. All results are persisted directly via the API endpoints (update_buildings,
        declare_legend, publish_energy_stats, update_stats); nothing is returned.
    """
    pipeline_start = time.perf_counter()
    log.info(f"=== Starting pipeline for scenario {scenario_id} ===")


    # 1. Bounding Box ermitteln
    t0 = time.perf_counter()
    bbox = get_scenario_bbox()
    _log_step("get_scenario_bbox", t0, bbox=bbox)

    # 2. Load OEP building data for the bbox
    t0 = time.perf_counter()
    buildings_data = import_oep_bbox_data(bbox)
    _log_step("import_oep_bbox_data", t0, n_buildings=len(buildings_data))

    # 3. Load the scenario's OSM buildings and heat network lines
    t0 = time.perf_counter()
    building_gdf, line_gdf, building_features, line_features = get_osm_features(scenario_id)
    if line_gdf is not None:
        line_gdf = calc_line_length(line_gdf, country)
    _log_step("get_osm_features", t0, n_osm_features=len(building_features))

    # 4. Spatial join + tag aggregation
    t0 = time.perf_counter()
    tagged_features, scenario_features_df = create_insert_tags(buildings_data, building_gdf, building_features)
    _log_step("create_insert_tags", t0, n_patches=len(tagged_features))

    # 5. This is where the technologies would need to be imported from the OEP and matched to the buildings
    energy_stats, tagged_features = calc_systems(tagged_features, country)

    # 6. Calculate the heat network's line density and each building's district-heating potential
    tagged_features, line_features = setup_heat_network(tagged_features, line_features, line_gdf)

    # Publish the initialized inputs
    declare_inputs(INPUT_INIT["inputs"])


    if not tagged_features:
        log.warning(f"No patches produced for scenario {scenario_id} -- skipping update_buildings")
    else:
        # 7. Patch building and heat network line tags
        t0 = time.perf_counter()
        features_to_patch = tagged_features + line_features
        update_buildings(features_to_patch)
        _log_step("update_buildings", t0, n_patches=len(features_to_patch))

    # 8. Bestehende Stats abrufen
    t0 = time.perf_counter()
    existing_stats = fetch_stats(scenario_id)
    _log_step("fetch_stats", t0, n_stats=len(existing_stats))

    # 9. Declare legends (upserts by key)
    t0 = time.perf_counter()
    legend_definitions = [
        {"key": REFURB_STATE_KEY, **LEGEND_REFURB_STATE_SOURCE},
        {"key": TECH_KEY, **LEGEND_TECH_SOURCE},
        {"key": PV_KEY, **LEGEND_PV_SOURCE},
        {"key": DH_KEY, **LEGEND_DH_SOURCE}
    ]

    # Heat demand legend
    heat_demand = np.array([feature["properties"]["tags"]["heat_cluster"] for feature in tagged_features])
    heat_demand_legend = build_demand_legend(heat_demand, "Heat demand", "heat_cluster")
    if heat_demand_legend is not None:
        legend_definitions.append(heat_demand_legend)

    # Electricity demand legend
    elec_demand = np.array([feature["properties"]["tags"]["elec_cluster"] for feature in tagged_features])
    elec_demand_legend = build_demand_legend(elec_demand, "Electricity demand", "elec_cluster")
    if elec_demand_legend is not None:
        legend_definitions.append(elec_demand_legend)

    # Base case heat line density legend
    base_heatline = np.array([pipe["properties"]["tags"]["base_heatline"] for pipe in line_features])
    base_heatline_legend = build_heatline_legend(base_heatline, "Heat line density base case", "base_heatline")
    if base_heatline_legend is not None:
        legend_definitions.append(base_heatline_legend)

    declare_legend(legend_definitions)
    _log_step("declare_legend", t0)

    t0 = time.perf_counter()

    patches = create_energy_patches(energy_stats, scenario_id)

    stats_to_publish, stats_to_patch = check_patches(patches, existing_stats)

    if stats_to_publish:
        publish_energy_stats(stats_to_publish)
    if stats_to_patch:
        update_stats(stats_to_patch)
    _log_step("publish_energy_stats", t0)

    
    total_duration = time.perf_counter() - pipeline_start
    log.info(f"=== Finished pipeline for scenario {scenario_id} in {total_duration:.2f}s ===")

#----------------------------------------------------------------------------
# Funktionen nur fuer Szenario-Aenderungen (process_scenario_changes)
#----------------------------------------------------------------------------

# Change the building refurbishment state and the heat_demand and heat_cluster values 
def set_refurb_state(feature_tags, min_refurb_state):
    feature_period = feature_tags["tabula_key"].split(".")[-1]
    if min_refurb_state == 2 and feature_period == "06":
        refurb_state_to_set = 3
    else:
        refurb_state_to_set = min_refurb_state
        
    feature_tags["refurbishment_state"] = refurb_state_to_set
    feature_tags["heat_demand"] = feature_tags[f"heat_demand_{refurb_state_to_set}"]
    feature_tags["heat_cluster"] = feature_tags[f"heat_cluster_{refurb_state_to_set}"]
        
# Change the building refurbishment state and the heat_demand and heat_cluster values 
def check_refurb_state(tagged_features, fetched_inputs):
    """Lowers each building's refurbishment_state (and the heat_demand/heat_cluster values
    that go with it) to the scenario's min_refurb_state input, where that is an
    improvement over the building's current state.

    New buildings (tabula_key period "06") skip refurbishment state 2 and go straight to
    3, since they don't have a second refurbishment state.

    Args:
        tagged_features: List of GeoJSON features with populated properties.tags.
        fetched_inputs: Dict of this scenario's current input values, as returned by
            fetch_inputs; must contain "min_refurb_state".

    Returns:
        The tagged_features list, whose tag dicts have been updated in place where
        applicable.
    """
    log.info(fetched_inputs) 
    min_refurb_state = fetched_inputs["min_refurb_state"]
    for feature in tagged_features:
        feature_tags = feature["properties"]["tags"]
        if "refurbishment_state" in feature_tags:

            if int(feature_tags["refurbishment_state"]) > min_refurb_state:
                if int(feature_tags["refurbishment_state_data"]) > min_refurb_state:
                    set_refurb_state(feature_tags, int(feature_tags["refurbishment_state_data"]))

                else:
                    set_refurb_state(feature_tags, min_refurb_state)

            elif int(feature_tags["refurbishment_state"]) < min_refurb_state:
                set_refurb_state(feature_tags, min_refurb_state)

    return tagged_features


def update_heat_techs(tagged_features, inputs):
    """Intended to redistribute buildings across heating technologies according to the
    ashp/gas/district-heat/pv scenario-input settings, then recompute per-technology heat
    demand and PV roof area.

    Args:
        tagged_features: List of GeoJSON features with populated properties.tags.
        inputs: Dict of this scenario's current input values, as returned by fetch_inputs.

    Returns:
        The tagged_features list, whose tag dicts have been updated in place where
        applicable (heat_technology/pv_activated redistribution).
    """

    # Find the current distributions of technologies in the scenario
    hp_candidates = {}
    dh_candidates = {}
    candidates = {}

    pv_cases = {}
    pv_cases_data_set = {}
    pv_cases_data_not_set = {}
    pv_candidates = {}
    pv_free_on = {}


    for feature in tagged_features:

        # Filter the activated pv_systems
        if feature["properties"]["tags"]["pv_activated"] == "true":
            pv_cases[feature["id"]] = float(feature["properties"]["tags"]["roof_cluster_south"]) + float(feature["properties"]["tags"]["roof_cluster_eastwest"])

        # Filter the candidates for pv systems
        if feature["properties"]["tags"]["pv_activated"] == "false" and feature["properties"]["tags"]["pv_activated_data"] == "false":
            pv_candidates[feature["id"]] = float(feature["properties"]["tags"]["roof_cluster_south"]) + float(feature["properties"]["tags"]["roof_cluster_eastwest"])

        # Currently activated but not mandated by the data -- i.e. only on because a
        # previous (higher) pv_setting turned it on. Free to be turned back off.
        if feature["properties"]["tags"]["pv_activated"] == "true" and feature["properties"]["tags"]["pv_activated_data"] == "false":
            pv_free_on[feature["id"]] = float(feature["properties"]["tags"]["roof_cluster_south"]) + float(feature["properties"]["tags"]["roof_cluster_eastwest"])

        # Filter the pv_systems that are already set due to the data or due to user input
        if feature["properties"]["tags"]["pv_activated_data"] == "true" and feature["properties"]["tags"]["pv_activated"] == "true":
            pv_cases_data_set[feature["id"]] = float(feature["properties"]["tags"]["roof_cluster_south"]) + float(feature["properties"]["tags"]["roof_cluster_eastwest"])

        # Filter the pv_systems that have to be set due to the data or due to user input
        if feature["properties"]["tags"]["pv_activated_data"] == "true" and feature["properties"]["tags"]["pv_activated"] == "false":
            pv_cases_data_not_set[feature["id"]] = float(feature["properties"]["tags"]["roof_cluster_south"]) + float(feature["properties"]["tags"]["roof_cluster_eastwest"])

        # Gather all the set heating techs in the features data
        if feature["properties"]["tags"]["heat_technology_data"] == "gas":
            hp_candidates[feature["id"]] = feature["properties"]["tags"]["heat_cluster"]
            if feature["properties"]["tags"]["dh_potential"] == "true":
                dh_candidates[feature["id"]] = feature["properties"]["tags"]["heat_cluster"]


    # Sort all data dicts from low to high demand
    candidates = dict(sorted(candidates.items(), key=lambda item: float(item[1])))
    hp_candidates = dict(sorted(hp_candidates.items(), key=lambda item: float(item[1])))
    dh_candidates = dict(sorted(dh_candidates.items(), key=lambda item: float(item[1])))

    print("hp_candidates", len(hp_candidates))

    # Get the user
    hp_setting = inputs["hp_setting"]
    print("hp_setting", hp_setting)
    dh_setting = inputs["dh_setting"]
    ashp_gas_set = inputs["set_ashp_gas"]
    pv_setting = inputs["pv_setting"]

    #  Determine the number of systems that have to be changed to hp or dh
    dh_to_set = int(round(((dh_setting / 100) * len(dh_candidates)), 0))
    hp_to_set = int(round((len(hp_candidates) - dh_to_set) * (hp_setting / 100)))
    print("hp to set", hp_to_set)

    changed_techs = {}

    # First we have to find all the features that can be redistributed
    if dh_to_set > 0:
        dh_patches = dict(list(dh_candidates.items())[-dh_to_set:])
        for feature in dh_patches:
            changed_techs[feature] = "district_heat"

    hp_candidates ={k: v for k, v in hp_candidates.items() if k not in changed_techs}

    if hp_to_set > 0:
        hp_patches = dict(list(hp_candidates.items())[:hp_to_set])
        print("patches", len(hp_patches))
        if ashp_gas_set:
            for feature_id in hp_patches:
                changed_techs[feature_id] = "ashp_gas"

        else:
            for feature_id in hp_patches:
                changed_techs[feature_id] = "hp"

    remaining_candidates = {k: v for k, v in hp_candidates.items() if k not in changed_techs}


    if len(changed_techs) > 0:
        # Patch the pv systems
        for feature in tagged_features:
            # Patch the pv_activated boolen
            if feature["id"] in changed_techs:

                tags = feature["properties"]["tags"]
                tags["heat_technology"] = changed_techs[feature["id"]]

    if len(remaining_candidates) > 0:
        # Patch the pv systems
        for feature in tagged_features:
            # Patch the pv_activated boolen
            if feature["id"] in remaining_candidates:

                tags = feature["properties"]["tags"]
                tags["heat_technology"] = tags["heat_technology_data"]

    # Match the PV to the buildings prioritizing heat pumps
    num_pv_data_not_set = len(pv_cases_data_not_set)

    pvs_to_patch = []
    # First set the pv systems that have to be set
    if num_pv_data_not_set > 0:
        for system, area in pv_cases_data_not_set.items():
            pvs_to_patch.append(system)
            pv_cases_data_set[system] = area
            pv_cases[system] = area

    # Full pool of buildings pv_setting is allowed to toggle either way -- currently off
    # candidates plus currently on ones that aren't mandated by the data. Buildings whose
    # pv_activated_data is "true" are excluded here; they're patched on unconditionally
    # above and must never be turned back off regardless of pv_setting.
    pv_eligible = {**pv_candidates, **pv_free_on}

    pv_to_set = int(round((pv_setting / 100) * len(pv_eligible), 0))

    if pv_to_set > 0:
        # Sort the candidates correctly, prioritize Buildings that are designated as hp and from big to small
        tags_by_id = {feature["id"]: feature["properties"]["tags"] for feature in tagged_features}
        hp_pv_candidates = {fid: area for fid, area in pv_eligible.items() if tags_by_id[fid]["heat_technology"] == "hp"}
        hp_pv_candidates = dict(sorted(hp_pv_candidates.items(), key=lambda item: float(item[1]), reverse=True))

        # No hp buildings are sorted from big to small roof areas
        no_hp_pv_candidates = {k: v for k, v in pv_eligible.items() if k not in hp_pv_candidates}
        no_hp_pv_candidates = dict(sorted(no_hp_pv_candidates.items(), key=lambda item: float(item[1]), reverse=True))

        # Merge both dicts to a coherent candidates dict
        pv_eligible_sorted = hp_pv_candidates | no_hp_pv_candidates

        # Add the patches to the list
        chosen_candidates = dict(list(pv_eligible_sorted.items())[:pv_to_set])
        patched_candidates = chosen_candidates.keys()
        for building in patched_candidates:
            pvs_to_patch.append(building)


    # Patch the pv systems: turn on everything chosen (mandated + newly/still selected),
    # and turn back off any eligible building that lost its spot when pv_setting was
    # lowered. Buildings outside pv_eligible (data-mandated, already on) are left as-is.
    for feature in tagged_features:
        feature_id = feature["id"]
        if feature_id in pvs_to_patch:
            feature["properties"]["tags"]["pv_activated"] = "true"
        elif feature_id in pv_eligible:
            feature["properties"]["tags"]["pv_activated"] = "false"

    return tagged_features


def update_energy_patches(energy_stats, existing_stats):
    """Writes the computed energy_stats values into the matching existing stat rows.

    For every existing stat whose name is also a key in energy_stats, overwrites its
    scenarioValue with the (int-cast) computed value.

    Args:
        energy_stats: Dict {stat_name: value}, as returned by calc_systems.
        existing_stats: List of existing stat dicts for the scenario, as returned by
            fetch_stats.

    Returns:
        The existing_stats list, mutated in place.
    """
    for energy_stat in existing_stats:
        name = energy_stat["name"]
        if name in energy_stats:
            energy_stat["scenarioValue"]["quantative"] = int(energy_stats[name])

    return existing_stats

def process_scenario_changes(scenario_id, country):
    """Runs the full refurbishment-state pipeline for one scenario, triggered by a
    SCENARIO_CHANGED or MANUAL poller event: fetch tagged buildings, stats and inputs ->
    lower refurbishment states per min_refurb_state -> recompute the heat network's line
    density and each building's district-heating potential from the updated heat demand ->
    recompute energy statistics and import the matching heating/PV system data -> patch
    the updated building and line tags -> publish the heat-demand and heat-line-density
    legends -> publish the new/updated energy stats.

    Args:
        scenario_id: ID of the scenario to process (from the triggering event's payload).
        country: "nl" or "de" -- selects the OEP table prefix and the metric CRS used for
            heat network pipe lengths.

    Returns:
        None. All results are persisted directly via the API endpoints (update_buildings,
        declare_legend, publish_energy_stats, update_stats).
    """
    pipeline_start = time.perf_counter()
    log.info(f"=== Starting pipeline for scenario {scenario_id} ===")

    tagged_features = fetch_tagged_buildings(scenario_id)

    existing_stats = fetch_stats(scenario_id)

    fetched_inputs = fetch_inputs(scenario_id)

    tagged_features = check_refurb_state(tagged_features, fetched_inputs)

    # Recompute the heat network's line density and each building's dh_potential from the
    # refurbishment-adjusted heat_cluster values, before update_heat_techs redistributes
    # buildings onto district heat using dh_potential.
    t0 = time.perf_counter()
    _, line_gdf, _, line_features = get_osm_features(scenario_id)
    if line_gdf is not None:
        line_gdf = calc_line_length(line_gdf, country)
    tagged_features, line_features = setup_heat_network(tagged_features, line_features, line_gdf)
    _log_step("setup_heat_network", t0, n_lines=len(line_features))

    tagged_features = update_heat_techs(tagged_features, fetched_inputs)

    # 5. This is where the technologies would need to be imported from the OEP and matched to the buildings
    energy_stats, tagged_features = calc_systems(tagged_features, country)

    # calc_systems never touches this key itself; without it the
    # "min_refurbishment_state" stat stays frozen at its initial value and never
    # reflects the min_refurb_state slider (see update_energy_patches below).
    energy_stats["min_refurbishment_state"] = fetched_inputs["min_refurb_state"]

    if not tagged_features:
        log.warning(f"No patches produced for scenario {scenario_id} -- skipping update_buildings")
    else:
        # 5. Patch building and heat network line tags
        t0 = time.perf_counter()
        features_to_patch = tagged_features + line_features
        update_buildings(features_to_patch)
        _log_step("update_buildings", t0, n_patches=len(features_to_patch))


    # 7. Declare legend (upserts by key)
    t0 = time.perf_counter()

    legend_definitions = []

    # Heat demand legend
    heat_demand = np.array([feature["properties"]["tags"]["heat_cluster"] for feature in tagged_features])
    heat_demand_legend = build_demand_legend(heat_demand, "Heat demand", "heat_cluster")
    if heat_demand_legend is not None:
        legend_definitions.append(heat_demand_legend)

    # Base case heat line density legend
    base_heatline = np.array([pipe["properties"]["tags"]["base_heatline"] for pipe in line_features])
    base_heatline_legend = build_heatline_legend(base_heatline, "Heat line density base case", "base_heatline")
    if base_heatline_legend is not None:
        legend_definitions.append(base_heatline_legend)

    if legend_definitions:
        declare_legend(legend_definitions)

    _log_step("declare_legend", t0)

    t0 = time.perf_counter()

    patches = update_energy_patches(energy_stats, existing_stats)

    stats_to_publish, stats_to_patch = check_patches(patches, existing_stats)

    if stats_to_publish:
        publish_energy_stats(stats_to_publish)
    if stats_to_patch:
        update_stats(stats_to_patch)
    _log_step("publish_energy_stats", t0)

    total_duration = time.perf_counter() - pipeline_start
    log.info(f"=== Finished pipeline for scenario {scenario_id} in {total_duration:.2f}s ===")


def get_country(scenario_id, scenarios):
    country_str = "nl"
    for scenario in scenarios: 
        if scenario["id"] == scenario_id and "country" in scenario:
            country_str = scenario["country"]

    # As long as there is no defined countries in the backend, we resort to hardcoded checking, 
    # defaulting to nl for the first pilots
    if country_str in ["Germany", "Deutschland", "DE", "GER"] :
        country = "de"
    elif country_str in ["Netherlands", "Nederlands", "Nl", "nl"]:
        country = "nl"
    else:
        country = "nl"

    return country


if __name__ == "__main__":
    with open("event.json") as f:
        event = json.load(f)
    event_type = event["event_type"]
    payload = event["payload"]
    log.info(f"Handling {event_type} event")

    scenarios = fetch_scenarios()

    if event_type == "SCENARIO_CREATED":
        scenario_id = payload["id"]
        country = get_country(scenario_id, scenarios)
        process_new_scenario(scenario_id, country)
    elif event_type == "SCENARIO_CHANGED":
        scenario_id = payload["scenario_id"]
        country = get_country(scenario_id, scenarios)
        process_scenario_changes(scenario_id, country)
    else:
        scenario_id = None

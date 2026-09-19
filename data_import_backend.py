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

API_BASE = os.environ.get("FUTUREBEEING_API_BASE", "https://<your-domain>")
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

# Building Data fields from the OEP DB that should stay invisible
INVISIBLE_FIELDS_OEP = legend_settings["invisible_fields_oep"]


def parse_profile(value):
    """Normalizes a profile tag value into something np.array(..., dtype=float) accepts.

    The OEP sometimes returns array columns already parsed into a list, and sometimes as
    a single comma-separated string -- this handles both.
    """
    if isinstance(value, str):
        return value.split(",")
    return value


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

    if n_unique <= n_max_buckets:
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
    features = [feature for feature in features if feature["geometry"]["type"] in  ["Polygon", "MultiPolygon"] and (feature["properties"]["building"] in ["house", "apartments", "bungalow", "detached", "residential", "terrace", "semidetached_house", "farm", "annexe"] or "ref:bag" in feature["properties"]["tags"])]
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
                print(f"FEHLER bei: {stat['name']}")
                print(resp.text)
                print(stat)
                log.error(f"POST /v1/stats failed with {resp.status_code}: {resp.text[:1000]}")
                resp.raise_for_status()


def update_stats(patches):
    """patches: list of {"id": stat_id, ...fields to change} (name/statType/visible/source/
    startValue/scenarioValue). Only the fields you include are changed."""
    log.info(f"Updating {len(patches)} stats.")
    if len(patches) > 0:
        resp = requests.patch(f"{API_BASE}/v1/stats", headers=HEADERS, json=patches)
        resp.raise_for_status()

    
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

    response = requests.post(
        "https://openenergyplatform.org/api/v0/advanced/search",
        headers=OEP_HEADERS,
        json=query,
    )
    response.raise_for_status()
    
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


def fetch_multiple_system_ids_advanced(system_ids, url, table_name, column_names):
    """Queries the OEP advanced-search API for every row of a table whose system_id is in
    the given set (system_id = sid1 OR system_id = sid2 OR ...), restricted to the given
    columns.

    Args:
        system_ids: Iterable of system_id strings to search for.
        url: Base URL of the OEP API (e.g. OEP_BASE_URL); "/advanced/search" is appended.
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

    res = requests.post(
        f"{url}/advanced/search",
        json=query,
        headers=OEP_HEADERS,
    )
    res.raise_for_status()
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


def get_osm_buildings(scenario_id):
    """Loads a scenario's OSM buildings/areas and turns them into a GeoDataFrame.

    Args:
        scenario_id: ID of the scenario whose OSM features should be loaded.

    Returns:
        Tuple (osm_gdf, feature_list):
            osm_gdf: GeoDataFrame (CRS EPSG:4326) with the OSM features as polygon
                geometries.
            feature_list: The underlying raw GeoJSON feature list (as returned by
                fetch_buildings), e.g. for later id-based patches.
    """
    feature_list = fetch_buildings(scenario_id)
    osm_gdf = gpd.GeoDataFrame.from_dict(feature_list)
    osm_gdf["geometry"] = osm_gdf["geometry"].apply(shape)
    osm_gdf.set_geometry("geometry", inplace=True)
    osm_gdf.set_crs("EPSG:4326", inplace=True)
    
    return osm_gdf, feature_list


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
        osm_gdf: GeoDataFrame of the OSM features (from get_osm_buildings).
        feature_list: The raw OSM GeoJSON feature list (from get_osm_buildings), used as
            the basis for the final patches.

    Returns:
        Tuple (patches, scenario_features_data):
            patches: List of {"id": feature_id, "properties": {"tags": {...}}} for
                update_buildings.
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
        patches.append({"id": feature["id"], "properties": {"tags": merged_tags}})

    return patches, scenario_features_data
     

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


def patch_system_data(tagged_features, response_data):
    """Enriches each feature with the technical system data matching its system_id.

    Features whose system_id has no entry in response_data are skipped (logged to the
    console, without aborting).

    Args:
        tagged_features: List of features whose tags already contain a "system_id" (see
            create_system_id).
        response_data: Dict {system_id: {column: value, ...}}, as returned by
            convert_response_data.

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
        feature_tags.update(tech_data)

        # The OEP only stores photovoltaic cap_invest/cost_invest/costs_om/cost_periodical
        # split by roof orientation (south/east/west) -- add the combined total here so it's
        # available under the same key naming as the already-combined production/profits.
        for pv_metric in ["cap_invest", "cost_invest", "costs_om", "cost_periodical"]:
            south_key = f"photovoltaic_south_{pv_metric}"
            if south_key in feature_tags:
                feature_tags[f"photovoltaic_{pv_metric}"] = (
                    float(feature_tags[south_key])
                    + float(feature_tags[f"photovoltaic_east_{pv_metric}"])
                    + float(feature_tags[f"photovoltaic_west_{pv_metric}"])
                )

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


def import_systems(case_features, case_name, area):
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
        The case_features list, whose tag dicts have been extended with the matching
        system data.
    """
    static_cols = IMPORT_VARIABLES["static_cols"][case_name]
    import_column_names = IMPORT_VARIABLES["column_names"][case_name]
    combinations = create_system_id(case_features, static_cols)

    table_name = area + f"_{case_name}"

    # Import and merge system data
    response, column_names = fetch_multiple_system_ids_advanced(combinations, OEP_BASE_URL, table_name, import_column_names)
    response_data = convert_response_data(response, column_names)

    # Patch the tags with the system data
    case_features = patch_system_data(case_features, response_data)
    
    return case_features


def calc_systems(tagged_features, area):
    """Computes energy statistics from tagged_features and calls import_systems for the
    required heating/PV combinations.

    Splits the features by heat_technology ("hp", "gas"/"district_heat", "ashp_gas") and
    then by pv_activated into up to six case groups, imports the matching system data for
    every non-empty group via import_systems, and sums the resulting costs, emissions,
    production values and load profiles into the final scenario statistics.

    Args:
        tagged_features: List of all building features of the scenario with populated
            properties.tags (heat_demand/heat_cluster, elec_demand/elec_cluster,
            heat_technology, pv_activated, roof_cluster_south/eastwest, ...).
        area: Area name passed through to import_systems (table-name prefix).

    Returns:
        Tuple (energy_stats, tagged_features):
            energy_stats: Dict of all computed scenario metrics (total_heat_demand,
                total_electricity_demand, pv_setting, self_sufficiency, total_emission,
                total_costs_om, ...).
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
 
    # Statistics accumulated from the features
    total_heat_demand = 0.0
    total_heat_produced = 0
    total_electricity_demand = 0.0
    pv_potential_total = 0.0
    pv_installed_total = 0.0
    total_gas_import = 0.0
    total_electricity_export = 0.0
    total_gas_cost = 0.0
    total_electricity_cost = 0.0
    total_gas_emissions = 0.0
 
    # Analogous totals for ASHP (field names confirmed correct)
    total_ashp_emissions = 0.0
 
    # Building emissions and PV production (previously missing entirely)
    total_building_electricity_emissions = 0.0
    total_photovoltaic_production = 0.0
 
    # NEU: Operation & Maintenance Kosten, pro Technologie getrennt
    total_gas_heating_costs_om = 0.0
    total_ashp_heating_costs_om = 0.0
    total_thermal_storage_costs_om = 0.0
    total_photovoltaic_costs_om = 0.0
 
    # Stays zero at scenario creation, since this is just the status quo and no costs have been incurred yet
    total_transfomation_cost = 0.0
 
    hp_cases = []
    gas_cases = []
    ashp_gas_cases = []
 
    # Still need to be scaled up to 8760 (hourly) values
    summed_elec_demand_profiles = np.zeros(10)
    summed_pv_production_profiles = np.zeros(10)
    summed_elec_import_profiles = np.zeros(10)
    summed_elec_export_profiles = np.zeros(10)
 
    for feature in tagged_features:
        tags = feature["properties"]["tags"]
 
        total_heat_demand += float(tags["heat_demand"])
        total_electricity_demand += float(tags["elec_demand"])
 
        roof_total = tags["roof_cluster_eastwest"] + tags["roof_cluster_south"]
        pv_potential_total += roof_total
        if tags["pv_activated"] == "true":
            pv_installed_total += roof_total
 
        if tags["heat_technology"] == "hp":
            hp_cases.append(feature)
        elif tags["heat_technology"] == "ashp_gas":
            ashp_gas_cases.append(feature)
        elif tags["heat_technology"] in ("gas", "district_heat"):
            gas_cases.append(feature)
 
    energy_stats["total_heat_demand"] = total_heat_demand
    energy_stats["total_electricity_demand"] = total_electricity_demand
    energy_stats["pv_setting"] = (pv_installed_total / pv_potential_total * 100) if pv_potential_total > 0 else 0.0
 
    # Load the required combinations from the MOSAIQ DB for the HP cases
    if len(hp_cases) > 0:
        hp_heat_demand = sum(float(f["properties"]["tags"]["heat_demand"]) for f in hp_cases)
        hp_share = hp_heat_demand / total_heat_demand
        energy_stats["ashp_setting"] = hp_share * 100
 
        hp_only_cases = [f for f in hp_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        hp_pv_cases = [f for f in hp_cases if f["properties"]["tags"]["pv_activated"] == "true"]
 
        if len(hp_only_cases) > 0:
            hp_only_cases = import_systems(hp_only_cases, "hp_only", area)
            for feature in hp_only_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(parse_profile(tags["electricity_import_profile"]), dtype=float)
                summed_elec_demand_profiles += np.array(parse_profile(tags["electricity_demand_profile"]), dtype=float)
 
                # Sums
                total_heat_produced += float(tags["ashp_production"])
                total_ashp_emissions += float(tags["ashp_emissions"])
                total_electricity_cost += float(tags["building_electricity_cost"])
                total_building_electricity_emissions += float(tags["building_electricity_emissions"])
                total_ashp_heating_costs_om += float(tags["ashp_costs_om"])
                total_thermal_storage_costs_om += float(tags["thermal_storage_costs_om"])
 
        if len(hp_pv_cases) > 0:
            hp_pv_cases = import_systems(hp_pv_cases, "hp_pv", area)
            for feature in hp_pv_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(parse_profile(tags["electricity_import_profile"]), dtype=float)
                summed_elec_demand_profiles += np.array(parse_profile(tags["electricity_demand_profile"]), dtype=float)
                summed_pv_production_profiles += np.array(parse_profile(tags["pv_generation_profiles"]), dtype=float)
                summed_elec_export_profiles += np.array(parse_profile(tags["electricity_export_profile"]), dtype=float)
 
                # Sums
                total_heat_produced += float(tags["ashp_production"])
                total_ashp_emissions += float(tags["ashp_emissions"])
                total_electricity_cost += float(tags["building_electricity_cost"])
                total_building_electricity_emissions += float(tags["building_electricity_emissions"])
                total_photovoltaic_production += float(tags["photovoltaic_production"])
                total_ashp_heating_costs_om += float(tags["ashp_costs_om"])
                total_thermal_storage_costs_om += float(tags["thermal_storage_costs_om"])
                total_photovoltaic_costs_om += float(tags["photovoltaic_costs_om"])
 
    # Load the required combinations from the MOSAIQ DB for the gas cases
    if len(gas_cases) > 0:
        gas_heat_demand = sum(float(f["properties"]["tags"]["heat_demand"]) for f in gas_cases)
        gas_share = gas_heat_demand / total_heat_demand
        energy_stats["gas_setting"] = gas_share * 100
 
        gas_only_cases = [f for f in gas_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        gas_pv_cases = [f for f in gas_cases if f["properties"]["tags"]["pv_activated"] == "true"]
 
        if len(gas_only_cases) > 0:
            gas_only_cases = import_systems(gas_only_cases, "gas_only", area)
            for feature in gas_only_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(parse_profile(tags["electricity_import_profile"]), dtype=float)
                summed_elec_demand_profiles += np.array(parse_profile(tags["electricity_demand_profile"]), dtype=float)
 
                # Sums
                # total_gas_invest_cost += tags["gas_heating_cost_invest"] + tags["thermal_storage_cost_invest"] # Nur bei scenario_change
                total_heat_produced += float(tags["gas_heating_production"])
                total_gas_import += float(tags["gas_heating_energy_import"])
                total_gas_cost += float(tags["gas_heating_import_cost"])
                total_gas_emissions += float(tags["gas_heating_emissions"])
                total_electricity_cost += float(tags["building_electricity_cost"])
                total_building_electricity_emissions += float(tags["building_electricity_emissions"])
                total_gas_heating_costs_om += float(tags["gas_heating_costs_om"])
                total_thermal_storage_costs_om += float(tags["thermal_storage_costs_om"])
 
        if len(gas_pv_cases) > 0:
            gas_pv_cases = import_systems(gas_pv_cases, "gas_pv", area)
            for feature in gas_pv_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(parse_profile(tags["electricity_import_profile"]), dtype=float)
                summed_elec_demand_profiles += np.array(parse_profile(tags["electricity_demand_profile"]), dtype=float)
                summed_pv_production_profiles += np.array(parse_profile(tags["pv_generation_profiles"]), dtype=float)
                summed_elec_export_profiles += np.array(parse_profile(tags["electricity_export_profile"]), dtype=float)
 
                # Sums
                # total_gas_invest_cost += tags["gas_heating_cost_invest"] + tags["thermal_storage_cost_invest"] # Nur bei scenario_change
                total_heat_produced += float(tags["gas_heating_production"])
                total_gas_import += float(tags["gas_heating_energy_import"])
                total_gas_cost += float(tags["gas_heating_import_cost"])
                total_gas_emissions += float(tags["gas_heating_emissions"])
                total_electricity_cost += float(tags["building_electricity_cost"])
                total_building_electricity_emissions += float(tags["building_electricity_emissions"])
                total_photovoltaic_production += float(tags["photovoltaic_production"])
                total_gas_heating_costs_om += float(tags["gas_heating_costs_om"])
                total_thermal_storage_costs_om += float(tags["thermal_storage_costs_om"])
                total_photovoltaic_costs_om += float(tags["photovoltaic_costs_om"])
 
    # Load the required combinations for ASHP+gas hybrid heat pumps.
    # Beide Komponenten sind gleichzeitig installiert, daher fliessen die Werte
    # in BEIDE bestehenden Technologie-Totals (ashp_* UND gas_*) gleichzeitig ein.
    if len(ashp_gas_cases) > 0:
        ashp_gas_heat_demand = sum(float(f["properties"]["tags"]["heat_demand"]) for f in ashp_gas_cases)
        ashp_gas_share = ashp_gas_heat_demand / total_heat_demand
        energy_stats["ashp_gas_setting"] = ashp_gas_share * 100
 
        ashp_gas_only_cases = [f for f in ashp_gas_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        ashp_gas_pv_cases = [f for f in ashp_gas_cases if f["properties"]["tags"]["pv_activated"] == "true"]
 
        if len(ashp_gas_only_cases) > 0:
            ashp_gas_only_cases = import_systems(ashp_gas_only_cases, "ashp_gas_only", area)
            for feature in ashp_gas_only_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(parse_profile(tags["electricity_import_profile"]), dtype=float)
                summed_elec_demand_profiles += np.array(parse_profile(tags["electricity_demand_profile"]), dtype=float)
 
                # Sums -- ASHP share
                total_heat_produced += float(tags["ashp_production"])
                total_ashp_emissions += float(tags["ashp_emissions"])
                total_ashp_heating_costs_om += float(tags["ashp_costs_om"])
 
                # Sums -- gas share
                total_heat_produced += float(tags["gas_heating_production"])
                total_gas_import += float(tags["gas_heating_energy_import"])
                total_gas_cost += float(tags["gas_heating_import_cost"])
                total_gas_emissions += float(tags["gas_heating_emissions"])
                total_gas_heating_costs_om += float(tags["gas_heating_costs_om"])
 
                # Sums -- shared
                total_electricity_cost += float(tags["building_electricity_cost"])
                total_building_electricity_emissions += float(tags["building_electricity_emissions"])
                total_thermal_storage_costs_om += float(tags["thermal_storage_costs_om"])
 
        if len(ashp_gas_pv_cases) > 0:
            ashp_gas_pv_cases = import_systems(ashp_gas_pv_cases, "ashp_gas_pv", area)
            for feature in ashp_gas_pv_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(parse_profile(tags["electricity_import_profile"]), dtype=float)
                summed_elec_demand_profiles += np.array(parse_profile(tags["electricity_demand_profile"]), dtype=float)
                summed_pv_production_profiles += np.array(parse_profile(tags["pv_generation_profile"]), dtype=float)
                summed_elec_export_profiles += np.array(parse_profile(tags["electricity_export_profile"]), dtype=float)
 
                # Sums -- ASHP share
                total_heat_produced += float(tags["ashp_heating_production"])
                total_ashp_emissions += float(tags["ashp_heating_emissions"])
                total_ashp_heating_costs_om += float(tags["ashp_heating_costs_om"])
 
                # Sums -- gas share
                total_heat_produced += float(tags["gas_heating_production"])
                total_gas_import += float(tags["gas_heating_energy_import"])
                total_gas_cost += float(tags["gas_heating_import_cost"])
                total_gas_emissions += float(tags["gas_heating_emissions"])
                total_gas_heating_costs_om += float(tags["gas_heating_costs_om"])
 
                # Sums -- shared
                total_electricity_cost += float(tags["building_electricity_cost"])
                total_building_electricity_emissions += float(tags["building_electricity_emissions"])
                total_thermal_storage_costs_om += float(tags["thermal_storage_costs_om"])
                total_photovoltaic_production += float(tags["photovoltaic_production"])
                total_photovoltaic_costs_om += float(tags["photovoltaic_costs_om"])
 
    total_electricity_import = sum(summed_elec_import_profiles)
    total_electricity_export = sum(summed_elec_export_profiles)
    # total_pv_production kommt direkt aus dem annualen "photovoltaic_production"-Tag,
    # NOT from the profile array (the profile is kept separately for time-series purposes)
    total_emission = total_gas_emissions + total_ashp_emissions + total_building_electricity_emissions
    total_costs_om = (
        total_gas_heating_costs_om
        + total_ashp_heating_costs_om
        + total_thermal_storage_costs_om
        + total_photovoltaic_costs_om
    )
    self_sufficiency = 1 - total_electricity_import / total_electricity_demand
 
    # How much of the import could be avoided through local sharing (simultaneous export)
    # Only the minimum of import/export counts per timestep.
    sharable_per_timestep = np.minimum(summed_elec_import_profiles, summed_elec_export_profiles)
    reducible_import = sum(sharable_per_timestep)          # kWh that could be avoided through sharing
    remaining_import = total_electricity_import - reducible_import  # kWh that would still have to come from the grid
    energy_sharing_potential = reducible_import / total_electricity_import if total_electricity_import > 0 else 0.0
 
    # Electricity stats
    energy_stats["self_sufficiency"] = self_sufficiency * 100
    energy_stats["total_electricity_import"] = total_electricity_import
    energy_stats["total_electricity_export"] = total_electricity_export
    energy_stats["total_electricity_cost"] = total_electricity_cost
    energy_stats["reducible_import_kwh"] = reducible_import
    energy_stats["remaining_import_kwh"] = remaining_import
    energy_stats["energy_sharing_potential"] = energy_sharing_potential * 100
 
    # Production & emissions (combined across all technologies)
    energy_stats["total_pv_production"] = total_photovoltaic_production
    energy_stats["total_heat_production"] = total_heat_produced
    # /1e6: g -> t CO2, keeps the published stat within the API's integer range
    energy_stats["total_emission"] = total_emission / 1_000_000
 
    # Operation & maintenance costs, per technology and combined
    energy_stats["total_gas_heating_costs_om"] = total_gas_heating_costs_om
    energy_stats["total_ashp_heating_costs_om"] = total_ashp_heating_costs_om
    energy_stats["total_thermal_storage_costs_om"] = total_thermal_storage_costs_om
    energy_stats["total_photovoltaic_costs_om"] = total_photovoltaic_costs_om
    energy_stats["total_costs_om"] = total_costs_om
 
    # Gas stats
    energy_stats["total_gas_import"] = total_gas_import
    energy_stats["total_gas_cost"] = total_gas_cost
 
    # Total costs
    energy_stats["transformation_cost"] = total_transfomation_cost
 
    tagged_features = gas_only_cases + gas_pv_cases + hp_only_cases + hp_pv_cases + ashp_gas_only_cases + ashp_gas_pv_cases
 
    return energy_stats, tagged_features


def create_init_inputs(tagged_features):
    """Builds the initial values for this script's declared scenario inputs from the
    imported buildings.

    For inputs listed under INPUT_INIT["inputs_to_adapt"]["categories"], sets default/max
    to the count of buildings whose heat_technology matches that category; for inputs
    listed under ["bools"], sets default/max to the count of buildings where that tag is
    True. Every other input is passed through unchanged.

    Args:
        tagged_features: List of GeoJSON features with populated properties.tags.

    Returns:
        List of input definition dicts, ready to pass to declare_inputs.
    """
    input_patches = []
    num_tagged_features = len(tagged_features)
    
    inputs_to_adapt = INPUT_INIT["inputs_to_adapt"]
    inputs = INPUT_INIT["inputs"]
    
    for input in inputs:
        input_key = input["key"]
        if input_key in inputs_to_adapt["categories"]:
            if "min" in inputs_to_adapt["categories"][input_key]:
                input_min = len([feature for feature in tagged_features if feature["properties"]["tags"]["heat_technology"] == inputs_to_adapt["categories"][input_key]["min"]])
                input["min"] = input_min

            if "default" in inputs_to_adapt["categories"][input_key]:
                input_default = len([feature for feature in tagged_features if feature["properties"]["tags"]["heat_technology"] == inputs_to_adapt["categories"][input_key]["default"]])
                input["default"] = input_default

            input["max"] = num_tagged_features
            input_patches.append(input)

        elif input_key in inputs_to_adapt["bools"]:

            if "min" in inputs_to_adapt["bools"][input_key]:
                input_min = len([feature for feature in tagged_features if feature["properties"]["tags"][inputs_to_adapt["bools"][input_key]["min"]] == True])
                input["min"] = input_min

            if "default" in inputs_to_adapt["bools"][input_key]:
                input_default = len([feature for feature in tagged_features if feature["properties"]["tags"][inputs_to_adapt["bools"][input_key]["default"]] == True])
                input["default"] = input_default
        
            input["max"] = num_tagged_features
            input_patches.append(input)
        else: 
            input_patches.append(input)

    return input_patches


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


def _log_step(step_name, start_time, **extra_info):
    """Logs the duration and optional extra info (e.g. row count) for a pipeline step."""
    duration = time.perf_counter() - start_time
    extra_str = " ".join(f"{k}={v}" for k, v in extra_info.items())
    log.info(f"STEP DONE: {step_name} ({duration:.2f}s) {extra_str}".rstrip())


def process_new_scenario(scenario_id):
    """Runs the full import/computation/publish pipeline for a new scenario.

    Flow: determine the newest scenario's bounding box -> load OEP building data for that
    bbox -> load the scenario's OSM buildings -> spatially join both and build tags ->
    compute and import energy systems/statistics -> patch the building tags -> fetch
    existing stats -> publish the legends (refurbishment state, heating technology, heat/
    electricity cluster) -> publish the energy statistics as new or updated stats.

    Args:
        scenario_id: ID of the scenario to process.

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

    # 3. Load the scenario's OSM buildings
    t0 = time.perf_counter()
    osm_gdf, feature_list = get_osm_buildings(scenario_id)
    _log_step("get_osm_buildings", t0, n_osm_features=len(feature_list))

    # 4. Spatial join + tag aggregation
    t0 = time.perf_counter()
    tagged_features, scenario_features_df = create_insert_tags(buildings_data, osm_gdf, feature_list)
    _log_step("create_insert_tags", t0, n_patches=len(tagged_features))

    # 5. This is where the technologies would need to be imported from the OEP and matched to the buildings
    energy_stats, tagged_features = calc_systems(tagged_features, "nl")

    # calc_systems never sets this key itself; without it the "min_refurbishment_state"
    # stat would fall back to STATS_INIT's hardcoded value instead of the actual
    # min_refurb_state input default (no scenario inputs have been set by the user yet).
    energy_stats["min_refurbishment_state"] = next(
        i["default"] for i in INPUT_INIT["inputs"] if i["key"] == "min_refurb_state"
    )

    # Publish the initialized inputs
    init_inputs = create_init_inputs(tagged_features)
    declare_inputs(init_inputs)


    if not tagged_features:
        log.warning(f"No patches produced for scenario {scenario_id} -- skipping update_buildings")
    else:
        # 5. Patch building tags
        t0 = time.perf_counter()
        update_buildings(tagged_features)
        _log_step("update_buildings", t0, n_patches=len(tagged_features))

    # 6. Bestehende Stats abrufen
    t0 = time.perf_counter()
    existing_stats = fetch_stats(scenario_id)
    _log_step("fetch_stats", t0, n_stats=len(existing_stats))

    # 7. Declare legends (upserts by key)
    t0 = time.perf_counter()
    legend_definitions = [
        {"key": REFURB_STATE_KEY, **LEGEND_REFURB_STATE_SOURCE},
        {"key": TECH_KEY, **LEGEND_TECH_SOURCE},
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
    

if __name__ == "__main__":
    with open("event.json") as f:
        event = json.load(f)
    event_type = event["event_type"]
    payload = event["payload"]
    log.info(f"Handling {event_type} event")

    if event_type == "SCENARIO_CREATED":
        scenario_id = payload["id"]
    elif event_type == "MANUAL":
        scenario_id = payload["scenario_id"]
    else:
        scenario_id = None

    if scenario_id:
        process_new_scenario(scenario_id)
    else:
        log.warning(f"Received unhandled event type {event_type}, nothing to process") 
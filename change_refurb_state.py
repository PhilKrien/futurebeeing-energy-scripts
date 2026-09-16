"""Standalone/local variant of the refurbishment-state pipeline: for the newest
scenario reachable with this API key, lowers building refurbishment states to the
scenario's min_refurb_state input, recomputes heating/PV energy statistics via OEP
system imports, and publishes the updated building tags, heat-demand legend and
energy stats. Reads its API key and OEP/legend config from local files
(fubee_key.txt, oep_token.txt, energy_files/*.json) rather than the poller's
environment/API -- see change_refurb_state_backend.py for the version that runs
on the poller.
"""

import datetime
import json
import os
import logging
import requests
import numpy as np
import matplotlib as mpl
import matplotlib.colors as mcolors

import time

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
with open("energy_files/energy_import_variables.json", "r") as f:
     IMPORT_VARIABLES = json.load(f)

# Import the settings necessary for different legends
with open("energy_files/energy_legend_settings.json", "r") as f:
    LEGEND_SETTINGS = json.load(f)

# Import the settings necessary for different legends
with open("energy_files/energy_input_init.json", "r") as f:
    INPUT_INIT = json.load(f)

# Building Data fields from the OEP DB that should stay invisible
INVISIBLE_FIELDS_OEP = LEGEND_SETTINGS["invisible_fields_oep"]

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
    """Publishes a new stat for the energy calculations""" 
    log.info(f"Publishing {len(patches)} stats.")
    if len(patches) > 0:
        resp = requests.post(f"{API_BASE}/v1/stats", headers=HEADERS, json=patches)
        if not resp.ok:
            log.error(f"POST /v1/stats failed with {resp.status_code}: {resp.text[:1000]}")
        resp.raise_for_status()


def update_stats(patches):
    """patches: list of {"id": stat_id, ...fields to change} (name/statType/visible/source/
    startValue/scenarioValue). Only the fields you include are changed."""
    log.info(f"Updating {len(patches)} stats.")
    if len(patches) > 0:
        for stat in patches:
            resp = requests.patch(f"{API_BASE}/v1/stats", headers=HEADERS, json=[stat])
            if not resp.ok:
                print("Stat:", stat)
                print(f"FEHLER bei: {stat['name']}")
                print(resp.text)


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
                 

# Fetch the energy systems from the OEP based on the system_ids
def fetch_multiple_system_ids_advanced(system_ids, url, table_name):
    """Queries the OEP advanced-search API for every row of a table whose system_id is in
    the given set (system_id = sid1 OR system_id = sid2 OR ...).

    Args:
        system_ids: Iterable of system_id strings to search for.
        url: Base URL of the OEP API (e.g. OEP_BASE_URL); "/advanced/search" is appended.
        table_name: Name of the target table on the OEP (e.g. "hoogeveen_gas_only").

    Returns:
        List of raw data rows (result["data"]) from the OEP response.
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
    )
    res.raise_for_status()
    result = res.json()

    rowcount = result.get("content", {}).get("rowcount")
    if rowcount == 0:
        return []

    if "data" not in result:
        raise RuntimeError(
            f"OEP advanced search for table '{table_name}' returned no 'data' key "
            f"(rowcount={rowcount}); full response: {result}"
        )

    return result["data"]

     
     
# Cluster/measurement static_cols whose OEP system_id segment is always a float string,
# even when the value is exactly 0. Tags read back from the backend's hstore storage lose
# the ".0" for whole numbers (see format_like_hstore), so str(feature_tags[col]) alone
# gives "0" instead of "0.0" for e.g. a building with no south-facing roof, which then
# never matches the OEP table's system_id. size_class/nearest_city/roof_type are
# categorical strings and refurbishment_state is a plain int -- left as-is.
FLOAT_SYSTEM_ID_COLS = {"elec_cluster", "heat_cluster", "roof_cluster_eastwest", "roof_cluster_south"}

# Find and write the necessary system_ids to pull from OEP
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


# Add the downloaded system data to the features tags
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

        for field in INVISIBLE_FIELDS_OEP["systems"]:
            feature_tags[f"visible:{field}"] = "false"

        patched_features.append(feature)

    return patched_features


def fetch_inputs(scenario_id):
    """Current value of every input this script has declared, for this scenario - {key: value},
    already falling back to that input's own declared default for anything the user hasn't set."""
    log.info(f"Fetching scenario inputs for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/scenario-inputs", headers=HEADERS, params={"scenario_id": scenario_id})
    
    resp.raise_for_status()
    return resp.json()


# Convert the response data to a hashable dict of tags -> system_id: tags
def convert_response_data(response, import_column_names):
    """Converts the raw, column-less OEP rows (lists of values) into a dict of named
    columns per system_id.

    Assumes each row in response carries the system_id at index 1 and the actual data
    from index 2 onward (index 0 is presumably the table's own id column).

    Args:
        response: List of raw data rows (lists), as returned by
            fetch_multiple_system_ids_advanced.
        import_column_names: Full column name list of the source table (including id and
            system_id at position 0/1); only the part from index 2 onward is used for
            the mapping.

    Returns:
        Dict {system_id: {column_name: value, ...}} for fast lookup in patch_system_data.
    """
    response_data = {}
    data_column_names = import_column_names[2:]
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
    response = fetch_multiple_system_ids_advanced(combinations, OEP_BASE_URL, table_name)
    response_data = convert_response_data(response, import_column_names)

    # Patch the tags with the system data
    case_features = patch_system_data(case_features, response_data)
    
    return case_features


def calc_systems_update(tagged_features, area):
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
 
        total_heat_demand += float(tags["heat_cluster"])
        total_electricity_demand += float(tags["elec_cluster"])
 
        roof_total = float(tags["roof_cluster_eastwest"]) + float(tags["roof_cluster_south"])
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
        hp_heat_demand = sum(float(f["properties"]["tags"]["heat_cluster"]) for f in hp_cases)
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
                summed_pv_production_profiles += np.array(parse_profile(tags["pv_generation_profile"]), dtype=float)
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
        gas_heat_demand = sum(float(f["properties"]["tags"]["heat_cluster"]) for f in gas_cases)
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
                summed_pv_production_profiles += np.array(parse_profile(tags["pv_generation_profile"]), dtype=float)
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
        ashp_gas_heat_demand = sum(float(f["properties"]["tags"]["heat_cluster"]) for f in ashp_gas_cases)
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
                total_heat_produced += float(tags["ashp_production"])
                total_ashp_emissions += float(tags["ashpg_emissions"])
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
    energy_stats["total_emission"] = total_emission
 
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


def update_energy_patches(energy_stats, existing_stats, fetched_inputs):
    """Writes the computed energy_stats values into the matching existing stat rows.

    For every existing stat whose name is also a key in energy_stats, overwrites its
    scenarioValue with the (int-cast) computed value.

    Args:
        energy_stats: Dict {stat_name: value}, as returned by calc_systems_update.
        existing_stats: List of existing stat dicts for the scenario, as returned by
            fetch_stats.

    Returns:
        The existing_stats list, mutated in place.
    """
    set_status_quo = fetched_inputs["set_status_quo"]

    for energy_stat in existing_stats:
        name = energy_stat["name"]
        if name in energy_stats:
            if set_status_quo:
                energy_stat["startValue"]["quantative"] = int(energy_stats[name])
                energy_stat["scenarioValue"]["quantative"] = int(energy_stats[name])
            else:
                energy_stat["scenarioValue"]["quantative"] = int(energy_stats[name])

    return existing_stats


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


# Reset the set_status_quo button after the script ran
def reset_status_quo_toggle(init_inputs):
    for input in init_inputs:
        if input["key"] == "set_status_quo":
            input["default"] = False 

    return init_inputs



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


def process_scenario_changes(scenario_id):
    """Runs the full refurbishment-state pipeline for an existing scenario: fetch tagged
    buildings, stats and inputs -> lower refurbishment states per min_refurb_state ->
    recompute energy statistics and import the matching heating/PV system data -> patch
    the updated building tags -> publish the heat-demand legend -> publish the new/updated
    energy stats.

    Reads scenario_id from CLI/local context (this is the standalone/local variant --
    see change_refurb_state_backend.py for the poller-run one).

    Args:
        scenario_id: ID of the scenario to process.

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

    # 5. This is where the technologies would need to be imported from the OEP and matched to the buildings
    energy_stats, tagged_features = calc_systems_update(tagged_features, "nl")

    # calc_systems_update never touches this key itself; without it the
    # "min_refurbishment_state" stat stays frozen at its initial value and never
    # reflects the min_refurb_state slider (see update_energy_patches below).
    energy_stats["min_refurbishment_state"] = fetched_inputs["min_refurb_state"]

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

    # 7. Declare legend (upserts by key)
    t0 = time.perf_counter()

    # Heat demand legend
    heat_demand = np.array([feature["properties"]["tags"]["heat_cluster"] for feature in tagged_features])
    heat_demand_legend = build_demand_legend(heat_demand, "Heat demand", "heat_cluster")

    if heat_demand_legend is not None:
        declare_legend([heat_demand_legend])

    _log_step("declare_legend", t0)

    t0 = time.perf_counter()

    # Update the inputs
    init_inputs = create_init_inputs(tagged_features)

    init_inputs = reset_status_quo_toggle(init_inputs)

    declare_inputs(init_inputs)

    patches = update_energy_patches(energy_stats, existing_stats, fetched_inputs)

    stats_to_publish, stats_to_patch = check_patches(patches, existing_stats)

    if stats_to_publish:
        publish_energy_stats(stats_to_publish)
    if stats_to_patch:
        update_stats(stats_to_patch)
    _log_step("publish_energy_stats", t0)

    total_duration = time.perf_counter() - pipeline_start
    log.info(f"=== Finished pipeline for scenario {scenario_id} in {total_duration:.2f}s ===")
    

if __name__ == "__main__":
    scenarios = fetch_scenarios()
    newest_scenario = scenarios[-1]
    scenario_id = newest_scenario["id"]
    process_scenario_changes(scenario_id)
   
   


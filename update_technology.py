"""Standalone/local pipeline for updating a scenario's heating-technology mix: for the
newest scenario reachable with this API key, applies the configured heat-pump/gas/
district-heat/PV settings, recomputes energy statistics via OEP system imports, and
publishes the updated building tags plus the refurbishment-state, heat-technology,
heat-demand and electricity-demand legends and energy stats. Reads its API key and
OEP/import config from local files rather than the poller's environment/API.
Note: the technology-redistribution step (update_heat_techs), which this pipeline
calls unconditionally, currently raises a NameError -- see its docstring.
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
API_KEY = "0921c901-9413-4a87-b6f4-045b221e11c6.cf9f3945-8e72-4de1-ae36-147f990aba24"
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

REFURB_STATE_KEY = plot_settings["refurbishment_state"]["key"]
LEGEND_REFURB_STATE_SOURCE = json.dumps(plot_settings["refurbishment_state"]["source"])

TECH_KEY = plot_settings["tech"]["key"]   # must exactly match the tag key in build_building_tags
LEGEND_TECH_SOURCE = json.dumps(plot_settings["tech"]["source"])

# Building Data fields from the OEP DB that should stay invisible
INVISIBLE_FIELDS_OEP = plot_settings["invisible_fields_oep"]

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

def build_demand_legend(values, name, unit="kWh/a", n_max_buckets=4, cmap_name="YlOrRd"):
    """Builds a legend definition from a set of values, either as exact-match buckets or
    as evenly sized range buckets.

    Args:
        values: Array (or array-like) of raw values to build the legend from (e.g. all
            heat_cluster or elec_cluster values of a scenario).
        name: Display name of the legend (title-cased and used as "label").
        unit: Unit shown in the bucket labels.
        n_max_buckets: Threshold at or below which exact-match buckets are built instead
            of evenly sized range buckets.
        cmap_name: Name of the Matplotlib colormap used for the bucket colours.

    Returns:
        JSON string with the legend definition (label, category, unit, buckets), or None
        if no values remain after removing NaNs.
    """
    float_values = values.astype(float)
    clean_values = float_values[~np.isnan(float_values)]
    unique_values = np.unique(clean_values)
    n_unique = len(unique_values)
    print(n_unique)

    if n_unique == 0:
        log.warning(f"No values available to build '{name}' legend -- skipping")
        return None

    if n_unique <= n_max_buckets:
        colours = sample_colours_from_cmap(cmap_name, n_unique)
        buckets = [
            {
                "kind": "range",
                "min": unique_values[i] - 1,
                "max": unique_values[i] + 1,
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

    return json.dumps({
        "label": f"{name.replace('_', ' ').title()}",
        "category": "ENERGY",
        "unit": unit,
        "buckets": buckets,
    })

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
    """Every stats row (including hidden stat_type: 'metadata' legend rows) for the scenario."""
    log.info(f"Fetching stats for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/stats", headers=HEADERS, params={"scenarioId": scenario_id})
    resp.raise_for_status()
    stats = resp.json()
    log.info(f"Fetched {len(stats)} stats for scenario {scenario_id}")
    return stats


def fetch_inputs(scenario_id):
    """Current value of every input this script has declared, for this scenario - {key: value},
    already falling back to that input's own declared default for anything the user hasn't set."""
    log.info(f"Fetching scenario inputs for scenario {scenario_id}")
    resp = requests.get(f"{API_BASE}/v1/scenario-inputs", headers=HEADERS, params={"scenario_id": scenario_id})
    resp.raise_for_status()
    return resp.json()


def publish_legend(scenario_id, existing_stats, label_key, legend_source):
    """Publishes or updates the (hidden) legend definition for this scenario."""
    existing = next((stat for stat in existing_stats if stat["name"] == label_key), None)

    if existing:
        log.info(f"Updating {label_key} legend for scenario {scenario_id}")
        resp = requests.patch(f"{API_BASE}/v1/stats", headers=HEADERS, json=[
            {"id": existing["id"], "source": legend_source},
        ])
    else:
        log.info(f"Publishing new {label_key} legend for scenario {scenario_id}")
        resp = requests.post(f"{API_BASE}/v1/stats", headers=HEADERS, json=[{
            "name": label_key,
            "scenarioId": scenario_id,
            "startValue": {"quantative": 0},
            "scenarioValue": {"quantative": 0},
            "statType": "metadata",
            "visible": False,
            "source": legend_source,
        }])
    resp.raise_for_status()
    log.info(f"Published {label_key} legend for scenario {scenario_id}")


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
        resp = requests.patch(f"{API_BASE}/v1/stats", headers=HEADERS, json=patches)
        resp.raise_for_status()
                 

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
        headers=HEADERS,
    )
    res.raise_for_status()
    return res.json()["data"]


with open("import_variables.json", "r") as f:
     IMPORT_VARIABLES = json.load(f)
     
     
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
            combination.append(str(feature_tags[col]))
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
        The tagged_features list, whose tag dicts have been extended in place with the
        matching system data and the "visible:" flags.
    """
    for feature in tagged_features:
        feature_tags = feature["properties"]["tags"]
        feature_system_id = feature_tags["system_id"]
        
        if feature_system_id not in response_data:
            print("kein OEP-Eintrag fuer dieses System -- ueberspringen")
            continue
        
        tech_data = response_data[feature_system_id]
        feature_tags.update(tech_data)
        
        for field in INVISIBLE_FIELDS_OEP["systems"]:
            feature_tags[f"visible:{field}"] = "false"
            
    return tagged_features


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
    
    # table_name = area + f"_{case_name}""
    table_name = "futurebeeing_test_gas_combinations"
    
    # Import and merge system data
    response = fetch_multiple_system_ids_advanced(combinations, OEP_BASE_URL, table_name)
    response_data = convert_response_data(response, import_column_names)

    # Patch the tags with the system data
    case_features = patch_system_data(case_features, response_data)
    
    return case_features


def update_heat_techs(tagged_features, existing_stats):
    """Intended to redistribute buildings across heating technologies according to the
    ashp/gas/district-heat/pv scenario-input settings, then recompute per-technology heat
    demand and PV roof area.

    Note: as currently written this function references several local variables
    (pv_potential_total, pv_installed_total, hp_cases, ashp_gas_cases, gas_cases) before
    they are ever assigned. It IS called from process_scenario_changes, so every run of
    this script currently raises a NameError here.

    Args:
        tagged_features: List of GeoJSON features with populated properties.tags.
        existing_stats: List of existing stat dicts for the scenario, as returned by
            fetch_stats.

    Returns:
        Tuple (tagged_features, energy_stats) -- as intended; not actually reached in
        the function's current state.
    """
    ashp_only_cases = []
    ashp_pv_cases = []
    gas_only_cases = []
    gas_pv_cases = []
    ashp_gas_only_cases = []
    ashp_gas_pv_cases = []
    
    ashphp_heat_demand = 0
    gas_heat_demand = 0
    dh_heat_demand = 0
    ashp_heat_demand = 0
    pv_roof_area = 0
    
    ashp_setting = next((s["scenarioValue"]["quantative"] for index, s in enumerate(existing_stats) if s["name"] == "ashp_setting"), None)
    gas_setting = next((s["scenarioValue"]["quantative"] for index, s in enumerate(existing_stats) if s["name"] == "gas_setting"), None)
    dh_setting = next((s["scenarioValue"]["quantative"] for index, s in enumerate(existing_stats) if s["name"] == "dh_setting"), None)
    ashp_gas_setting = next((s["scenarioValue"]["quantative"] for index, s in enumerate(existing_stats) if s["name"] == "ashp_gas_setting"), None)
    pv_setting = next((s["scenarioValue"]["quantative"] for index, s in enumerate(existing_stats) if s["name"] == "pv_setting"), None)
    
    
    for feature in tagged_features:
        tags = feature["properties"]["tags"]
 
        roof_total = tags["roof_cluster_eastwest"] + tags["roof_cluster_south"]
        pv_potential_total += roof_total
        if tags["pv_activated"] == "true":
            pv_installed_total += roof_total
 
        if tags["heat_technology"] == "hp":
            if tags["pv_activated"] == "True":
                hp_cases.append(feature)
        elif tags["heat_technology"] == "ashp_gas":
            ashp_gas_cases.append(feature)
        elif tags["heat_technology"] in ("gas", "district_heat"):
            gas_cases.append(feature)
    

    return tagged_features, energy_stats

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
 
        total_heat_demand += tags["heat_demand"]
        total_electricity_demand += tags["elec_demand"]
 
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
    energy_stats["pv_setting"] = pv_installed_total / pv_potential_total
 
    # Load the required combinations from the MOSAIQ DB for the HP cases
    if len(hp_cases) > 0:
        hp_heat_demand = sum(f["properties"]["tags"]["heat_demand"] for f in hp_cases)
        hp_share = hp_heat_demand / total_heat_demand
        energy_stats["ashp_setting"] = hp_share
 
        hp_only_cases = [f for f in hp_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        hp_pv_cases = [f for f in hp_cases if f["properties"]["tags"]["pv_activated"] == "true"]
 
        if len(hp_only_cases) > 0:
            hp_only_cases = import_systems(hp_only_cases, "hp_only", area)
            for feature in hp_only_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(tags["electricity_import_profiles"])
                summed_elec_demand_profiles += np.array(tags["electricity_demand_profiles"])
 
                # Sums
                total_heat_produced += tags["ashp_heating_production"]
                total_ashp_emissions += tags["ashp_heating_emissions"]
                total_electricity_cost += tags["building_electricity_cost"]
                total_building_electricity_emissions += tags["building_electricity_emissions"]
                total_ashp_heating_costs_om += tags["ashp_heating_costs_om"]
                total_thermal_storage_costs_om += tags["thermal_storage_costs_om"]
 
        if len(hp_pv_cases) > 0:
            hp_pv_cases = import_systems(hp_pv_cases, "hp_pv", area)
            for feature in hp_pv_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(tags["electricity_import_profiles"])
                summed_elec_demand_profiles += np.array(tags["electricity_demand_profiles"])
                summed_pv_production_profiles += np.array(tags["pv_generation_profiles"])
                summed_elec_export_profiles += np.array(tags["electricity_export_profiles"])
 
                # Sums
                total_heat_produced += tags["ashp_heating_production"]
                total_ashp_emissions += tags["ashp_heating_emissions"]
                total_electricity_cost += tags["building_electricity_cost"]
                total_building_electricity_emissions += tags["building_electricity_emissions"]
                total_photovoltaic_production += tags["photovoltaic_production"]
                total_ashp_heating_costs_om += tags["ashp_heating_costs_om"]
                total_thermal_storage_costs_om += tags["thermal_storage_costs_om"]
                total_photovoltaic_costs_om += tags["photovoltaic_costs_om"]
 
    # Load the required combinations from the MOSAIQ DB for the gas cases
    if len(gas_cases) > 0:
        gas_heat_demand = sum(f["properties"]["tags"]["heat_demand"] for f in gas_cases)
        gas_share = gas_heat_demand / total_heat_demand
        energy_stats["gas_setting"] = gas_share
 
        gas_only_cases = [f for f in gas_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        gas_pv_cases = [f for f in gas_cases if f["properties"]["tags"]["pv_activated"] == "true"]
 
        if len(gas_only_cases) > 0:
            gas_only_cases = import_systems(gas_only_cases, "gas_only", area)
            for feature in gas_only_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(tags["electricity_import_profiles"])
                summed_elec_demand_profiles += np.array(tags["electricity_demand_profiles"])
 
                # Sums
                # total_gas_invest_cost += tags["gas_heating_cost_invest"] + tags["thermal_storage_cost_invest"] # Nur bei scenario_change
                total_heat_produced += tags["gas_heating_production"]
                total_gas_import += tags["gas_heating_energy_import"]
                total_gas_cost += tags["gas_heating_import_cost"]
                total_gas_emissions += tags["gas_heating_emissions"]
                total_electricity_cost += tags["building_electricity_cost"]
                total_building_electricity_emissions += tags["building_electricity_emissions"]
                total_gas_heating_costs_om += tags["gas_heating_costs_om"]
                total_thermal_storage_costs_om += tags["thermal_storage_costs_om"]
 
        if len(gas_pv_cases) > 0:
            gas_pv_cases = import_systems(gas_pv_cases, "gas_pv", area)
            for feature in gas_pv_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(tags["electricity_import_profiles"])
                summed_elec_demand_profiles += np.array(tags["electricity_demand_profiles"])
                summed_pv_production_profiles += np.array(tags["pv_generation_profiles"])
                summed_elec_export_profiles += np.array(tags["electricity_export_profiles"])
 
                # Sums
                # total_gas_invest_cost += tags["gas_heating_cost_invest"] + tags["thermal_storage_cost_invest"] # Nur bei scenario_change
                total_heat_produced += tags["gas_heating_production"]
                total_gas_import += tags["gas_heating_energy_import"]
                total_gas_cost += tags["gas_heating_import_cost"]
                total_gas_emissions += tags["gas_heating_emissions"]
                total_electricity_cost += tags["building_electricity_cost"]
                total_building_electricity_emissions += tags["building_electricity_emissions"]
                total_photovoltaic_production += tags["photovoltaic_production"]
                total_gas_heating_costs_om += tags["gas_heating_costs_om"]
                total_thermal_storage_costs_om += tags["thermal_storage_costs_om"]
                total_photovoltaic_costs_om += tags["photovoltaic_costs_om"]
 
    # Load the required combinations for ASHP+gas hybrid heat pumps.
    # Beide Komponenten sind gleichzeitig installiert, daher fliessen die Werte
    # in BEIDE bestehenden Technologie-Totals (ashp_* UND gas_*) gleichzeitig ein.
    if len(ashp_gas_cases) > 0:
        ashp_gas_heat_demand = sum(f["properties"]["tags"]["heat_demand"] for f in ashp_gas_cases)
        ashp_gas_share = ashp_gas_heat_demand / total_heat_demand
        energy_stats["ashp_gas_setting"] = ashp_gas_share
 
        ashp_gas_only_cases = [f for f in ashp_gas_cases if f["properties"]["tags"]["pv_activated"] == "false"]
        ashp_gas_pv_cases = [f for f in ashp_gas_cases if f["properties"]["tags"]["pv_activated"] == "true"]
 
        if len(ashp_gas_only_cases) > 0:
            ashp_gas_only_cases = import_systems(ashp_gas_only_cases, "ashp_gas_only", area)
            for feature in ashp_gas_only_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(tags["electricity_import_profiles"])
                summed_elec_demand_profiles += np.array(tags["electricity_demand_profiles"])
 
                # Sums -- ASHP share
                total_heat_produced += tags["ashp_heating_production"]
                total_ashp_emissions += tags["ashp_heating_emissions"]
                total_ashp_heating_costs_om += tags["ashp_heating_costs_om"]
 
                # Sums -- gas share
                total_heat_produced += tags["gas_heating_production"]
                total_gas_import += tags["gas_heating_energy_import"]
                total_gas_cost += tags["gas_heating_import_cost"]
                total_gas_emissions += tags["gas_heating_emissions"]
                total_gas_heating_costs_om += tags["gas_heating_costs_om"]
 
                # Sums -- shared
                total_electricity_cost += tags["building_electricity_cost"]
                total_building_electricity_emissions += tags["building_electricity_emissions"]
                total_thermal_storage_costs_om += tags["thermal_storage_costs_om"]
 
        if len(ashp_gas_pv_cases) > 0:
            ashp_gas_pv_cases = import_systems(ashp_gas_pv_cases, "ashp_gas_pv", area)
            for feature in ashp_gas_pv_cases:
                tags = feature["properties"]["tags"]
 
                # Profiles
                summed_elec_import_profiles += np.array(tags["electricity_import_profiles"])
                summed_elec_demand_profiles += np.array(tags["electricity_demand_profiles"])
                summed_pv_production_profiles += np.array(tags["pv_generation_profiles"])
                summed_elec_export_profiles += np.array(tags["electricity_export_profiles"])
 
                # Sums -- ASHP share
                total_heat_produced += tags["ashp_heating_production"]
                total_ashp_emissions += tags["ashp_heating_emissions"]
                total_ashp_heating_costs_om += tags["ashp_heating_costs_om"]
 
                # Sums -- gas share
                total_heat_produced += tags["gas_heating_production"]
                total_gas_import += tags["gas_heating_energy_import"]
                total_gas_cost += tags["gas_heating_import_cost"]
                total_gas_emissions += tags["gas_heating_emissions"]
                total_gas_heating_costs_om += tags["gas_heating_costs_om"]
 
                # Sums -- shared
                total_electricity_cost += tags["building_electricity_cost"]
                total_building_electricity_emissions += tags["building_electricity_emissions"]
                total_thermal_storage_costs_om += tags["thermal_storage_costs_om"]
                total_photovoltaic_production += tags["photovoltaic_production"]
                total_photovoltaic_costs_om += tags["photovoltaic_costs_om"]
 
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
    energy_stats["self_sufficiency"] = self_sufficiency
    energy_stats["total_electricity_import"] = total_electricity_import
    energy_stats["total_electricity_export"] = total_electricity_export
    energy_stats["total_electricity_cost"] = total_electricity_cost
    energy_stats["reducible_import_kwh"] = reducible_import
    energy_stats["remaining_import_kwh"] = remaining_import
    energy_stats["energy_sharing_potential"] = energy_sharing_potential
 
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


def update_energy_patches(energy_stats, existing_stats):
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
    for energy_stat in existing_stats:
        name = energy_stat["name"]
        if name in energy_stats:
            energy_stat["scenarioValue"]["quantative"] = energy_stats[name]

    return existing_stats

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
    """Runs the full heating-technology pipeline for an existing scenario: fetch tagged
    buildings and stats, apply the (currently hardcoded) heat-pump/gas/district-heat
    technology split, recompute energy statistics and import the matching heating/PV
    system data, patch the updated building tags, publish the refurbishment-state,
    heat-technology, heat-demand and electricity-demand legends, and publish the new/
    updated energy stats.

    Args:
        scenario_id: ID of the scenario to process.

    Returns:
        None. All results are persisted directly via the API endpoints (update_buildings,
        publish_legend, publish_energy_stats, update_stats).
    """
    pipeline_start = time.perf_counter()
    log.info(f"=== Starting pipeline for scenario {scenario_id} ===")

    tagged_features = fetch_buildings(scenario_id)
    
    existing_stats = fetch_stats(scenario_id)
    
    # Set the setings -> optimally this should happen in the frontend
    hp_setting = 100 # In percent of all heat demand that should be covered with heat pumps
    hp_setting_stat = next((s for index, s in enumerate(existing_stats) if s["name"] == "ashp_setting"), None)
    if hp_setting_stat: 
        hp_setting_stat["scenarioValue"]["quantative"] = hp_setting
    gas_setting = 0 # In percent of all heat demand that should be covered with heat pumps
    gas_setting_stat = next((s for index, s in enumerate(existing_stats) if s["name"] == "gas_setting"), None)
    if gas_setting_stat: 
        gas_setting_stat["scenarioValue"]["quantative"] = gas_setting
    dh_setting = 0 # In percent of all heat demand that should be covered with heat pumps
    dh_setting_stat = next((s for index, s in enumerate(existing_stats) if s["name"] == "dh_setting"), None)
    if dh_setting_stat: 
        dh_setting_stat["scenarioValue"]["quantative"] = dh_setting
        
    tagged_features, energy_stats = update_heat_techs(tagged_features, existing_stats)

    # 5. This is where the technologies would need to be imported from the OEP and matched to the buildings
    energy_stats, tagged_features = calc_systems_update(tagged_features, "nl")

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

    # 7. Publish legend (if not already present)
    t0 = time.perf_counter()
    publish_legend(scenario_id, existing_stats, REFURB_STATE_KEY, LEGEND_REFURB_STATE_SOURCE)
    
    # Publish the heat technology legend
    publish_legend(scenario_id, existing_stats, TECH_KEY, LEGEND_TECH_SOURCE)
    
    # Create and publish heat demand legend
    heat_demand = np.array([feature["properties"]["tags"]["heat_cluster"] for feature in tagged_features])
    heat_demand_legend = build_demand_legend("heat_demand", heat_demand, "Heat demand")
    if heat_demand_legend is not None:
        publish_legend(scenario_id, existing_stats, "heat_demand", heat_demand_legend)
    
    # Create and publish elec demand legend
    elec_demand = np.array([feature["properties"]["tags"]["elec_cluster"] for feature in tagged_features])
    elec_demand_legend = build_demand_legend("elec_demand", elec_demand, "Electricity demand")
    publish_legend(scenario_id, existing_stats, "elec_demand", elec_demand_legend)

    _log_step("publish_legend", t0)

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
    

if __name__ == "__main__":
    scenarios = fetch_scenarios()
    newest_scenario = scenarios[-1]
    scenario_id = newest_scenario["id"]

    process_scenario_changes(scenario_id)
    
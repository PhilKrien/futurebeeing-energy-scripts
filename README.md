# FuBee skripts -- Function Reference

This documents the poller scripts that actually run in FuBee: `check_stats.py`, `change_refurb_state_backend.py` and `data_import_backend.py`. Each is triggered by a poller event and reads its API key and script config (JSON files) from the poller's environment and the `/v1/scripts/files` endpoint.

**Not covered below** (excluded on purpose, not just missed): `change_refurb_state.py` and `data_import.py` are the local/standalone variants of the two `_backend` scripts above -- same pipeline, but reading config from local files for manual/offline testing; they are not deployed to FuBee. `update_technology.py` has no `_backend` counterpart at all, so it isn't deployed either -- and as documented in its own module docstring, its `process_scenario_changes` currently raises a `NameError` in `update_heat_techs` on every run, so it wouldn't be usable as-is even if deployed. If any of these three get promoted to production, re-run this reference including them.

## Scripts

| Script | Purpose |
|---|---|
| [`check_stats.py`](check_stats.py) | Poller script: on a STATS_UPDATED or MANUAL event, fetches and prints every stats row for the event's scenario. |
| [`change_refurb_state_backend.py`](change_refurb_state_backend.py) | Poller-run variant of the refurbishment-state pipeline: on a SCENARIO_CHANGED or MANUAL event, lowers the event's scenario's building refurbishment states to its min_refurb_state input, recomputes heating/PV energy statistics via OEP system imports, and publishes the updated building tags, heat-demand legend and energy stats. |
| [`data_import_backend.py`](data_import_backend.py) | Poller-run variant of the new-scenario import pipeline: on a SCENARIO_CREATED or MANUAL event, loads OEP building data (BAG) for the scenario's bounding box, spatially joins it to the scenario's OSM buildings, computes heating/PV energy statistics via OEP system imports, declares the initial scenario inputs, and publishes the building tags, legends and energy stats. |

See each file's own module docstring (top of the file) for the full pipeline description, config source and trigger event.

## Function reference

Every function defined across the three deployed scripts, alphabetically. **Used in** lists every one of these scripts that defines a function of that name (a function also present in one of the excluded local/dev scripts is not counted there); where the implementation differs between scripts, each variant is shown separately.

| Function | Used in (n) |
|---|---|
| [`_log_step`](#_log_step) | 2 |
| [`build_building_tags`](#build_building_tags) | 1 |
| [`build_demand_legend`](#build_demand_legend) | 2 |
| [`calc_systems`](#calc_systems) | 1 |
| [`calc_systems_update`](#calc_systems_update) | 1 |
| [`check_patches`](#check_patches) | 2 |
| [`check_refurb_state`](#check_refurb_state) | 1 |
| [`convert_response_data`](#convert_response_data) | 2 |
| [`create_energy_patches`](#create_energy_patches) | 1 |
| [`create_init_inputs`](#create_init_inputs) | 1 |
| [`create_insert_tags`](#create_insert_tags) | 1 |
| [`create_system_id`](#create_system_id) | 2 |
| [`declare_inputs`](#declare_inputs) | 1 |
| [`fetch_buildings`](#fetch_buildings) | 1 |
| [`fetch_inputs`](#fetch_inputs) | 1 |
| [`fetch_multiple_system_ids_advanced`](#fetch_multiple_system_ids_advanced) | 2 |
| [`fetch_scenarios`](#fetch_scenarios) | 1 |
| [`fetch_stats`](#fetch_stats) | 3 |
| [`fetch_tagged_buildings`](#fetch_tagged_buildings) | 1 |
| [`format_like_hstore`](#format_like_hstore) | 2 |
| [`get_osm_buildings`](#get_osm_buildings) | 1 |
| [`get_scenario_bbox`](#get_scenario_bbox) | 1 |
| [`import_oep_bbox_data`](#import_oep_bbox_data) | 1 |
| [`import_systems`](#import_systems) | 2 |
| [`patch_system_data`](#patch_system_data) | 2 |
| [`process_new_scenario`](#process_new_scenario) | 1 |
| [`process_scenario_changes`](#process_scenario_changes) | 1 |
| [`publish_energy_stats`](#publish_energy_stats) | 2 |
| [`publish_legend`](#publish_legend) | 2 |
| [`sample_colours_from_cmap`](#sample_colours_from_cmap) | 2 |
| [`update_buildings`](#update_buildings) | 2 |
| [`update_energy_patches`](#update_energy_patches) | 1 |
| [`update_stats`](#update_stats) | 2 |

### `_log_step`

**Signature:** `_log_step(step_name, start_time, **extra_info)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Logs the duration and optional extra info (e.g. row count) for a pipeline step.
```

### `build_building_tags`

**Signature:** `build_building_tags(row)`  
**Used in (1):** `data_import_backend.py`

```text
Builds the tag dict for a single building.

Args:
    row: A namedtuple row (from GeoDataFrame.itertuples()) with the OEP building
        columns, as produced by the join in create_insert_tags.

Returns:
    Dict of building tags ready to merge into a feature's properties.tags, with the
    OEP-only fields also marked invisible via "visible:<field>": "false".
```

### `build_demand_legend`

**Signature:** `build_demand_legend(values, name, unit='kWh/a', n_max_buckets=4, cmap_name='YlOrRd')`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Builds a legend definition from a set of values, either as exact-match buckets or
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
```

### `calc_systems`

**Signature:** `calc_systems(tagged_features, area)`  
**Used in (1):** `data_import_backend.py`

```text
Computes energy statistics from tagged_features and calls import_systems for the
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
```

### `calc_systems_update`

**Signature:** `calc_systems_update(tagged_features, area)`  
**Used in (1):** `change_refurb_state_backend.py`

```text
Computes energy statistics from tagged_features and calls import_systems for the
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
```

### `check_patches`

**Signature:** `check_patches(patches, existing_stats)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Splits patches into those that need to be published as new stats and those that
update an existing one.
```

### `check_refurb_state`

**Signature:** `check_refurb_state(tagged_features, fetched_inputs)`  
**Used in (1):** `change_refurb_state_backend.py`

```text
Lowers each building's refurbishment_state (and the heat_demand/heat_cluster values
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
```

### `convert_response_data`

**Signature:** `convert_response_data(response, import_column_names)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Converts the raw, column-less OEP rows (lists of values) into a dict of named
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
```

### `create_energy_patches`

**Signature:** `create_energy_patches(energy_stats, scenario_id)`  
**Used in (1):** `data_import_backend.py`

```text
Builds the patch list for the stats endpoint from the STATS_INIT template and the
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
```

### `create_init_inputs`

**Signature:** `create_init_inputs(tagged_features)`  
**Used in (1):** `data_import_backend.py`

```text
Builds the initial values for this script's declared scenario inputs from the
imported buildings.

For inputs listed under INPUT_INIT["inputs_to_adapt"]["categories"], sets default/max
to the count of buildings whose heat_technology matches that category; for inputs
listed under ["bools"], sets default/max to the count of buildings where that tag is
True. Every other input is passed through unchanged.

Args:
    tagged_features: List of GeoJSON features with populated properties.tags.

Returns:
    List of input definition dicts, ready to pass to declare_inputs.
```

### `create_insert_tags`

**Signature:** `create_insert_tags(buildings_data, osm_gdf, feature_list)`  
**Used in (1):** `data_import_backend.py`

```text
Spatially joins OEP building data (BAG) to OSM features and builds the resulting
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
```

### `create_system_id`

**Signature:** `create_system_id(tagged_features, static_cols)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Builds a unique, hyphen-separated system_id for each feature from its static_cols
values and writes it straight into the feature's tag dict.

Args:
    tagged_features: List of GeoJSON features with populated properties.tags.
    static_cols: List of tag keys the system_id is composed from (order determines the
        order within the system_id), e.g. from IMPORT_VARIABLES["static_cols"][case_name].

Returns:
    Set of every (unique) system_id string generated across all features.
```

### `declare_inputs`

**Signature:** `declare_inputs(definitions)`  
**Used in (1):** `data_import_backend.py`

```text
Declares (or updates) this script's own user-adjustable scenario inputs - shown as
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
card with no header.
```

### `fetch_buildings`

**Signature:** `fetch_buildings(scenario_id)`  
**Used in (1):** `data_import_backend.py`

```text
Every building/area GeoJSON Feature in the scenario - raw hstore tags are in
feature["properties"]["tags"], which can be None (not just missing) for features with no tags.
```

### `fetch_inputs`

**Signature:** `fetch_inputs(scenario_id)`  
**Used in (1):** `change_refurb_state_backend.py`

```text
Current value of every input this script has declared, for this scenario - {key: value},
already falling back to that input's own declared default for anything the user hasn't set.
```

### `fetch_multiple_system_ids_advanced`

**Signature:** `fetch_multiple_system_ids_advanced(system_ids, url, table_name)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Queries the OEP advanced-search API for every row of a table whose system_id is in
the given set (system_id = sid1 OR system_id = sid2 OR ...).

Args:
    system_ids: Iterable of system_id strings to search for.
    url: Base URL of the OEP API (e.g. OEP_BASE_URL); "/advanced/search" is appended.
    table_name: Name of the target table on the OEP (e.g. "hoogeveen_gas_only").

Returns:
    List of raw data rows (result["data"]) from the OEP response.
```

### `fetch_scenarios`

**Signature:** `fetch_scenarios()`  
**Used in (1):** `data_import_backend.py`

```text
Every scenario this key's municipalities cover, with id/name/bbox.
```

### `fetch_stats`

**Signature:** `fetch_stats(scenario_id)`  
**Used in (3):** `check_stats.py`, `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Every stats row (including hidden stat_type: 'metadata' legend rows) for the scenario.
```

### `fetch_tagged_buildings`

**Signature:** `fetch_tagged_buildings(scenario_id)`  
**Used in (1):** `change_refurb_state_backend.py`

```text
Every building/area GeoJSON Feature in the scenario - raw hstore tags are in
feature["properties"]["tags"], which can be None (not just missing) for features with no tags.
```

### `format_like_hstore`

**Signature:** `format_like_hstore(v)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Mirrors how Python floats get serialised when written to hstore -- integer-valued
floats lose their ".0" suffix, real decimals are kept as-is.
```

### `get_osm_buildings`

**Signature:** `get_osm_buildings(scenario_id)`  
**Used in (1):** `data_import_backend.py`

```text
Loads a scenario's OSM buildings/areas and turns them into a GeoDataFrame.

Args:
    scenario_id: ID of the scenario whose OSM features should be loaded.

Returns:
    Tuple (osm_gdf, feature_list):
        osm_gdf: GeoDataFrame (CRS EPSG:4326) with the OSM features as polygon
            geometries.
        feature_list: The underlying raw GeoJSON feature list (as returned by
            fetch_buildings), e.g. for later id-based patches.
```

### `get_scenario_bbox`

**Signature:** `get_scenario_bbox()`  
**Used in (1):** `data_import_backend.py`

```text
Determines the bounding box of the most recently created scenario.

Fetches every scenario for this API key, finds the one with the newest "created_at"
(smallest difference from "now"), and returns its bbox.

Returns:
    The bbox of the newest scenario, in the format supplied by the API (a
    [min_lon, min_lat, max_lon, max_lat]-style list/tuple).
```

### `import_oep_bbox_data`

**Signature:** `import_oep_bbox_data(bbox)`  
**Used in (1):** `data_import_backend.py`

```text
Loads every building from the OEP table supply.nl_mosaiq_phase_1 whose geometry
intersects the given bounding box.

Builds an advanced-search query against the OEP (ST_Intersects with ST_MakeEnvelope
built from the bbox), converts the returned GeoJSON geometry column into real
geometry objects via shapely.shape, and returns the result as a GeoDataFrame.

Args:
    bbox: Bounding box as [min_lon, min_lat, max_lon, max_lat] (order matches how
        bbox[0..3] are used when building the envelope).

Returns:
    GeoDataFrame (CRS EPSG:4326) with every column in COLUMNS plus "geometry".
```

### `import_systems`

**Signature:** `import_systems(case_features, case_name, area)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Imports the matching technical system data from the OEP for one technology/PV case
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
```

### `patch_system_data`

**Signature:** `patch_system_data(tagged_features, response_data)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Enriches each feature with the technical system data matching its system_id.

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
```

### `process_new_scenario`

**Signature:** `process_new_scenario(scenario_id)`  
**Used in (1):** `data_import_backend.py`

```text
Runs the full import/computation/publish pipeline for a new scenario.

Flow: determine the newest scenario's bounding box -> load OEP building data for that
bbox -> load the scenario's OSM buildings -> spatially join both and build tags ->
compute and import energy systems/statistics -> patch the building tags -> fetch
existing stats -> publish the legends (refurbishment state, heating technology, heat/
electricity cluster) -> publish the energy statistics as new or updated stats.

Args:
    scenario_id: ID of the scenario to process.

Returns:
    None. All results are persisted directly via the API endpoints (update_buildings,
    publish_legend, publish_energy_stats, update_stats); nothing is returned.
```

### `process_scenario_changes`

**Signature:** `process_scenario_changes(scenario_id)`  
**Used in (1):** `change_refurb_state_backend.py`

```text
Runs the full refurbishment-state pipeline for one scenario, triggered by a
SCENARIO_CHANGED or MANUAL poller event: fetch tagged buildings, stats and inputs ->
lower refurbishment states per min_refurb_state -> recompute energy statistics and
import the matching heating/PV system data -> patch the updated building tags ->
publish the heat-demand legend -> publish the new/updated energy stats (also
resetting the status-quo baseline when the scenario's "set_status_quo" input is
on -- see update_energy_patches).

Args:
    scenario_id: ID of the scenario to process (from the triggering event's payload).

Returns:
    None. All results are persisted directly via the API endpoints (update_buildings,
    publish_legend, publish_energy_stats, update_stats).
```

### `publish_energy_stats`

**Used in (2), implementation differs by script:**

- `publish_energy_stats(patches)` in `change_refurb_state_backend.py`:
```text
Publishes a new stat for the energy calculations
```
- `publish_energy_stats(patches)` in `data_import_backend.py`:
```text
Publishes a new stat for the energy calculations.

Sends the stats one at a time (instead of as a batch) so that a single invalid stat
doesn't block the whole publish -- the failing entry is logged (including the full
response) and the exception is then re-raised.

Args:
    patches: List of new stat dicts (name, scenarioId, startValue, scenarioValue,
        statType, visible, source) to publish via POST.
```

### `publish_legend`

**Signature:** `publish_legend(scenario_id, existing_stats, label_key, legend_source)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Publishes or updates the (hidden) legend definition for this scenario.
```

### `sample_colours_from_cmap`

**Signature:** `sample_colours_from_cmap(cmap_name, n)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
Sample n evenly spaced colours from a Matplotlib colormap.

Args:
    cmap_name: Name of the Matplotlib colormap (e.g. "YlOrRd").
    n: Number of colours to sample.

Returns:
    List of n hex colour strings, e.g. ["#ffffcc", "#fd8d3c", ...].
```

### `update_buildings`

**Signature:** `update_buildings(patches)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
patches: list of {"id": feature_id, "properties": {"tags": {...}}} (or any of the other
allowed OSM properties instead of/alongside tags). Only the fields you include are changed.
Recorded as a changeset attributed to this script - visible (and revertible) from the
scenario's History tab, same as a person's own edits.
```

### `update_energy_patches`

**Signature:** `update_energy_patches(energy_stats, existing_stats, fetched_inputs)`  
**Used in (1):** `change_refurb_state_backend.py`

```text
Writes the computed energy_stats values into the matching existing stat rows.

For every existing stat whose name is also a key in energy_stats, overwrites its
scenarioValue with the (int-cast) computed value. When the scenario's
"set_status_quo" input is on, statusValue is overwritten with the same value too
-- i.e. the freshly computed numbers become the new baseline, not just the current
scenario state. Use this once, right after import, to establish the status-quo
stats a scenario is later compared against; leave the input off for every regular
recompute afterwards, or the baseline would keep drifting to match the scenario.

Args:
    energy_stats: Dict {stat_name: value}, as returned by calc_systems_update.
    existing_stats: List of existing stat dicts for the scenario, as returned by
        fetch_stats.
    fetched_inputs: Dict of this scenario's current input values, as returned by
        fetch_inputs; must contain "set_status_quo".

Returns:
    The existing_stats list, mutated in place.
```

### `update_stats`

**Signature:** `update_stats(patches)`  
**Used in (2):** `change_refurb_state_backend.py`, `data_import_backend.py`

```text
patches: list of {"id": stat_id, ...fields to change} (name/statType/visible/source/
startValue/scenarioValue). Only the fields you include are changed.
```

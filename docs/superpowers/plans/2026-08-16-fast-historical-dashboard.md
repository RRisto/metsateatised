# Fast Historical Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a previously imported ten-year Forest Register dataset reopen, filter, chart, and map quickly without repeating network retrieval, spatial intersections, or carbon calculations.

**Architecture:** Persist source and calculated rows as year/month-partitioned GeoParquet, track completeness and versions in an atomically published manifest, and query the files through one cached DuckDB connection. Precompute chart aggregates and a 5 × 5 km overview grid; serve simplified or full polygons only for the current viewport and zoom level.

**Tech Stack:** Python 3.11+, Streamlit, GeoPandas, Shapely, PyArrow/GeoParquet, DuckDB, Folium, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-08-16-fast-historical-dashboard-design.md`

## Global Constraints

- Preserve the current carbon formulas and keep standing live-biomass carbon separate from planned-harvest biomass.
- Preserve full source geometry on disk; simplification is display-only and uses a 10 metre EPSG:3301 tolerance.
- Use local GeoParquet and DuckDB; do not add a database server, cloud service, or vector-tile server.
- Publish manifests and derived artifacts atomically so an interrupted refresh leaves the last complete dataset readable.
- Never send more than 5,000 detailed polygons in one map response.
- Keep both map color modes: `Süsinikuvaru` and `Raieliik`.
- A stored dashboard must open within 3 seconds, chart-filter queries within 1 second, and viewport map queries within 2 seconds on the project development machine with the 100,000-feature benchmark fixture.
- Use tests first for every behavior change and run Ruff plus the full pytest suite before completion.

## File Structure

- Create `historical_store.py`: manifest types, partition planning, atomic GeoParquet publication, migration, and calculation-version invalidation.
- Create `historical_queries.py`: cached DuckDB connection factory and parameterized chart/detail queries.
- Create `historical_aggregates.py`: chart aggregate and 5 km grid builders.
- Create `historical_map.py`: map detail-level selection, viewport query contract, simplification, and GeoJSON feature collection construction.
- Modify `app.py`: orchestrate incremental refreshes and render query-backed charts/maps without loading all historical geometry.
- Modify `data_cache.py`: clear cache namespaces independently.
- Modify `analysis_cache.py`: expose legacy-result discovery for one-time migration.
- Modify `pyproject.toml` and `uv.lock`: add direct `duckdb` dependency.
- Create `tests/test_historical_store.py`, `tests/test_historical_queries.py`, `tests/test_historical_aggregates.py`, `tests/test_historical_map.py`, and `tests/test_historical_performance.py`.
- Modify `tests/test_dashboard.py`, `tests/test_analysis_cache.py`, and `README.md` for integration behavior and user documentation.

## Shared Test Fixtures

Keep fixture construction in the test module that owns it. Use these exact contracts throughout the tasks:

- `_write_query_fixture(root)` writes three EPSG:4326 notices: two 2025 `Harvendusraie` rows totalling 4.0 ha, 500.0 standing t CO₂e, and 180.0 planned-harvest t CO₂e; one 2024 `Lageraie` row contributes the remaining control total of 225.0 standing t CO₂e.
- `_manifest_with_partitions(*partitions)` returns schema version 1, data version 1, model `estonia-bcef-v1`, schema hash `schema-v1`, and the supplied covered months.
- `_manifest(model_version)` returns the same manifest for January 2025 with the requested model version.
- `_source_frame(partition)` returns one raw EPSG:4326 notice row dated on the first day of the partition.
- `_calculated_frames(partition)` returns that notice's one-row calculated GeoDataFrame and one-row species DataFrame.
- `_write_source_partition(root, partition)` writes `_source_frame(partition)` through the production source-partition API introduced in Task 4.
- `_grid_fixture()` returns three EPSG:3301 notice polygons in cell `500000:6500000`: two `Harvendusraie` and one `Lageraie`, totalling 7.5 ha and 900.0 standing t CO₂e.
- `_complex_polygon_fixture()` returns one EPSG:4326 polygon with at least 100 boundary vertices plus an independently supplied `area_ha` value.
- `_complete_store(root)` returns a frozen test dataclass containing `root`, `start`, `end`, `model_version`, `source_schema_hash`, `manifest`, `partitions`, and `row_count` after publishing one complete partition.

These helpers use literal expected values; they must not call the production aggregate, grid, or cache-key functions to derive assertions.

---

### Task 1: Versioned Manifest and Partition Planning

**Files:**
- Create: `historical_store.py`
- Create: `tests/test_historical_store.py`

**Interfaces:**
- Consumes: `pathlib.Path`, `datetime.date`, and JSON-compatible metadata.
- Produces: `HistoricalManifest`, `PartitionKey`, `load_manifest(root)`, `publish_manifest(root, manifest)`, and `missing_partitions(manifest, start, end)`.

- [ ] **Step 1: Write failing manifest and partition tests**

```python
from datetime import date

from historical_store import (
    HistoricalManifest,
    PartitionKey,
    load_manifest,
    missing_partitions,
    publish_manifest,
)


def test_missing_partitions_returns_only_uncovered_months():
    manifest = HistoricalManifest(
        schema_version=1,
        data_version=4,
        model_version="estonia-bcef-v1",
        source_schema_hash="abc",
        covered_partitions=(PartitionKey(2025, 1), PartitionKey(2025, 2)),
        updated_at="2026-08-16T12:00:00+00:00",
    )

    assert missing_partitions(manifest, date(2025, 1, 1), date(2025, 4, 30)) == (
        PartitionKey(2025, 3),
        PartitionKey(2025, 4),
    )


def test_manifest_round_trip_is_atomic_and_typed(tmp_path):
    manifest = HistoricalManifest(
        schema_version=1,
        data_version=1,
        model_version="estonia-bcef-v1",
        source_schema_hash="abc",
        covered_partitions=(PartitionKey(2025, 1),),
        updated_at="2026-08-16T12:00:00+00:00",
    )

    publish_manifest(tmp_path, manifest)

    assert load_manifest(tmp_path) == manifest
    assert not list(tmp_path.glob("manifest-*.tmp"))
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/Scripts/pytest.exe tests/test_historical_store.py -q`

Expected: collection fails because `historical_store` does not exist.

- [ ] **Step 3: Implement the manifest model and month planner**

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(frozen=True, order=True)
class PartitionKey:
    year: int
    month: int


@dataclass(frozen=True)
class HistoricalManifest:
    schema_version: int
    data_version: int
    model_version: str
    source_schema_hash: str
    covered_partitions: tuple[PartitionKey, ...]
    updated_at: str


def _months(start: date, end: date) -> tuple[PartitionKey, ...]:
    current = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    values = []
    while current <= last:
        values.append(PartitionKey(current.year, current.month))
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return tuple(values)


def missing_partitions(
    manifest: HistoricalManifest | None, start: date, end: date
) -> tuple[PartitionKey, ...]:
    covered = set(manifest.covered_partitions if manifest else ())
    return tuple(partition for partition in _months(start, end) if partition not in covered)


def publish_manifest(root: Path, manifest: HistoricalManifest) -> None:
    root.mkdir(parents=True, exist_ok=True)
    payload = asdict(manifest)
    payload["covered_partitions"] = [asdict(item) for item in manifest.covered_partitions]
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=root, prefix="manifest-", suffix=".tmp", delete=False
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
        temporary_path = Path(temporary.name)
    temporary_path.replace(root / "manifest.json")


def load_manifest(root: Path) -> HistoricalManifest | None:
    path = root / "manifest.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["covered_partitions"] = tuple(
        PartitionKey(**item) for item in payload["covered_partitions"]
    )
    return HistoricalManifest(**payload)
```

- [ ] **Step 4: Run the manifest tests and Ruff**

Run: `.venv/Scripts/pytest.exe tests/test_historical_store.py -q`

Expected: all tests pass.

Run: `.venv/Scripts/ruff.exe check historical_store.py tests/test_historical_store.py`

Expected: `All checks passed!`

- [ ] **Step 5: Commit the manifest foundation**

```bash
git add historical_store.py tests/test_historical_store.py
git commit -m "Add historical dataset manifest"
```

---

### Task 2: Atomic Partition Storage and Legacy Cache Migration

**Files:**
- Modify: `historical_store.py`
- Modify: `analysis_cache.py`
- Modify: `tests/test_historical_store.py`
- Modify: `tests/test_analysis_cache.py`

**Interfaces:**
- Consumes: `PartitionKey`, `HistoricalManifest`, existing GeoDataFrame results, species DataFrame, and `data/cache/analysis` legacy files.
- Produces: `write_partition(root, partition, results, species)`, `read_partition(root, partition)`, `discover_legacy_entries(cache_root)`, and `migrate_legacy_entry(entry, historical_root, expected_model_version)`.

- [ ] **Step 1: Write failing atomic partition tests**

```python
import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from historical_store import PartitionKey, read_partition, write_partition


def test_partition_round_trip_preserves_geometry_and_species(tmp_path):
    results = gpd.GeoDataFrame(
        {"teatis_id": [1], "standing_live_biomass_tco2": [12.5]},
        geometry=[Point(24.5, 58.7)],
        crs="EPSG:4326",
    )
    species = pd.DataFrame({"teatis_id": [1], "species": ["Kask"]})

    write_partition(tmp_path, PartitionKey(2025, 1), results, species)
    restored_results, restored_species = read_partition(tmp_path, PartitionKey(2025, 1))

    assert restored_results.crs == results.crs
    assert restored_results.geometry.iloc[0].equals(results.geometry.iloc[0])
    pd.testing.assert_frame_equal(restored_species, species)
    assert not list(tmp_path.rglob("*.tmp.parquet"))
```

- [ ] **Step 2: Write a failing legacy migration test**

```python
from analysis_cache import discover_legacy_entries, write_analysis_cache


def test_legacy_cache_entry_can_be_discovered_for_migration(tmp_path):
    results, species = _cached_frames()
    write_analysis_cache(
        date(2025, 1, 1), date(2025, 1, 31), 100_000, "estonia-bcef-v1",
        results, species, cache_root=tmp_path,
    )

    entries = discover_legacy_entries(tmp_path)

    assert len(entries) == 1
    assert entries[0].model_version == "estonia-bcef-v1"
    assert entries[0].results_path.name == "results.parquet"
```

- [ ] **Step 3: Run both focused tests and verify RED**

Run: `.venv/Scripts/pytest.exe tests/test_historical_store.py::test_partition_round_trip_preserves_geometry_and_species tests/test_analysis_cache.py::test_legacy_cache_entry_can_be_discovered_for_migration -q`

Expected: failures because the new functions are undefined.

- [ ] **Step 4: Implement atomic partition writes**

Add exact paths and functions to `historical_store.py`:

```python
def partition_directory(root: Path, partition: PartitionKey) -> Path:
    return root / "notices" / f"year={partition.year:04d}" / f"month={partition.month:02d}"


def write_partition(
    root: Path,
    partition: PartitionKey,
    results: gpd.GeoDataFrame,
    species: pd.DataFrame,
) -> None:
    directory = partition_directory(root, partition)
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    result_tmp = directory / f"results-{token}.tmp.parquet"
    species_tmp = directory / f"species-{token}.tmp.parquet"
    results.to_parquet(result_tmp, index=False)
    species.to_parquet(species_tmp, index=False)
    result_tmp.replace(directory / "results.parquet")
    species_tmp.replace(directory / "species.parquet")


def read_partition(
    root: Path, partition: PartitionKey
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    directory = partition_directory(root, partition)
    return (
        gpd.read_parquet(directory / "results.parquet"),
        pd.read_parquet(directory / "species.parquet"),
    )
```

Import `uuid4`, `geopandas as gpd`, and `pandas as pd`.

- [ ] **Step 5: Expose typed legacy entries and migration validation**

Add to `analysis_cache.py`:

```python
@dataclass(frozen=True)
class LegacyAnalysisEntry:
    directory: Path
    results_path: Path
    species_path: Path
    model_version: str


def discover_legacy_entries(cache_root: Path) -> tuple[LegacyAnalysisEntry, ...]:
    entries = []
    for manifest_path in sorted((cache_root / "analysis").glob("*/manifest.json")):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        directory = manifest_path.parent
        results_path = directory / "results.parquet"
        species_path = directory / "species.parquet"
        if results_path.exists() and species_path.exists():
            entries.append(
                LegacyAnalysisEntry(
                    directory, results_path, species_path, str(payload["model_version"])
                )
            )
    return tuple(entries)
```

Add `migrate_legacy_entry` to `historical_store.py`; read GeoParquet, require `geometry`, `teatis_id`, and `standing_live_biomass_tco2`, require matching CRS and model version, derive the partition from the notice date column, then call `write_partition`. Return the tuple of partitions written so the caller can update the manifest only after success.

- [ ] **Step 6: Run storage, legacy-cache, and lint checks**

Run: `.venv/Scripts/pytest.exe tests/test_historical_store.py tests/test_analysis_cache.py -q`

Expected: all tests pass.

Run: `.venv/Scripts/ruff.exe check historical_store.py analysis_cache.py tests/test_historical_store.py tests/test_analysis_cache.py`

Expected: `All checks passed!`

- [ ] **Step 7: Commit storage and migration**

```bash
git add historical_store.py analysis_cache.py tests/test_historical_store.py tests/test_analysis_cache.py
git commit -m "Persist historical partitions atomically"
```

---

### Task 3: DuckDB Query Layer and Chart Aggregates

**Files:**
- Create: `historical_queries.py`
- Create: `historical_aggregates.py`
- Create: `tests/test_historical_queries.py`
- Create: `tests/test_historical_aggregates.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Consumes: partitioned `results.parquet` and `species.parquet` files.
- Produces: `HistoricalFilters`, `open_historical_database(root)`, `query_summary(connection, filters)`, `query_species(connection, filters)`, and `build_aggregate_artifacts(connection, root, model_version)`.

- [ ] **Step 1: Add DuckDB as a direct dependency**

Add `"duckdb>=1.4"` to `[project].dependencies` in `pyproject.toml`, then run:

Run: `uv lock`

Expected: exit 0 and `duckdb` appears in the project dependency block of `uv.lock`.

- [ ] **Step 2: Write a failing filtered summary query test**

```python
from datetime import date

from historical_queries import HistoricalFilters, open_historical_database, query_summary


def test_summary_query_filters_period_and_cutting_type(tmp_path):
    _write_query_fixture(tmp_path)
    connection = open_historical_database(tmp_path)
    filters = HistoricalFilters(
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        cutting_types=("Harvendusraie",),
        species=(),
        bbox=None,
    )

    summary = query_summary(connection, filters)

    assert summary.to_dict("records") == [
        {
            "year": 2025,
            "notice_count": 2,
            "area_ha": 4.0,
            "standing_live_biomass_tco2": 500.0,
            "planned_harvest_biomass_tco2": 180.0,
        }
    ]
```

- [ ] **Step 3: Run the query test and verify RED**

Run: `.venv/Scripts/pytest.exe tests/test_historical_queries.py::test_summary_query_filters_period_and_cutting_type -q`

Expected: collection failure because `historical_queries` does not exist.

- [ ] **Step 4: Implement typed filters and parameterized DuckDB queries**

```python
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd


@dataclass(frozen=True)
class HistoricalFilters:
    start: date
    end: date
    cutting_types: tuple[str, ...] = ()
    species: tuple[str, ...] = ()
    bbox: tuple[float, float, float, float] | None = None


def open_historical_database(root: Path) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    results_glob = (root / "notices" / "year=*" / "month=*" / "results.parquet").as_posix()
    species_glob = (root / "notices" / "year=*" / "month=*" / "species.parquet").as_posix()
    connection.execute(
        "CREATE VIEW notice_results AS SELECT * FROM read_parquet(?, hive_partitioning=true)",
        [results_glob],
    )
    connection.execute(
        "CREATE VIEW species_results AS SELECT * FROM read_parquet(?, hive_partitioning=true)",
        [species_glob],
    )
    return connection
```

Implement one internal `_where(filters, alias)` builder that returns SQL plus parameters. Use only `?` placeholders; never interpolate user-selected values. `query_summary` groups by `year`, uses `COUNT(DISTINCT teatis_id)`, and sums the three numeric result columns with DuckDB `SUM`.

- [ ] **Step 5: Write failing aggregate artifact tests**

```python
from historical_aggregates import build_aggregate_artifacts


def test_aggregate_artifacts_match_detail_control_totals(tmp_path):
    _write_query_fixture(tmp_path)
    connection = open_historical_database(tmp_path)

    paths = build_aggregate_artifacts(connection, tmp_path, "estonia-bcef-v1")

    annual = pd.read_parquet(paths.annual)
    assert annual["notice_count"].sum() == 3
    assert annual["standing_live_biomass_tco2"].sum() == 725.0
    assert paths.monthly.exists()
    assert paths.cutting_type.exists()
    assert paths.species.exists()
```

- [ ] **Step 6: Implement atomic aggregate publication**

Create an `AggregatePaths` frozen dataclass with `annual`, `monthly`, `cutting_type`, and `species` paths. Query each aggregate from DuckDB into a DataFrame, write each to a `*.tmp.parquet` sibling, replace the final file only after all four temporary writes succeed, and return `AggregatePaths`.

- [ ] **Step 7: Run query and aggregate tests plus Ruff**

Run: `.venv/Scripts/pytest.exe tests/test_historical_queries.py tests/test_historical_aggregates.py -q`

Expected: all tests pass.

Run: `.venv/Scripts/ruff.exe check historical_queries.py historical_aggregates.py tests/test_historical_queries.py tests/test_historical_aggregates.py`

Expected: `All checks passed!`

- [ ] **Step 8: Commit DuckDB queries and aggregates**

```bash
git add pyproject.toml uv.lock historical_queries.py historical_aggregates.py tests/test_historical_queries.py tests/test_historical_aggregates.py
git commit -m "Query historical results with DuckDB"
```

---

### Task 4: Incremental Import and Calculation Invalidation

**Files:**
- Modify: `historical_store.py`
- Modify: `app.py`
- Modify: `tests/test_historical_store.py`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `load_notices`, `resolve_stands_for_notices`, `analyze`, manifest APIs, and `ANALYSIS_MODEL_VERSION`.
- Produces: `refresh_historical_period(start, end, max_features, *, force_source, force_calculation, root, load_source_partition, calculate_partition, rebuild_aggregates, model_version, source_schema_hash) -> RefreshResult` and UI states `stored`, `updating`, `complete`, `failed`.

- [ ] **Step 1: Write a failing incremental-refresh orchestration test**

```python
def test_refresh_downloads_only_missing_month_and_preserves_existing_partition(tmp_path):
    existing = _manifest_with_partitions(PartitionKey(2025, 1))
    publish_manifest(tmp_path, existing)
    calls = []

    result = refresh_historical_period(
        date(2025, 1, 1),
        date(2025, 2, 28),
        100_000,
        force_source=False,
        force_calculation=False,
        root=tmp_path,
        load_source_partition=lambda partition, max_features: calls.append(partition)
        or _source_frame(partition),
        calculate_partition=lambda partition, source: _calculated_frames(partition),
        rebuild_aggregates=lambda root, model_version: None,
        model_version="estonia-bcef-v1",
        source_schema_hash="schema-v1",
    )

    assert calls == [PartitionKey(2025, 2)]
    assert result.processed_partitions == (PartitionKey(2025, 2),)
    assert load_manifest(tmp_path).covered_partitions == (
        PartitionKey(2025, 1), PartitionKey(2025, 2)
    )
```

- [ ] **Step 2: Write a failing model-version invalidation test**

```python
def test_model_change_reuses_source_but_rebuilds_calculations(tmp_path):
    _write_source_partition(tmp_path, PartitionKey(2025, 1))
    publish_manifest(tmp_path, _manifest(model_version="estonia-bcef-v1"))
    source_calls = []
    calculation_calls = []

    refresh_historical_period(
        date(2025, 1, 1), date(2025, 1, 31), 100_000,
        force_source=False, force_calculation=False, root=tmp_path,
        load_source_partition=lambda partition, max_features: source_calls.append(partition),
        calculate_partition=lambda partition, source: calculation_calls.append(partition)
        or _calculated_frames(partition),
        rebuild_aggregates=lambda root, model_version: None,
        model_version="estonia-bcef-v2", source_schema_hash="schema-v1",
    )

    assert source_calls == []
    assert calculation_calls == [PartitionKey(2025, 1)]
```

- [ ] **Step 3: Run the focused tests and verify RED**

Run: `.venv/Scripts/pytest.exe tests/test_historical_store.py -q`

Expected: failures because `refresh_historical_period` and `RefreshResult` are undefined.

- [ ] **Step 4: Implement `RefreshResult` and incremental publication**

Define:

```python
@dataclass(frozen=True)
class RefreshResult:
    processed_partitions: tuple[PartitionKey, ...]
    reused_partitions: tuple[PartitionKey, ...]
    data_version: int
    model_version: str
```

`refresh_historical_period` must calculate the target month sequence, select missing months unless `force_source` is true, process one partition at a time, rebuild all target calculation partitions when the manifest model version differs or `force_calculation` is true, build aggregates, and publish the incremented manifest last. If any callback raises, propagate the exception and leave the old manifest untouched.

Before implementing the orchestration, add source-only storage to `historical_store.py`:

```python
def source_partition_directory(root: Path, partition: PartitionKey) -> Path:
    return root / "source" / f"year={partition.year:04d}" / f"month={partition.month:02d}"


def write_source_partition(
    root: Path, partition: PartitionKey, source: gpd.GeoDataFrame
) -> None:
    directory = source_partition_directory(root, partition)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"source-{uuid4().hex}.tmp.parquet"
    source.to_parquet(temporary, index=False)
    temporary.replace(directory / "source.parquet")


def read_source_partition(root: Path, partition: PartitionKey) -> gpd.GeoDataFrame:
    return gpd.read_parquet(source_partition_directory(root, partition) / "source.parquet")
```

The refresh function calls `load_source_partition(partition, max_features)` only for missing or forced source months, writes that raw frame with `write_source_partition`, passes the stored source frame to `calculate_partition`, writes calculated results through `write_partition`, calls `rebuild_aggregates(root, model_version)` after all target calculations succeed, and publishes the manifest last.

- [ ] **Step 5: Adapt the existing analysis pipeline to a month callback**

Extract the body currently under `if run:` in `app.py` into:

```python
def process_historical_partition(
    partition: PartitionKey,
    max_features: int,
    progress: HistoricalProgress,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    start = date(partition.year, partition.month, 1)
    end = date(partition.year + (partition.month == 12), partition.month % 12 + 1, 1)
    end -= timedelta(days=1)
    notices = load_notices(start, end, max_features)
    stands = resolve_stands_for_notices(
        notices,
        exact_loader=load_stands_for_filter,
        bbox_loader=load_stands_for_bbox,
        batch_size=50,
        progress_callback=progress.stands,
    )
    return analyze(notices, stands, detail_progress_callback=progress.details)
```

Define `HistoricalProgress` as a dataclass of three callbacks: `partition`, `stands`, and `details`. Pass the existing Streamlit progress widgets through these callbacks; keep network and analysis logic out of `historical_store.py`.

- [ ] **Step 6: Add explicit refresh controls and status tests**

Update `tests/test_dashboard.py` to assert these exact labels:

```python
assert "Värskenda valitud perioodi lähteandmed" in [item.label for item in app.checkbox]
assert "Arvuta süsinikutulemused uuesti" in [item.label for item in app.checkbox]
```

Replace the existing combined forced-recalculation checkbox. Display the manifest's covered period, `updated_at`, and `model_version` in the sidebar. A cache hit must render `Salvestatud andmed`; an active refresh must render `Uuendatakse`.

- [ ] **Step 7: Run incremental-store and dashboard tests**

Run: `.venv/Scripts/pytest.exe tests/test_historical_store.py tests/test_dashboard.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit incremental refresh integration**

```bash
git add historical_store.py app.py tests/test_historical_store.py tests/test_dashboard.py
git commit -m "Refresh historical data incrementally"
```

---

### Task 5: Five-Kilometre Overview Grid

**Files:**
- Modify: `historical_aggregates.py`
- Modify: `historical_queries.py`
- Create: `tests/test_historical_map.py`
- Modify: `tests/test_historical_aggregates.py`

**Interfaces:**
- Consumes: full EPSG:4326 notice geometry and calculated carbon fields.
- Produces: `assign_grid_cell(geometry)`, `build_grid_5km(results)`, `query_grid(connection, filters)`, and `grid_5km.parquet` in EPSG:3301.

- [ ] **Step 1: Write failing deterministic grid tests**

```python
from shapely.geometry import Point

from historical_aggregates import assign_grid_cell, build_grid_5km


def test_grid_cell_uses_five_kilometre_epsg3301_origin():
    assert assign_grid_cell(Point(500_001, 6_500_001)) == "500000:6500000"
    assert assign_grid_cell(Point(504_999, 6_504_999)) == "500000:6500000"
    assert assign_grid_cell(Point(505_000, 6_505_000)) == "505000:6505000"


def test_grid_aggregates_area_carbon_and_dominant_cutting_type():
    grid = build_grid_5km(_grid_fixture())
    row = grid.loc[grid["grid_id"] == "500000:6500000"].iloc[0]
    assert row["notice_count"] == 3
    assert row["area_ha"] == pytest.approx(7.5)
    assert row["standing_live_biomass_tco2"] == pytest.approx(900.0)
    assert row["dominant_cutting_type"] == "Harvendusraie"
    assert row["dominant_cutting_type_share"] == pytest.approx(2 / 3)
```

- [ ] **Step 2: Run the grid tests and verify RED**

Run: `.venv/Scripts/pytest.exe tests/test_historical_aggregates.py -q`

Expected: failures because grid functions are undefined.

- [ ] **Step 3: Implement grid assignment and aggregation**

Project results to EPSG:3301. Assign by geometry centroid using:

```python
GRID_SIZE_M = 5_000


def assign_grid_cell(point: Point) -> str:
    x = math.floor(point.x / GRID_SIZE_M) * GRID_SIZE_M
    y = math.floor(point.y / GRID_SIZE_M) * GRID_SIZE_M
    return f"{x}:{y}"
```

Construct each cell geometry with `shapely.geometry.box(x, y, x + 5000, y + 5000)`. Aggregate numeric columns with `sum(min_count=1)`, count distinct notice IDs, and choose the dominant cutting type by greatest notice count with alphabetical tie-breaking. Write `grid_5km.parquet` atomically beside the chart aggregates.

- [ ] **Step 4: Add a filtered DuckDB grid query**

`query_grid(connection, filters)` must select cells intersecting the optional bbox and apply period, cutting type, and species filters before aggregation. Return EPSG:4326 GeoDataFrame rows for Folium.

- [ ] **Step 5: Run grid, query, and Ruff checks**

Run: `.venv/Scripts/pytest.exe tests/test_historical_aggregates.py tests/test_historical_queries.py tests/test_historical_map.py -q`

Expected: all tests pass.

Run: `.venv/Scripts/ruff.exe check historical_aggregates.py historical_queries.py tests/test_historical_aggregates.py tests/test_historical_map.py`

Expected: `All checks passed!`

- [ ] **Step 6: Commit the overview grid**

```bash
git add historical_aggregates.py historical_queries.py tests/test_historical_aggregates.py tests/test_historical_queries.py tests/test_historical_map.py
git commit -m "Add five kilometre overview grid"
```

---

### Task 6: Viewport-Based Multi-Resolution Map

**Files:**
- Create: `historical_map.py`
- Modify: `app.py`
- Modify: `tests/test_historical_map.py`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `HistoricalFilters`, current Leaflet bounds and zoom, `query_grid`, and detail-query results.
- Produces: `MapDetailLevel`, `choose_map_detail(zoom: int, feature_count: int)`, `query_map_features(connection, filters, bbox, zoom) -> HistoricalMapFeatures`, and `make_historical_map(features, color_mode) -> HistoricalMapResult`.

- [ ] **Step 1: Write failing detail-level boundary tests**

```python
from historical_map import MapDetailLevel, choose_map_detail


@pytest.mark.parametrize(
    ("zoom", "feature_count", "expected"),
    [
        (7, 100_000, MapDetailLevel.GRID),
        (10, 20_000, MapDetailLevel.SIMPLIFIED),
        (13, 5_000, MapDetailLevel.FULL),
        (13, 5_001, MapDetailLevel.SIMPLIFIED),
    ],
)
def test_map_detail_level_respects_zoom_and_feature_cap(zoom, feature_count, expected):
    assert choose_map_detail(zoom, feature_count) is expected
```

- [ ] **Step 2: Write a failing simplification-semantics test**

```python
def test_simplified_map_geometry_does_not_change_calculated_area():
    source = _complex_polygon_fixture()
    simplified = simplify_for_map(source)

    assert len(simplified.geometry.iloc[0].exterior.coords) < len(
        source.geometry.iloc[0].exterior.coords
    )
    assert simplified.loc[0, "area_ha"] == source.loc[0, "area_ha"]
```

- [ ] **Step 3: Run map tests and verify RED**

Run: `.venv/Scripts/pytest.exe tests/test_historical_map.py -q`

Expected: collection failure because `historical_map` does not exist.

- [ ] **Step 4: Implement detail selection and display simplification**

```python
class MapDetailLevel(StrEnum):
    GRID = "grid"
    SIMPLIFIED = "simplified"
    FULL = "full"


def choose_map_detail(zoom: int, feature_count: int) -> MapDetailLevel:
    if zoom <= 8:
        return MapDetailLevel.GRID
    if zoom >= 12 and feature_count <= 5_000:
        return MapDetailLevel.FULL
    return MapDetailLevel.SIMPLIFIED


def simplify_for_map(results: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    simplified = results.to_crs(3301).copy()
    simplified.geometry = simplified.geometry.simplify(10, preserve_topology=True)
    return simplified.to_crs(4326)
```

- [ ] **Step 5: Implement viewport queries and one-layer GeoJSON output**

`query_map_features` must first count filtered features intersecting the bbox, choose the detail level, then return grid, simplified, or full rows. Select only popup, tooltip, color, ID, and geometry columns. `make_historical_map` must reuse the existing batched FeatureCollection pattern and changing legend, never add one `folium.GeoJson` child per row.

Add an explicit result object:

```python
@dataclass(frozen=True)
class HistoricalMapFeatures:
    features: gpd.GeoDataFrame
    detail_level: MapDetailLevel
    source_feature_count: int
    truncated: bool

    def to_geojson(self) -> str:
        return self.features.to_json(drop_id=True)


@dataclass(frozen=True)
class HistoricalMapResult:
    map: folium.Map
    detail_level: MapDetailLevel
    feature_count: int
    truncated: bool
```

When full detail exceeds 5,000 features, return simplified geometry with `truncated=True`; the UI text must say `Liiga palju üksikobjekte — suumi lähemale.`

- [ ] **Step 6: Wire Leaflet bounds and zoom back into Streamlit**

Give `st_folium` a stable key and request returned objects `bounds` and `zoom`. Persist the latest viewport in `st.session_state`; call `make_historical_map` with that viewport on rerun. Show one of `Koondruudud`, `Lihtsustatud raiealad`, or `Täpsed raiealad` above the map.

- [ ] **Step 7: Run map and dashboard integration tests**

Run: `.venv/Scripts/pytest.exe tests/test_historical_map.py tests/test_dashboard.py -q`

Expected: all tests pass, including both color modes, popup fields, one GeoJSON layer, and the 5,000-feature fallback.

- [ ] **Step 8: Commit the multi-resolution map**

```bash
git add historical_map.py app.py tests/test_historical_map.py tests/test_dashboard.py
git commit -m "Render historical map by viewport detail"
```

---

### Task 7: Query-Backed Dashboard and Cache Invalidation

**Files:**
- Modify: `historical_queries.py`
- Modify: `app.py`
- Modify: `data_cache.py`
- Modify: `tests/test_historical_queries.py`
- Modify: `tests/test_data_cache.py`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: manifest `data_version`, `model_version`, `HistoricalFilters`, aggregate queries, and map queries.
- Produces: fast chart tabs, scoped cache clearing, and query cache keys that invalidate on manifest changes.

- [ ] **Step 1: Write a failing cache-key invalidation test**

```python
from historical_queries import query_cache_key


def test_query_cache_key_changes_with_manifest_and_filters():
    filters = HistoricalFilters(date(2025, 1, 1), date(2025, 12, 31))
    baseline = query_cache_key(filters, data_version=4, model_version="v1")

    assert query_cache_key(filters, data_version=5, model_version="v1") != baseline
    assert query_cache_key(filters, data_version=4, model_version="v2") != baseline
    assert query_cache_key(
        replace(filters, cutting_types=("Lageraie",)), 4, "v1"
    ) != baseline
```

- [ ] **Step 2: Write failing scoped cache-clear tests**

```python
from data_cache import clear_cache_namespace


def test_clearing_query_cache_preserves_historical_source(tmp_path):
    (tmp_path / "historical" / "notices").mkdir(parents=True)
    (tmp_path / "historical" / "notices" / "source.parquet").write_bytes(b"source")
    (tmp_path / "query").mkdir()
    (tmp_path / "query" / "result.json").write_text("{}", encoding="utf-8")

    clear_cache_namespace("query", cache_root=tmp_path)

    assert not (tmp_path / "query").exists()
    assert (tmp_path / "historical" / "notices" / "source.parquet").exists()
```

- [ ] **Step 3: Run focused tests and verify RED**

Run: `.venv/Scripts/pytest.exe tests/test_historical_queries.py::test_query_cache_key_changes_with_manifest_and_filters tests/test_data_cache.py::test_clearing_query_cache_preserves_historical_source -q`

Expected: failures because both functions are undefined.

- [ ] **Step 4: Implement deterministic query keys and scoped clearing**

`query_cache_key` must JSON-serialize `asdict(filters)`, dates as ISO strings, bbox as four floats, `data_version`, and `model_version` with sorted keys, then return SHA-256 hex. `clear_cache_namespace` must accept only `wfs`, `details`, `analysis`, `historical`, or `query`; reject other values with `ValueError` and delete only `cache_root / namespace`.

- [ ] **Step 5: Replace full-DataFrame dashboard reads with queries**

In `app.py`, cache one DuckDB connection:

```python
@st.cache_resource
def historical_database(root: Path, data_version: int):
    return open_historical_database(root)
```

Use `query_summary` for KPI and time charts, `query_species` for the species tab, `query_map_features` for the map, and a paginated detail query for the data tab. Do not retain a complete historical GeoDataFrame in `st.session_state`; retain `HistoricalFilters`, viewport, and manifest versions only.

- [ ] **Step 6: Add dashboard behavior tests**

Use a temporary historical store fixture and assert:

```python
assert app.metric[0].value == "3"
assert "Salvestatud andmed" in _rendered_text(app)
assert not app.exception
```

Change the cutting-type selector through `AppTest`, rerun, and assert the KPI reflects the filtered fixture. Add a spy at the app's source-loader boundary and assert it is not called when the manifest covers the selected period.

- [ ] **Step 7: Run dashboard, query, and cache suites**

Run: `.venv/Scripts/pytest.exe tests/test_dashboard.py tests/test_historical_queries.py tests/test_data_cache.py -q`

Expected: all tests pass.

- [ ] **Step 8: Commit the query-backed dashboard**

```bash
git add app.py historical_queries.py data_cache.py tests/test_dashboard.py tests/test_historical_queries.py tests/test_data_cache.py
git commit -m "Load dashboard views from historical queries"
```

---

### Task 8: Performance Gates, Recovery, and Documentation

**Files:**
- Create: `tests/test_historical_performance.py`
- Modify: `tests/test_historical_store.py`
- Modify: `README.md`
- Modify: `app.py`

**Interfaces:**
- Consumes: completed historical store, query layer, map layer, and 100,000-feature synthetic fixture.
- Produces: repeatable performance evidence, interrupted-write recovery, and operator documentation.

- [ ] **Step 1: Write the 100,000-feature performance harness**

Create a session-scoped fixture that writes 100,000 deterministic 40-vertex polygons across the Estonia bounding box, cycles through eight cutting types and twelve species, and assigns literal numeric carbon and area values. Mark it `@pytest.mark.performance` and measure with `time.perf_counter()`:

```python
@pytest.mark.performance
def test_historical_dashboard_performance(benchmark_store):
    started = time.perf_counter()
    connection = open_historical_database(benchmark_store.root)
    opened = time.perf_counter()
    summary = query_summary(connection, benchmark_store.filters)
    charted = time.perf_counter()
    map_result = query_map_features(
        connection, benchmark_store.filters, benchmark_store.estonia_bbox, zoom=7
    )
    mapped = time.perf_counter()

    assert opened - started < 3.0
    assert charted - opened < 1.0
    assert mapped - charted < 2.0
    assert len(map_result.features) < 5_000
    assert len(map_result.to_geojson().encode("utf-8")) < 5_000_000
```

- [ ] **Step 2: Add an interrupted-publication recovery test**

```python
def test_failed_refresh_keeps_previous_manifest_and_partitions(tmp_path):
    original = _complete_store(tmp_path)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        refresh_historical_period(
            original.start, original.end, 100_000,
            force_source=True, force_calculation=False, root=tmp_path,
            process_partition=lambda partition: (_ for _ in ()).throw(
                RuntimeError("synthetic failure")
            ),
            model_version=original.model_version,
            source_schema_hash=original.source_schema_hash,
        )

    assert load_manifest(tmp_path) == original.manifest
    restored, _ = read_partition(tmp_path, original.partitions[0])
    assert len(restored) == original.row_count
```

- [ ] **Step 3: Run functional recovery tests**

Run: `.venv/Scripts/pytest.exe tests/test_historical_store.py -q`

Expected: all tests pass and the old manifest remains readable after the injected failure.

- [ ] **Step 4: Run the performance test and record actual timings**

Run: `.venv/Scripts/pytest.exe tests/test_historical_performance.py -m performance -s`

Expected: all four thresholds pass. If a threshold fails, profile the measured stage before changing any limit; retain the exact acceptance thresholds from the spec.

- [ ] **Step 5: Document operation and recovery**

Add README sections with exact UI actions and storage behavior:

```markdown
## Ajalooliste andmete vahemälu

Esimene päring laadib ja arvutab ainult manifestist puuduvad kuud. Järgmine sama
perioodi avamine kasutab `data/cache/historical` GeoParquet-faile ja DuckDB
koondpäringuid.

- **Värskenda valitud perioodi lähteandmed** laadib valitud kuud uuesti.
- **Arvuta süsinikutulemused uuesti** kasutab salvestatud lähteandmeid, kuid
  taastab arvutused ja koondid.
- Eesti üldvaade kuvab 5 × 5 km koondruute. Täpsed raiealad ilmuvad lähivaates,
  kui nähtavas alas on kuni 5 000 objekti.

Katkestatud värskenduse korral jääb eelmine terviklik manifest kasutatavaks.
Ära kustuta `data/cache/analysis` faile enne, kui migratsioon on edukalt lõppenud.
```

- [ ] **Step 6: Run final verification**

Run: `.venv/Scripts/ruff.exe check .`

Expected: `All checks passed!`

Run: `.venv/Scripts/pytest.exe -q`

Expected: all non-live tests pass; the existing live Forest Register test may remain skipped.

Run: `.venv/Scripts/pytest.exe tests/test_historical_performance.py -m performance -s`

Expected: performance thresholds pass with printed stage timings.

Run: `git diff --check`

Expected: exit 0 with no whitespace errors.

- [ ] **Step 7: Commit performance gates and documentation**

```bash
git add tests/test_historical_performance.py tests/test_historical_store.py README.md app.py
git commit -m "Verify historical dashboard performance"
```

---

## Final Acceptance Checklist

- [ ] A second opening of a covered ten-year period performs no Forest Register network requests and no carbon recalculation.
- [ ] Adding one new month rewrites only that month's source/calculation partitions and affected aggregates.
- [ ] A model-version change reuses source partitions and rebuilds calculated artifacts.
- [ ] Charts use DuckDB aggregate queries and meet the one-second filter target.
- [ ] The Estonia overview uses 5 × 5 km aggregate cells.
- [ ] Medium zoom uses 10 metre display-only simplification.
- [ ] Full polygons are limited to 5,000 per viewport response.
- [ ] Both carbon-intensity and cutting-type colors work at all map levels.
- [ ] Interrupted refresh retains the last complete manifest and readable dataset.
- [ ] Ruff, functional tests, and 100,000-feature performance tests pass.

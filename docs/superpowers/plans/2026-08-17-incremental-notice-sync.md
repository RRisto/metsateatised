# Incremental Forest Notice Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a resumable Streamlit workflow that downloads complete monthly Forest Register notice partitions once, then merges only newer or explicitly refreshed periods into the durable local history.

**Architecture:** Extend the WFS client so an unbounded request paginates to exhaustion and reports page progress. Add a focused GeoParquet notice store with an atomic JSON manifest, then coordinate monthly downloads through a synchronization service that the existing Streamlit app calls without invoking stand or biomass processing.

**Tech Stack:** Python 3.11+, GeoPandas, pandas, PyArrow/GeoParquet, Requests, Streamlit, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-08-17-incremental-notice-sync-design.md`

## Global Constraints

- Store durable raw notices below `data/notices`; do not place them in or delete them with `data/cache`.
- Persist one GeoParquet partition per source layer and calendar month in EPSG:4326.
- Download raw notice attributes and geometry only; do not load stands, request details, or calculate biomass.
- Publish a partition and its manifest entry only after the complete partition is written and read back successfully.
- Skip completed partitions by default; explicitly refreshed partitions use non-destructive upsert semantics.
- Never contact the live Forest Register from automated tests.
- Preserve the existing analysis-loading and calculation workflow.

---

## File Structure

- Modify `wfs.py`: paginate bounded and unbounded requests consistently and emit page progress.
- Create `notice_store.py`: month splitting, partition identity/path handling, deduplication, upserts, atomic GeoParquet/manifest publication, and stored summaries.
- Create `notice_sync.py`: coordinate layer/month WFS requests with the durable store and report synchronization progress/results.
- Modify `app.py`: add a separate raw-notice synchronization panel and adapt WFS page progress into Streamlit progress.
- Modify `data_cache.py`: make explicit that clearing short-lived caches does not touch `data/notices` through integration tests, without changing its cache root.
- Create `tests/test_notice_store.py`: store behavior and failure-safety tests.
- Create `tests/test_notice_sync.py`: orchestration tests with fake local loaders.
- Modify `tests/test_wfs.py`: unbounded pagination and progress tests.
- Modify `tests/test_dashboard.py`: Streamlit synchronization-control tests.
- Modify `README.md`: document initial and incremental raw-notice synchronization and storage layout.

### Task 1: Complete WFS Pagination and Page Progress

**Files:**
- Modify: `wfs.py:18-82`
- Modify: `tests/test_wfs.py`

**Interfaces:**
- Consumes: existing `fetch_wfs_features(...) -> list[dict]` callers.
- Produces: `fetch_wfs_features(..., page_progress: Callable[[int, int, int], None] | None = None) -> list[dict]`, where callback arguments are one-based page number, rows in the page, and cumulative rows.

- [ ] **Step 1: Write failing tests for unbounded pagination and progress**

Add to `tests/test_wfs.py`:

```python
def test_fetch_wfs_features_paginates_until_short_page_when_unbounded():
    request_get = Mock(
        side_effect=[
            response_with_features(1, 2),
            response_with_features(3, 4),
            response_with_features(5),
        ]
    )

    features = fetch_wfs_features(
        "metsaregister:teatis_arhiiv",
        max_features=None,
        page_size=2,
        request_get=request_get,
    )

    assert [feature["id"] for feature in features] == [1, 2, 3, 4, 5]
    assert [call.kwargs["params"]["startIndex"] for call in request_get.call_args_list] == [
        0,
        2,
        4,
    ]


def test_fetch_wfs_features_reports_completed_pages():
    progress = Mock()
    request_get = Mock(side_effect=[response_with_features(1, 2), response_with_features(3)])

    fetch_wfs_features(
        "metsaregister:teatis_arhiiv",
        max_features=None,
        page_size=2,
        request_get=request_get,
        page_progress=progress,
    )

    assert progress.call_args_list == [call(1, 2, 2), call(2, 1, 3)]
```

Also import `call` from `unittest.mock`.

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_wfs.py::test_fetch_wfs_features_paginates_until_short_page_when_unbounded tests/test_wfs.py::test_fetch_wfs_features_reports_completed_pages -q
```

Expected: FAIL because an unbounded request stops after its first page and `page_progress` is not accepted.

- [ ] **Step 3: Implement consistent pagination and progress**

In `wfs.py`, import `Callable` is already present. Add the parameter:

```python
page_progress: Callable[[int, int, int], None] | None = None,
```

Replace conditional count/start-index construction with:

```python
remaining = None if max_features is None else max_features - len(features)
requested_count = page_size if remaining is None else min(page_size, remaining)
params["count"] = requested_count
params["startIndex"] = start_index
```

After `features.extend(page)`, report and terminate deterministically:

```python
if page_progress:
    page_progress(start_index // page_size + 1, len(page), len(features))
if not page or len(page) < requested_count:
    break
start_index += len(page)
```

Retain the loop guard `while max_features is None or len(features) < max_features` so bounded requests cannot exceed their requested maximum.

- [ ] **Step 4: Run WFS tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_wfs.py -q
```

Expected: all WFS tests PASS.

- [ ] **Step 5: Commit the WFS change**

```powershell
git add wfs.py tests/test_wfs.py
git commit -m "Support complete paginated WFS downloads"
```

### Task 2: Monthly GeoParquet Notice Store

**Files:**
- Create: `notice_store.py`
- Create: `tests/test_notice_store.py`

**Interfaces:**
- Consumes: GeoDataFrames in EPSG:4326 with `_source_layer` and `_date_col` metadata.
- Produces:
  - `PartitionKey(layer: str, year: int, month: int)`
  - `MonthInterval(start: date, end_exclusive: date)`
  - `StoreSummary(total_records: int, completed_partitions: int, first_date: date | None, last_date: date | None, last_sync_at: datetime | None)`
  - `split_month_intervals(start: date, end: date) -> list[MonthInterval]`
  - `read_manifest(root: Path) -> dict`
  - `partition_path(root: Path, key: PartitionKey) -> Path`
  - `is_partition_complete(root: Path, key: PartitionKey) -> bool`
  - `upsert_partition(root: Path, key: PartitionKey, incoming: gpd.GeoDataFrame, *, identity_candidates: Sequence[str], now: datetime | None = None) -> int`
  - `summarize_store(root: Path) -> StoreSummary`

- [ ] **Step 1: Write failing tests for month boundaries and paths**

Create `tests/test_notice_store.py` with:

```python
from datetime import UTC, date, datetime
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point

from notice_store import (
    PartitionKey,
    partition_path,
    split_month_intervals,
)


def notice_frame(rows):
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def test_split_month_intervals_covers_partial_months_without_gaps():
    intervals = split_month_intervals(date(2025, 1, 20), date(2025, 3, 5))

    assert [(item.start, item.end_exclusive) for item in intervals] == [
        (date(2025, 1, 20), date(2025, 2, 1)),
        (date(2025, 2, 1), date(2025, 3, 1)),
        (date(2025, 3, 1), date(2025, 3, 6)),
    ]


def test_partition_path_separates_layer_year_and_month(tmp_path: Path):
    key = PartitionKey("archive_notices", 2025, 3)

    assert partition_path(tmp_path, key) == (
        tmp_path / "archive_notices" / "year=2025" / "month=03" / "notices.parquet"
    )
```

- [ ] **Step 2: Run boundary tests and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_notice_store.py -q
```

Expected: collection FAIL because `notice_store` does not exist.

- [ ] **Step 3: Implement data types, month splitting, paths, and empty manifest reads**

Create `notice_store.py` with frozen dataclasses. Treat the user end date as inclusive by converting it to `end + timedelta(days=1)`. Reject `start > end` with `ValueError("start must not be after end")`. Use zero-padded month directories. `read_manifest` returns this exact empty document when no manifest exists:

```python
{
    "format_version": 1,
    "last_sync_at": None,
    "partitions": {},
}
```

Use a stable manifest key formatted as `f"{layer}/{year:04d}-{month:02d}"`.

- [ ] **Step 4: Run boundary tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_notice_store.py -q
```

Expected: both tests PASS.

- [ ] **Step 5: Write failing tests for deduplication and overlap upserts**

Append:

```python
from notice_store import is_partition_complete, read_manifest, upsert_partition


def test_upsert_partition_deduplicates_and_prefers_new_rows(tmp_path: Path):
    key = PartitionKey("archive_notices", 2025, 3)
    first = notice_frame(
        [
            {"teatis_id": 1, "otsus": "old", "geometry": Point(24.0, 59.0)},
            {"teatis_id": 2, "otsus": "keep", "geometry": Point(25.0, 58.0)},
        ]
    )
    refreshed = notice_frame(
        [
            {"teatis_id": 1, "otsus": "new", "geometry": Point(24.0, 59.0)},
            {"teatis_id": 3, "otsus": "added", "geometry": Point(26.0, 57.0)},
        ]
    )

    upsert_partition(tmp_path, key, first, identity_candidates=["teatis_id"])
    count = upsert_partition(tmp_path, key, refreshed, identity_candidates=["teatis_id"])
    stored = gpd.read_parquet(partition_path(tmp_path, key)).sort_values("teatis_id")

    assert count == 3
    assert stored[["teatis_id", "otsus"]].to_dict("records") == [
        {"teatis_id": 1, "otsus": "new"},
        {"teatis_id": 2, "otsus": "keep"},
        {"teatis_id": 3, "otsus": "added"},
    ]
    assert is_partition_complete(tmp_path, key)
```

- [ ] **Step 6: Run the upsert test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_notice_store.py::test_upsert_partition_deduplicates_and_prefers_new_rows -q
```

Expected: FAIL because `upsert_partition` and completion tracking are absent.

- [ ] **Step 7: Implement atomic partition upsert and manifest publication**

Implement identity selection by the first candidate present in the incoming or stored columns. Normalize both frames to EPSG:4326, concatenate stored rows before incoming rows, and call `drop_duplicates(subset=[identity_column], keep="last")`. When no candidate exists, calculate a temporary geometry WKB key and use it as the fallback.

Write the merged frame to `notices-<uuid>.parquet` in the destination directory, read it back with `gpd.read_parquet`, verify its row count and CRS, then use `Path.replace` to publish `notices.parquet`. Write the manifest through `NamedTemporaryFile` in the store root and replace `manifest.json` only after the partition succeeds.

Each manifest partition entry must contain:

```python
{
    "status": "complete",
    "record_count": len(merged),
    "identity_field": identity_column_or_geometry_wkb,
    "observed_start": observed_start_or_none,
    "observed_end": observed_end_or_none,
    "schema_fingerprint": sha256_of_sorted_column_dtype_pairs,
    "updated_at": current_utc_iso_timestamp,
}
```

Derive observed dates from each row's `_date_col` value where that named column exists. `is_partition_complete` requires both a complete manifest entry and an existing partition file.

- [ ] **Step 8: Add and pass failure-safety and summary tests**

Add tests that monkeypatch `GeoDataFrame.to_parquet` to raise and assert an existing partition and its manifest remain byte-for-byte unchanged. Add this summary test:

```python
from notice_store import summarize_store


def test_summarize_store_uses_completed_manifest_entries(tmp_path: Path):
    key = PartitionKey("archive_notices", 2025, 3)
    frame = notice_frame(
        [
            {
                "teatis_id": 1,
                "_date_col": "otsuse_kp",
                "otsuse_kp": "2025-03-04T12:00:00Z",
                "geometry": Point(24.0, 59.0),
            }
        ]
    )
    now = datetime(2025, 3, 6, 10, 0, tzinfo=UTC)

    upsert_partition(tmp_path, key, frame, identity_candidates=["teatis_id"], now=now)
    summary = summarize_store(tmp_path)

    assert summary.total_records == 1
    assert summary.completed_partitions == 1
    assert summary.first_date == date(2025, 3, 4)
    assert summary.last_date == date(2025, 3, 4)
    assert summary.last_sync_at == now
```

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_notice_store.py -q
```

Expected: all notice-store tests PASS.

- [ ] **Step 9: Commit the notice store**

```powershell
git add notice_store.py tests/test_notice_store.py
git commit -m "Add durable monthly notice store"
```

### Task 3: Incremental Synchronization Service

**Files:**
- Create: `notice_sync.py`
- Create: `tests/test_notice_sync.py`

**Interfaces:**
- Consumes: Task 1 `fetch_wfs_features(..., page_progress=...)` and Task 2 notice-store interfaces.
- Produces:
  - `SyncProgress(layer: str, month: date, page: int, page_rows: int, cumulative_rows: int)`
  - `SyncResult(downloaded_partitions: int, skipped_partitions: int, failed_partitions: tuple[str, ...], stored_records: int)`
  - `synchronize_notices(start: date, end: date, date_columns: Mapping[str, str], *, root: Path = Path("data/notices"), refresh_completed: bool = False, fetcher: Callable = fetch_wfs_features, progress: Callable[[SyncProgress], None] | None = None) -> SyncResult`

- [ ] **Step 1: Write the failing skip-and-download orchestration test**

Create `tests/test_notice_sync.py`:

```python
from datetime import date
from pathlib import Path

from notice_sync import synchronize_notices
from notice_store import PartitionKey, is_partition_complete


def feature(feature_id, decision_date):
    return {
        "type": "Feature",
        "id": f"teatis.{feature_id}",
        "properties": {"teatis_id": feature_id, "otsuse_kp": decision_date},
        "geometry": {"type": "Point", "coordinates": [24.0, 59.0]},
    }


def test_synchronize_downloads_missing_months_and_skips_completed_ones(tmp_path: Path):
    calls = []

    def fetcher(type_name, **kwargs):
        calls.append((type_name, kwargs["cql_filter"]))
        return [feature(len(calls), "2025-01-15T10:00:00Z")]

    date_columns = {
        "archive_notices": "otsuse_kp",
        "current_notices": "otsuse_kp",
    }
    first = synchronize_notices(
        date(2025, 1, 1),
        date(2025, 1, 31),
        date_columns,
        root=tmp_path,
        fetcher=fetcher,
    )
    second = synchronize_notices(
        date(2025, 1, 1),
        date(2025, 1, 31),
        date_columns,
        root=tmp_path,
        fetcher=fetcher,
    )

    assert first.downloaded_partitions == 2
    assert second.skipped_partitions == 2
    assert len(calls) == 2
    assert is_partition_complete(tmp_path, PartitionKey("archive_notices", 2025, 1))
```

- [ ] **Step 2: Run the synchronization test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_notice_sync.py -q
```

Expected: collection FAIL because `notice_sync` does not exist.

- [ ] **Step 3: Implement monthly layer coordination**

Create frozen `SyncProgress` and `SyncResult` dataclasses. Define the layer mapping locally:

```python
NOTICE_LAYERS = {
    "archive_notices": "metsaregister:teatis_arhiiv",
    "current_notices": "metsaregister:teatis",
}
```

For every `split_month_intervals(start, end)` result and layer:

- require a date column in `date_columns`, otherwise append `"<layer>/<YYYY-MM>: missing date column"` to failures;
- skip complete partitions unless `refresh_completed` is true;
- build `f"{date_column} >= '{start.isoformat()}' AND {date_column} < '{end_exclusive.isoformat()}'"`;
- call the fetcher with `max_features=None`, the CQL filter, `cache_root=DEFAULT_CACHE_ROOT`, and a page callback adapted to `SyncProgress`;
- convert returned GeoJSON features with `gpd.GeoDataFrame.from_features(..., crs="EPSG:4326")`, including an empty EPSG:4326 GeoDataFrame for no features;
- add `_source_layer` and `_date_col` columns;
- call `upsert_partition` with identity candidates `("teatis_id", "teatise_id", "id", "dokumendi_id", "teatise_nr")`.

Catch exceptions per partition, record `"<layer>/<YYYY-MM>: <exception>"`, and continue so completed months are preserved. Compute `stored_records` from `summarize_store(root)` after all selected partitions.

- [ ] **Step 4: Add refresh-upsert and progress tests**

Add one test that synchronizes a month twice with `refresh_completed=True`, changes an existing notice property on the second response, and asserts the partition contains the changed row plus previously stored rows. Add one test whose fake fetcher calls `page_progress(1, 2, 2)` and assert the received `SyncProgress` contains the layer key and month start.

- [ ] **Step 5: Run synchronization tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_notice_sync.py tests/test_notice_store.py tests/test_wfs.py -q
```

Expected: all synchronization, store, and WFS tests PASS.

- [ ] **Step 6: Commit the synchronization service**

```powershell
git add notice_sync.py tests/test_notice_sync.py
git commit -m "Add incremental notice synchronization service"
```

### Task 4: Streamlit Synchronization Controls

**Files:**
- Modify: `app.py:14-40, 780-830`
- Modify: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `summarize_store(Path("data/notices"))` and `synchronize_notices(...)` from Tasks 2-3.
- Produces: a separate sidebar section labeled `Metsateatiste andmete sünkroonimine`, with inputs `Sünkroonimise algus`, `Sünkroonimise lõpp`, checkbox `Uuenda ka juba laaditud kattuvaid kuid`, and button `Laadi/uuenda metsateatised`.

- [ ] **Step 1: Write the failing dashboard control test**

Add to `tests/test_dashboard.py`:

```python
def test_dashboard_exposes_raw_notice_synchronization_controls():
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")

    app.run(timeout=20)

    assert not app.exception
    assert any(
        item.label == "Sünkroonimise algus" for item in app.date_input
    )
    assert any(
        item.label == "Sünkroonimise lõpp" for item in app.date_input
    )
    assert any(
        item.label == "Uuenda ka juba laaditud kattuvaid kuid" for item in app.checkbox
    )
    assert any(
        item.label == "Laadi/uuenda metsateatised" for item in app.button
    )
```

- [ ] **Step 2: Run the control test and verify RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard.py::test_dashboard_exposes_raw_notice_synchronization_controls -q
```

Expected: FAIL because the synchronization controls are absent.

- [ ] **Step 3: Add stored-summary rendering and controls**

Import `summarize_store` and `synchronize_notices`. Define `NOTICE_STORE_ROOT = Path("data/notices")`. In the sidebar, add an expander titled `Metsateatiste andmete sünkroonimine`. Render total records, completed partitions, coverage, and last sync from `summarize_store`.

Default synchronization end to today. Default start to the day after stored `last_date` when data exists, otherwise ten years before today. Keep `refresh_completed` off by default.

- [ ] **Step 4: Add button behavior with progress and summary**

When the synchronization button is clicked:

1. Validate start is not after end and show `st.error` without calling the service when invalid.
2. Detect a date field for both notice layers through existing `sample_layer` and `detect_date_column`.
3. Create one progress bar and one empty status container.
4. Pass a callback that updates the status with layer, `YYYY-MM`, page, and cumulative count. Because the final page count is unknown, use a pulsing page-based display rather than claiming a completion percentage during a partition.
5. Call `synchronize_notices` with the selected interval and overlap flag.
6. Show `st.success` with downloaded/skipped partition counts and stored record count.
7. Show each failed partition through `st.error` without deleting successful partitions.

Do not call `clear_data_cache`, `load_notices`, `load_stands_for_notices`, or `analyze` from this button path.

- [ ] **Step 5: Add a behavior test proving sync does not trigger analysis**

Use `unittest.mock.patch("notice_sync.synchronize_notices")` before `AppTest.from_file`, return `SyncResult(0, 0, (), 0)`, set the sync button, and run the app. Assert the synchronization service was called once. Patch `app.analyze` only if Streamlit's module execution makes it reachable; assert it was not called. The observable contract is that clicking raw synchronization completes without populating `st.session_state["results"]`.

- [ ] **Step 6: Run dashboard tests and verify GREEN**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_dashboard.py -q
```

Expected: all dashboard tests PASS.

- [ ] **Step 7: Commit the Streamlit workflow**

```powershell
git add app.py tests/test_dashboard.py
git commit -m "Add raw notice synchronization controls"
```

### Task 5: Documentation and End-to-End Verification

**Files:**
- Modify: `README.md`
- Test: `tests/test_data_cache.py`

**Interfaces:**
- Consumes: completed synchronization workflow from Tasks 1-4.
- Produces: user instructions for initial historical download, later incremental periods, overlap refreshes, storage location, and deferred visualization/calculation.

- [ ] **Step 1: Add a cache-boundary regression test**

Add to `tests/test_data_cache.py`:

```python
def test_clearing_short_lived_cache_does_not_remove_durable_notice_store(tmp_path):
    cache_root = tmp_path / "cache"
    notice_root = tmp_path / "notices"
    cache_root.mkdir()
    notice_root.mkdir()
    durable_file = notice_root / "manifest.json"
    durable_file.write_text("{}", encoding="utf-8")

    clear_data_cache(cache_root=cache_root)

    assert durable_file.read_text(encoding="utf-8") == "{}"
```

- [ ] **Step 2: Run the boundary test**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_data_cache.py::test_clearing_short_lived_cache_does_not_remove_durable_notice_store -q
```

Expected: PASS, documenting the existing separation between `data/cache` and `data/notices`.

- [ ] **Step 3: Document the synchronization workflow**

Add a `## Metsateatiste ajalooline sünkroonimine` section to `README.md` explaining:

- choose a long date range for the first download;
- later select only dates after the stored coverage;
- enable overlap refresh only when intentionally updating already downloaded months;
- data is stored below `data/notices/<layer>/year=YYYY/month=MM/notices.parquet`;
- the operation downloads raw notices only;
- stand matching, biomass calculation, and combined large-scale visualization remain separate work;
- deleting `data/cache` does not delete the durable notice store.

- [ ] **Step 4: Run formatting, full tests, and notebook regression checks**

Run:

```powershell
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m pytest -q
```

Expected: Ruff reports `All checks passed!`; pytest reports all tests passing with only explicitly marked skips.

Open the existing notebook JSON and execute every code cell through the same project environment against the latest analysis cache to ensure the new raw store does not affect analysis-cache discovery.

- [ ] **Step 5: Perform a controlled two-period manual synchronization**

Use a temporary store root and a fake fetcher in a short Python session:

1. synchronize `2025-01-01` through `2025-01-31`;
2. synchronize `2025-02-01` through `2025-02-28`;
3. assert four completed partitions exist (two layers times two months);
4. assert the second call made no requests for January;
5. read every partition with GeoPandas and verify EPSG:4326.

Expected: adjacent periods coexist, earlier partitions remain unchanged, and all files are readable.

- [ ] **Step 6: Commit documentation and final verification test**

```powershell
git add README.md tests/test_data_cache.py
git commit -m "Document incremental notice synchronization"
```

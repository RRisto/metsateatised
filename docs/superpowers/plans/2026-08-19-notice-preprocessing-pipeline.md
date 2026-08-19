# Ten-Year Notice Preprocessing Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert every existing raw notice month into resumable, notebook-ready notice- and species-level biomass/carbon artifacts without redownloading or modifying raw notices.

**Architecture:** Extract the existing calculation engine from Streamlit into a reusable module, add a processed monthly store with an atomic manifest, and orchestrate each raw calendar month through stand resolution, detail retrieval, calculation, and publication. A CLI runs all or selected months, while lightweight loaders and a starter notebook expose only verified complete outputs.

**Tech Stack:** Python 3.11+, GeoPandas, pandas, PyArrow/GeoParquet, aiohttp, pytest, Ruff, Jupyter notebook JSON

**Spec:** `docs/superpowers/specs/2026-08-19-notice-preprocessing-pipeline-design.md`

## Global Constraints

- Treat `data/notices` as immutable input: never write, rename, or delete anything below it.
- Discover input exclusively through complete entries in `data/notices/manifest.json` with matching canonical Parquet files.
- Combine both raw notice layers into one output per calendar month.
- Query the network only for forest stands and stand details; never redownload notices.
- Publish monthly outputs and the processed manifest atomically after read-back validation.
- Skip a completed month only when files, input fingerprint, and `ANALYSIS_MODEL_VERSION` still match.
- Keep the full job resumable at calendar-month boundaries.
- Keep external request concurrency bounded and process months sequentially by default.
- Do not describe biomass estimates as realized emissions or climate impacts.

---

## File Structure

- Create `notice_analysis.py`: reusable stand-detail retrieval and notice biomass/carbon calculation, with no Streamlit imports.
- Modify `app.py`: import shared analysis functions and retain UI/cache wrappers only.
- Create `processed_notice_store.py`: raw-month discovery, fingerprints, processed manifest, atomic publication, and notebook loaders.
- Create `notice_preprocessing.py`: monthly orchestration, dependency injection, progress, resume decisions, and run results.
- Create `preprocess_notices.py`: command-line interface.
- Create `notebooks/notice_exploration.ipynb`: starter exploration notebook using processed loaders.
- Modify `README.md`: operator commands, storage layout, resume behavior, and notebook use.
- Create `tests/test_notice_analysis.py`, `tests/test_processed_notice_store.py`, `tests/test_notice_preprocessing.py`, and `tests/test_preprocess_notices_cli.py`.
- Modify `tests/test_quality_metrics.py` and `tests/test_detail_progress.py` to target the extracted shared module.

### Task 1: Extract the Reusable Analysis Engine

**Files:**
- Create: `notice_analysis.py`
- Modify: `app.py`
- Create: `tests/test_notice_analysis.py`
- Modify: `tests/test_quality_metrics.py`
- Modify: `tests/test_detail_progress.py`

**Interfaces:**
- Consumes: existing `carbon.py` and `stand_model.py` calculation functions.
- Produces: `fetch_stand_details(stand_ids, *, detail_progress_callback=None, n_workers=12) -> dict`, `analyze_notices(notices, stands, *, detail_loader=fetch_stand_details, detail_progress_callback=None) -> tuple[gpd.GeoDataFrame, pd.DataFrame]`.

- [ ] **Step 1: Write a failing import/behavior test for a Streamlit-free analysis module**

```python
# tests/test_notice_analysis.py
import sys

import geopandas as gpd
from shapely.geometry import Polygon


def test_analysis_module_imports_without_streamlit():
    sys.modules.pop("notice_analysis", None)
    import notice_analysis  # noqa: PLC0415

    assert "streamlit" not in notice_analysis.__dict__


def test_analyze_notices_accepts_an_injected_detail_loader():
    from notice_analysis import analyze_notices

    geometry = Polygon([(24, 59), (24.01, 59), (24.01, 59.01), (24, 59.01)])
    notices = gpd.GeoDataFrame([{"teatis_id": 1, "geometry": geometry}], crs="EPSG:4326")
    stands = gpd.GeoDataFrame([{"eraldis_id": 7, "geometry": geometry}], crs="EPSG:4326")
    calls = []

    analyze_notices(notices, stands, detail_loader=lambda ids, **kwargs: calls.append(tuple(ids)) or {})

    assert calls == [(7,)]
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_notice_analysis.py -q`

Expected: FAIL because `notice_analysis` does not exist.

- [ ] **Step 3: Move reusable code out of `app.py` without changing calculations**

Move `_fetch_detail_one`, `_fetch_detail_batch`, `fetch_stand_details`, `analyze`, and their non-UI helper dependencies into `notice_analysis.py`. Rename `analyze` to `analyze_notices`; add `detail_loader` injection and replace the internal direct call with:

```python
details_by_id = detail_loader(
    stand_ids,
    detail_progress_callback=detail_progress_callback,
)
```

Keep endpoint constants and calculation semantics unchanged. In `app.py`, import:

```python
from notice_analysis import analyze_notices, fetch_stand_details
```

and call `analyze_notices(...)`. Do not import Streamlit from `notice_analysis.py`.

- [ ] **Step 4: Retarget characterization tests to the shared module**

Change monkeypatches such as `monkeypatch.setattr(app, "fetch_stand_details", fake)` to inject `detail_loader=fake` into `analyze_notices`. Change detail-progress tests to patch `notice_analysis._fetch_detail_one` and call `notice_analysis._fetch_detail_batch`.

- [ ] **Step 5: Run analysis and existing application tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_notice_analysis.py tests/test_quality_metrics.py tests/test_detail_progress.py tests/test_dashboard.py -q`

Expected: PASS with existing numerical expectations unchanged.

- [ ] **Step 6: Commit the extraction**

```powershell
git add notice_analysis.py app.py tests/test_notice_analysis.py tests/test_quality_metrics.py tests/test_detail_progress.py
git commit -m "Extract reusable notice analysis engine"
```

### Task 2: Discover and Read Raw Calendar Months

**Files:**
- Create: `processed_notice_store.py`
- Create: `tests/test_processed_notice_store.py`

**Interfaces:**
- Consumes: `notice_store.PartitionKey`, canonical raw manifest entries and Parquet paths.
- Produces: `MonthKey(year: int, month: int)`, `RawMonth(key, partition_keys, input_fingerprint)`, `discover_raw_months(raw_root: Path) -> tuple[RawMonth, ...]`, `read_raw_month(raw_root: Path, raw_month: RawMonth) -> gpd.GeoDataFrame`.

- [ ] **Step 1: Write failing discovery and combination tests**

```python
def test_discover_raw_months_groups_layers_and_ignores_incomplete_entries(raw_store):
    months = discover_raw_months(raw_store)

    assert [month.key for month in months] == [MonthKey(2025, 1)]
    assert [key.layer for key in months[0].partition_keys] == [
        "archive_notices",
        "current_notices",
    ]


def test_read_raw_month_combines_layers_and_deduplicates_identity(raw_store):
    month = discover_raw_months(raw_store)[0]
    frame = read_raw_month(raw_store, month)

    assert frame["teatis_id"].tolist() == [1, 2, 3]
    assert frame.crs.equals("EPSG:4326")
```

Build `raw_store` with two tiny canonical `notices.parquet` files and a literal manifest. Include identity `2` in both layers and assert the later manifest-key order keeps one row.

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_processed_notice_store.py -q`

Expected: FAIL because the module and types do not exist.

- [ ] **Step 3: Implement immutable raw discovery**

```python
@dataclass(frozen=True, order=True)
class MonthKey:
    year: int
    month: int


@dataclass(frozen=True)
class RawMonth:
    key: MonthKey
    partition_keys: tuple[PartitionKey, ...]
    input_fingerprint: str
```

Read the raw manifest once, accept only `status == "complete"` entries whose canonical files exist, group them by month, sort layer keys, and fingerprint the stable JSON serialization of the selected raw manifest entries with SHA-256. Reject malformed keys and months outside 1–12.

- [ ] **Step 4: Implement direct monthly reading and deduplication**

Read each file with `gpd.GeoDataFrame.from_arrow(pq.ParquetFile(path).read())`, normalize to EPSG:4326, concatenate, and deduplicate by the first available candidate from:

```python
IDENTITY_CANDIDATES = ("teatis_id", "teatise_id", "id", "dokumendi_id", "teatise_nr")
```

Fall back to geometry WKB. Never call `to_parquet`, `replace`, `unlink`, or `mkdir` with a path below `raw_root`.

- [ ] **Step 5: Run store tests and lint**

Run: `.venv\Scripts\python.exe -m pytest tests/test_processed_notice_store.py -q`

Run: `.venv\Scripts\ruff.exe check processed_notice_store.py tests/test_processed_notice_store.py`

Expected: both PASS.

- [ ] **Step 6: Commit raw discovery**

```powershell
git add processed_notice_store.py tests/test_processed_notice_store.py
git commit -m "Add immutable raw notice month discovery"
```

### Task 3: Add Atomic Processed Storage and Resume Decisions

**Files:**
- Modify: `processed_notice_store.py`
- Modify: `tests/test_processed_notice_store.py`

**Interfaces:**
- Consumes: `MonthKey`, input fingerprint, analysis model version, notice GeoDataFrame, species DataFrame.
- Produces: `ProcessedMonthEntry`, `is_month_current(processed_root, raw_month, model_version) -> bool`, `publish_processed_month(...) -> ProcessedMonthEntry`, `load_processed_notices(...)`, `load_processed_species(...)`.

- [ ] **Step 1: Write failing atomic publication and resume tests**

```python
def test_publish_processed_month_writes_verified_outputs_and_manifest(tmp_path, results):
    entry = publish_processed_month(
        tmp_path,
        MonthKey(2025, 1),
        results.notices,
        results.species,
        input_fingerprint="raw-v1",
        model_version="model-v1",
    )

    assert entry.notice_rows == len(results.notices)
    assert is_month_current(tmp_path, RawMonth(MonthKey(2025, 1), (), "raw-v1"), "model-v1")


def test_failed_species_write_preserves_previous_outputs_and_manifest(tmp_path, results, monkeypatch):
    publish_processed_month(tmp_path, MonthKey(2025, 1), results.notices, results.species,
                            input_fingerprint="raw-v1", model_version="model-v1")
    before = snapshot_processed_month(tmp_path, MonthKey(2025, 1))
    monkeypatch.setattr(pd.DataFrame, "to_parquet", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        publish_processed_month(tmp_path, MonthKey(2025, 1), results.notices, results.species,
                                input_fingerprint="raw-v2", model_version="model-v1")

    assert snapshot_processed_month(tmp_path, MonthKey(2025, 1)) == before
```

- [ ] **Step 2: Run focused tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_processed_notice_store.py -q`

Expected: FAIL because publication APIs do not exist.

- [ ] **Step 3: Implement manifest types and current-month checks**

Use manifest entries containing exact relative paths, row counts, observed bounds, schema fingerprints, input fingerprint, model version, and UTC completion timestamp. `is_month_current` returns true only when status is complete, both files exist, and fingerprints/versions match.

- [ ] **Step 4: Implement journaled two-artifact publication**

Write `notices-<uuid>.parquet` and `species-<uuid>.parquet`, read them back, validate counts and notice CRS, prepare `manifest-<uuid>.json`, and write a transaction journal before replacing existing outputs. Recovery restores both prior outputs if either replacement or manifest promotion fails. Publish the manifest last.

- [ ] **Step 5: Implement notebook loaders**

```python
def load_processed_notices(
    root: Path,
    start: MonthKey | None = None,
    end: MonthKey | None = None,
    columns: Sequence[str] | None = None,
) -> gpd.GeoDataFrame: ...


def load_processed_species(
    root: Path,
    start: MonthKey | None = None,
    end: MonthKey | None = None,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame: ...
```

Read only complete entries with both matching artifacts. Filter manifest entries before reading files and pass projected columns to PyArrow.

- [ ] **Step 6: Add stale-input, model-change, range, recovery, and loader tests**

Assert a changed input fingerprint and changed model version each return false without affecting other months. Inject an interruption after the first output replacement, call a read/current API, and assert the prior pair and manifest are restored. Assert range loaders exclude outside months.

- [ ] **Step 7: Run processed-store tests and lint**

Run: `.venv\Scripts\python.exe -m pytest tests/test_processed_notice_store.py -q`

Run: `.venv\Scripts\ruff.exe check processed_notice_store.py tests/test_processed_notice_store.py`

Expected: both PASS.

- [ ] **Step 8: Commit processed storage**

```powershell
git add processed_notice_store.py tests/test_processed_notice_store.py
git commit -m "Add resumable processed notice storage"
```

### Task 4: Orchestrate Monthly Stand Resolution and Calculation

**Files:**
- Create: `notice_preprocessing.py`
- Create: `tests/test_notice_preprocessing.py`

**Interfaces:**
- Consumes: `discover_raw_months`, `read_raw_month`, `is_month_current`, `publish_processed_month`, `forest_data.load_stands_for_notices`, `notice_analysis.analyze_notices`.
- Produces: `PreprocessProgress`, `MonthFailure`, `PreprocessResult`, `process_month(...)`, `preprocess_notices(...)`.

- [ ] **Step 1: Write a failing end-to-end monthly orchestration test**

```python
def test_preprocess_notices_processes_each_month_once_and_resumes(raw_store, tmp_path):
    calls = []

    def resolve(notices, **kwargs):
        calls.append(("stands", tuple(notices["teatis_id"])))
        return stand_frame()

    def calculate(notices, stands, **kwargs):
        calls.append(("calculate", tuple(notices["teatis_id"])))
        return calculated_notice_frame(notices), species_frame()

    first = preprocess_notices(raw_store, tmp_path, resolve_stands=resolve, calculate=calculate)
    second = preprocess_notices(raw_store, tmp_path, resolve_stands=resolve, calculate=calculate)

    assert first.completed == (MonthKey(2025, 1),)
    assert second.skipped == (MonthKey(2025, 1),)
    assert [name for name, _ in calls] == ["stands", "calculate"]
```

- [ ] **Step 2: Run focused test and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_notice_preprocessing.py -q`

Expected: FAIL because orchestration APIs do not exist.

- [ ] **Step 3: Implement result and progress types**

```python
@dataclass(frozen=True)
class PreprocessProgress:
    month: MonthKey
    stage: Literal["reading", "stands", "details", "publishing"]
    completed: int | None = None
    total: int | None = None


@dataclass(frozen=True)
class MonthFailure:
    month: MonthKey
    message: str


@dataclass(frozen=True)
class PreprocessResult:
    completed: tuple[MonthKey, ...]
    skipped: tuple[MonthKey, ...]
    failed: tuple[MonthFailure, ...]
```

- [ ] **Step 4: Implement one-month processing with injected boundaries**

`process_month` reads raw notices, invokes `resolve_stands`, invokes `calculate`, then publishes. Production defaults adapt existing loaders:

```python
stands = load_stands_for_notices(
    notices,
    exact_loader=load_stands_exact,
    bbox_loader=load_stands_bbox,
    batch_size=50,
    progress_callback=stand_progress,
)
results, species = analyze_notices(
    notices,
    stands,
    detail_progress_callback=detail_progress,
)
```

Move WFS stand-loader wrappers out of `app.py` only if needed to keep `notice_preprocessing.py` free of Streamlit.

- [ ] **Step 5: Implement sequential resumable orchestration**

Discover and range-filter months, skip current months unless `force=True`, catch exceptions per month, and continue. Report failed month plus exception text. Do not add month-level parallelism.

- [ ] **Step 6: Add failure continuation and selection tests**

Use three fixture months, fail the calculator for the middle month, and assert the first and third publish while the middle appears in `failed`. Assert inclusive `start`/`end` selection and `force=True` behavior.

- [ ] **Step 7: Run orchestration tests and lint**

Run: `.venv\Scripts\python.exe -m pytest tests/test_notice_preprocessing.py tests/test_forest_data.py tests/test_notice_analysis.py -q`

Run: `.venv\Scripts\ruff.exe check notice_preprocessing.py tests/test_notice_preprocessing.py`

Expected: both PASS.

- [ ] **Step 8: Commit orchestration**

```powershell
git add notice_preprocessing.py tests/test_notice_preprocessing.py
git commit -m "Add monthly notice preprocessing orchestration"
```

### Task 5: Add the Resumable Command-Line Runner

**Files:**
- Create: `preprocess_notices.py`
- Create: `tests/test_preprocess_notices_cli.py`

**Interfaces:**
- Consumes: `notice_preprocessing.preprocess_notices`.
- Produces: `build_parser() -> argparse.ArgumentParser`, `main(argv: Sequence[str] | None = None) -> int`.

- [ ] **Step 1: Write failing CLI argument and exit-code tests**

```python
def test_cli_defaults_to_existing_raw_and_processed_roots(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "preprocess_notices", lambda raw, processed, **kwargs: captured.update(raw=raw, processed=processed) or empty_result())

    assert cli.main([]) == 0
    assert captured == {"raw": Path("data/notices"), "processed": Path("data/processed/notices")}


def test_cli_returns_nonzero_when_any_month_fails(monkeypatch):
    monkeypatch.setattr(cli, "preprocess_notices", lambda *args, **kwargs: failed_result(2025, 1, "network down"))

    assert cli.main([]) == 1
```

- [ ] **Step 2: Run CLI tests and confirm RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_preprocess_notices_cli.py -q`

Expected: FAIL because the CLI module does not exist.

- [ ] **Step 3: Implement explicit CLI options**

Support `--raw-root`, `--processed-root`, `--start YYYY-MM`, `--end YYYY-MM`, `--force`, and `--detail-workers` (default 12, minimum 1). Print durable progress lines and a final completed/skipped/failed summary. Invalid month syntax exits through `argparse` with code 2; month failures return code 1.

- [ ] **Step 4: Run CLI tests and help smoke check**

Run: `.venv\Scripts\python.exe -m pytest tests/test_preprocess_notices_cli.py -q`

Run: `.venv\Scripts\python.exe preprocess_notices.py --help`

Expected: tests PASS and help lists all six options.

- [ ] **Step 5: Commit CLI**

```powershell
git add preprocess_notices.py tests/test_preprocess_notices_cli.py
git commit -m "Add resumable notice preprocessing CLI"
```

### Task 6: Add Notebook Exploration and Operator Documentation

**Files:**
- Create: `notebooks/notice_exploration.ipynb`
- Modify: `README.md`
- Modify: `tests/test_processed_notice_store.py`

**Interfaces:**
- Consumes: `load_processed_notices`, `load_processed_species`, processed manifest.
- Produces: executable starter notebook and documented commands.

- [ ] **Step 1: Add a loader integration test using projected columns**

```python
def test_notebook_loaders_support_all_months_and_column_projection(processed_store):
    notices = load_processed_notices(processed_store, columns=["teatis_id", "geometry"])
    species = load_processed_species(processed_store, columns=["teatis_id", "species_code", "biomass_tco2"])

    assert list(notices.columns) == ["teatis_id", "geometry"]
    assert list(species.columns) == ["teatis_id", "species_code", "biomass_tco2"]
```

- [ ] **Step 2: Run the focused loader test and confirm its behavior**

Run: `.venv\Scripts\python.exe -m pytest tests/test_processed_notice_store.py::test_notebook_loaders_support_all_months_and_column_projection -q`

Expected: PASS if Task 3 correctly implemented projection; otherwise fix production loader before continuing.

- [ ] **Step 3: Create the notebook with executable cells**

Include cells that import from the repository root, read the processed manifest, load a selected month range, show coverage/shape/missingness, aggregate notice count and area by month, aggregate biomass by month and species, and map a sample only:

```python
from pathlib import Path
from processed_notice_store import MonthKey, load_processed_notices, load_processed_species

PROCESSED_ROOT = Path("../data/processed/notices")
notices = load_processed_notices(PROCESSED_ROOT)
species = load_processed_species(PROCESSED_ROOT)
```

For mapping, use `notices.sample(min(2_000, len(notices)), random_state=42).explore()` and explain why rendering all geometries is unsafe.

- [ ] **Step 4: Document preprocessing and resume commands**

Add exact commands:

```powershell
.venv\Scripts\python.exe preprocess_notices.py
.venv\Scripts\python.exe preprocess_notices.py --start 2016-08 --end 2016-08
.venv\Scripts\python.exe preprocess_notices.py --force --start 2026-08 --end 2026-08
```

Document raw immutability, processed layout, safe interruption/restart, network dependency for stands/details, model-version invalidation, and notebook startup.

- [ ] **Step 5: Execute the notebook against a fixture processed root**

Parameterize `PROCESSED_ROOT` temporarily through notebook execution tooling or an environment variable, execute every cell, and verify there are no exceptions. Do not execute against the full dataset during this test.

- [ ] **Step 6: Commit notebook and docs**

```powershell
git add notebooks/notice_exploration.ipynb README.md tests/test_processed_notice_store.py
git commit -m "Document processed notice exploration"
```

### Task 7: Verify Safety and Start with a One-Month Live Smoke Run

**Files:**
- Modify only if verification exposes a defect in files owned by Tasks 1–6.

**Interfaces:**
- Consumes: complete preprocessing subsystem.
- Produces: verified command and operational evidence before the ten-year run.

- [ ] **Step 1: Record raw-store immutability evidence**

Capture sorted relative path, length, and last-write time for every file under `data/notices` before the live smoke run. Do not hash all 979,163 records; file metadata is sufficient to detect pipeline writes because the pipeline must not touch raw files.

- [ ] **Step 2: Run the full automated suite**

Run: `.venv\Scripts\python.exe -m pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 3: Run full lint**

Run: `.venv\Scripts\ruff.exe check .`

Expected: PASS with zero errors.

- [ ] **Step 4: Run one oldest-month live smoke test**

Run:

```powershell
.venv\Scripts\python.exe preprocess_notices.py --start 2016-08 --end 2016-08
```

Expected: exit 0; `data/processed/notices/year=2016/month=08/notices.parquet`, `species.parquet`, and a complete manifest entry exist.

- [ ] **Step 5: Verify raw-store metadata is unchanged**

Capture the same metadata and compare it byte-for-byte with Step 1. Expected: no path, length, or timestamp changes below `data/notices`.

- [ ] **Step 6: Verify resume behavior on the live month**

Run the same one-month command again.

Expected: exit 0 and summary reports one skipped month with no stand/detail processing.

- [ ] **Step 7: Start the full resumable job only after smoke verification**

Run:

```powershell
.venv\Scripts\python.exe preprocess_notices.py
```

The process may be stopped safely. Rerunning the same command must skip every completed month and continue with the first missing or stale month.

- [ ] **Step 8: Commit any verification-only fixes separately**

If Steps 2–6 required a code correction, add only the affected files and commit with a message naming the verified defect. If no changes were needed, do not create an empty commit.

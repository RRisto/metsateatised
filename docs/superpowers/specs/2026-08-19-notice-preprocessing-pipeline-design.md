# Ten-Year Notice Preprocessing Pipeline

## Goal

Preprocess every forest notice already stored under `data/notices` into resumable,
notebook-ready monthly analysis artifacts. The pipeline must never redownload, rewrite, or delete
the raw notice partitions. It may query the Forest Register only for forest stands and stand
details required by the existing biomass and carbon calculations.

The recovered input contains two raw notice layers for 121 calendar months. Processing combines
the layers by calendar month and produces one independently recoverable monthly result.

## Scope

The pipeline will:

- discover canonical raw partitions exclusively through `data/notices/manifest.json`;
- read both available notice layers for each calendar month;
- deduplicate notices using the same identity rules as synchronization;
- resolve intersecting forest stands with the existing exact-reference and bounding-box fallback
  loaders;
- fetch the stand details required by the existing calculation model;
- calculate notice-level and species-level biomass/carbon results with the existing model;
- publish monthly GeoParquet and Parquet outputs atomically under `data/processed/notices`;
- record completed, failed, and input-stale months in a processing manifest;
- resume without repeating completed months whose inputs and model version have not changed;
- expose a loader suitable for exploratory notebooks; and
- provide a command-line entry point that processes all discovered months or a selected range.

It will not build a new Streamlit dashboard, alter the raw store, infer missing raw months, or
claim that biomass estimates are realized emissions or climate impacts.

## Storage Layout

```text
data/
  notices/                              # immutable input
    manifest.json
    <layer>/year=YYYY/month=MM/notices.parquet
  processed/notices/
    manifest.json
    year=YYYY/month=MM/
      notices.parquet                   # notice-level spatial analysis
      species.parquet                   # species/scope detail rows
```

Temporary files are created beside their destinations and promoted with atomic replacement only
after they can be read back and validated. A failed month leaves any previous completed artifacts
and manifest entry intact.

## Monthly Processing Flow

1. Read the raw manifest once and group its complete partition entries by `(year, month)`.
2. Read each month's existing layer files directly with `pyarrow.parquet.ParquetFile`, avoiding
   Hive path-field inference.
3. Normalize geometry to EPSG:4326, concatenate layers, and deduplicate by the first available
   notice identity field, falling back to geometry WKB.
4. Resolve stands using the existing batched exact-reference loader and spatial fallback.
5. Fetch unique stand-detail records with bounded concurrency and the existing persistent response
   cache. Retries reuse cached successful responses rather than repeating them.
6. Run the existing notice/stand intersection and biomass/carbon calculation model.
7. Write notice-level GeoParquet and species-level Parquet temporary artifacts.
8. Read both artifacts back and verify row counts, schemas, and the notice CRS.
9. Atomically publish both artifacts, then atomically update the processing manifest.

Months run sequentially by default so interruption, API load, memory use, and error reporting stay
predictable. Concurrency remains bounded inside stand-detail retrieval, where the existing code
already supports it.

## Processing Manifest

The processed manifest has a format version, model version, latest successful update time, and one
entry per calendar month. Each completed entry records:

- status (`complete`);
- input partition keys and their raw manifest fingerprints;
- notice and species row counts;
- observed date range;
- output schema fingerprints;
- calculation model version;
- completion timestamp; and
- relative artifact paths.

The input fingerprint is derived from the complete raw manifest entries for that month. A month is
skipped only when its output files exist, its status is complete, its input fingerprint matches,
and its model version equals the current calculation model. Otherwise it is reprocessed.

Failures are reported in the command result and log, but are not published as complete manifest
entries. A later run retries only missing, stale, or failed months.

## Interfaces

The preprocessing module will provide focused interfaces equivalent to:

```python
discover_months(raw_root: Path) -> tuple[MonthKey, ...]
process_month(raw_root: Path, processed_root: Path, month: MonthKey, ...) -> MonthResult
preprocess_notices(raw_root: Path, processed_root: Path, start=None, end=None, ...) -> RunResult
load_processed_notices(processed_root: Path, start=None, end=None) -> GeoDataFrame
load_processed_species(processed_root: Path, start=None, end=None) -> DataFrame
```

The command-line wrapper will accept raw and processed roots, an optional inclusive month range,
a retry/force option, and bounded detail-request concurrency. Its default operation processes every
month present in the recovered raw manifest.

## Notebook Use

The notebook-facing loaders read only complete processed-manifest entries and concatenate the
requested monthly artifacts. A starter notebook will demonstrate:

- loading all completed months or a smaller date range;
- inspecting coverage, row counts, columns, and missingness;
- monthly notice and area trends;
- biomass/carbon summaries by month, notice type, and species; and
- sampling spatial results for maps without loading every geometry into an interactive renderer.

Large tabular exploration should use projected columns and monthly filtering. The notebook will
warn against rendering the entire ten-year geometry collection at once.

## Error Handling and Operations

- Missing or unreadable raw partitions fail before any output mutation.
- A malformed raw manifest is reported with the affected key.
- Stand/detail network errors identify the calendar month and remain resumable.
- Partial temporary outputs are removed on ordinary exceptions; published artifacts are preserved.
- The raw `data/notices` tree is treated as read-only by API and by tests.
- Progress reports the current month, notices, stand-resolution stage, detail completion, completed
  months, skipped months, and failures.

## Testing and Verification

Tests will use small local raw partitions and injected stand/detail loaders. They will verify:

- two raw layers combine into one monthly output;
- processing reads raw files but never writes under the raw root;
- identity deduplication and calculations use real production logic;
- a second identical run skips completed months;
- changed raw fingerprints or model versions reprocess only affected months;
- injected failures preserve previous completed artifacts and manifest bytes;
- interrupted publication is recoverable;
- notebook loaders include only complete, matching artifacts; and
- range selection does not process months outside the requested interval.

Final verification includes the complete unit suite, linting, one local fixture-based end-to-end
run, and a short live smoke run before starting the full ten-year job.

## Execution Expectations

The full preprocessing run is a long-running operation because stand discovery and detail retrieval
require external requests. It must be safe to stop and rerun. No estimate of completion time is
treated as a correctness guarantee; progress and durable monthly checkpoints are the operational
contract.

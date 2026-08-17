# Incremental Forest Notice Synchronization Design

## Purpose

Build a durable local store of raw Forest Register notices without coupling the download to
stand matching, biomass calculation, or dashboard visualization. The user can perform one
large historical download, then request only newer periods and merge them into the stored
history.

This phase delivers reliable acquisition and storage. Large-scale Streamlit visualization and
integration with the biomass calculation are explicitly deferred.

## User Workflow

The Streamlit sidebar provides a separate **Notice data synchronization** section. It shows the
stored date coverage, record count, and last successful synchronization. The user selects a
start and end date and starts a download/update operation.

For the initial synchronization, the user selects the complete historical period. For later
updates, the user selects only the newer period. Completed monthly partitions are skipped by
default. An explicit overlap/update option allows selected completed months to be downloaded
again so changed records can replace their earlier versions.

The synchronization action downloads raw notice geometry and attributes only. It does not load
stands, request stand details, or calculate biomass.

## Storage Architecture

Notices are stored as monthly GeoParquet partitions, separated by source layer:

```text
data/notices/
  archive_notices/year=2018/month=01/notices.parquet
  archive_notices/year=2018/month=02/notices.parquet
  current_notices/year=2026/month=08/notices.parquet
  manifest.json
```

Monthly partitions keep individual writes bounded, avoid rewriting the complete history during
an update, and remain directly readable by GeoPandas, pandas, and future analytical tools.

The manifest contains:

- storage format version;
- source layer and partition identity;
- completed partition status;
- record count and observed date range per partition;
- schema fingerprint;
- last successful synchronization timestamp.

The manifest describes only successfully published partitions. It is written atomically after
partition publication.

## Download and Merge Flow

The selected inclusive date range is divided into calendar-month intervals. Each interval is
queried independently for `metsaregister:teatis_arhiiv` and `metsaregister:teatis`. Existing WFS
pagination is used to request every matching page rather than imposing a user-selected record
cap.

For each layer and month:

1. Detect the layer's notice date field using the existing detection behavior.
2. Apply an inclusive-start, exclusive-end CQL date filter for the monthly interval.
3. Download all pages and report layer, month, page, and cumulative record progress.
4. Convert the features to a GeoDataFrame in EPSG:4326.
5. Add source-layer and detected-date-field metadata columns.
6. Deduplicate by the best available stable notice identifier.
7. If refreshing an existing partition, combine stored and downloaded rows and keep the newly
   downloaded row for matching identifiers.
8. Write a temporary GeoParquet file, validate that it can be read, then atomically replace the
   partition.
9. Atomically update the manifest.

When no stable notice identifier exists, geometry identity is used as the documented fallback.
This matches the existing notice-loading behavior but is less capable of recognizing changed
attributes; the manifest records which identity field or fallback was used.

## Incremental and Overlapping Updates

The normal incremental path skips completed partitions and downloads only months not yet in the
manifest. This supports selecting a newer period without reading or rewriting old partitions.

When overlap/update is enabled, every selected month is queried again. New downloads replace
stored rows with the same stable notice identifier, while stored rows absent from the response
remain in the partition. This is an upsert, not a destructive mirror, because a temporary or
filtered server response must not silently erase local history.

A future explicit mirror/delete mode may handle confirmed upstream deletions, but it is outside
this phase.

## Failure Handling and Resumption

A partition is never marked complete before all WFS pages are received and the GeoParquet file
has been written and read back successfully. Network errors, malformed responses, or write
failures leave the previous partition and manifest entry unchanged.

Temporary files use unique names in the destination directory. A later run retries any selected
partition that is absent or not complete. Failures are reported with the layer and month so the
user can resume a large download without restarting completed months.

The current short-lived WFS page cache may still reduce duplicate requests during a retry, but
the partition store—not the page cache—is the durable source of truth.

## Module Boundaries

- `wfs.py` remains responsible for paginated WFS requests and exposes optional page-progress
  reporting required by synchronization.
- A new notice-store module owns month splitting, partition paths, atomic GeoParquet writes,
  manifest reads/writes, record identity, merge behavior, and stored-coverage summaries.
- A new synchronization service coordinates WFS loading and the notice store through injected
  loader/progress functions so it can be tested without network access.
- `app.py` renders synchronization controls and progress, then calls the service. Existing
  analysis loading and calculation continue unchanged.

## Streamlit Interface

The synchronization section includes:

- stored first and last dates;
- total stored records and completed partition count;
- synchronization start and end date inputs;
- an **Update overlapping completed months** checkbox, off by default;
- a **Download/update notices** button;
- progress and current layer/month/page status;
- a final summary of downloaded, merged, skipped, and failed partitions.

The existing **Refresh source data** action continues to clear short-lived request/detail caches.
It must not delete the durable `data/notices` store. Durable-store removal requires a separate,
explicitly confirmed operation and is not included in this phase.

## Verification

Automated tests use controlled local GeoDataFrames and fake WFS loaders. They cover:

- inclusive date-range splitting across partial and complete months;
- paginated page-progress reporting;
- partition path and manifest schema;
- stable-ID deduplication;
- overlap upserts preferring newly downloaded rows;
- preservation of stored rows missing from a refresh response;
- completed-partition skipping;
- retrying incomplete partitions;
- atomic publication and unchanged prior data after simulated failures;
- stored coverage/count summaries;
- Streamlit synchronization controls without live network calls.

The full project test suite and lint checks must pass. A small manual local run verifies that two
adjacent periods create combined, readable partitions without downloading the first period
again.

## Deferred Work

- nationwide visualization of the combined store in Streamlit;
- connecting stored raw notices to incremental stand-detail and biomass calculation;
- administrative county/municipality enrichment;
- confirmed upstream deletion handling;
- automatic scheduled synchronization.

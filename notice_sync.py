"""Incremental synchronization of Forest Register notice layers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import geopandas as gpd

from data_cache import DEFAULT_CACHE_ROOT
from notice_store import (
    PartitionKey,
    is_partition_complete,
    split_month_intervals,
    summarize_store,
    upsert_partition,
)
from wfs import fetch_wfs_features

NOTICE_LAYERS = {
    "archive_notices": "metsaregister:teatis_arhiiv",
    "current_notices": "metsaregister:teatis",
}

IDENTITY_CANDIDATES = ("teatis_id", "teatise_id", "id", "dokumendi_id", "teatise_nr")


@dataclass(frozen=True)
class SyncProgress:
    layer: str
    month: date
    page: int
    page_rows: int
    cumulative_rows: int


@dataclass(frozen=True)
class SyncResult:
    downloaded_partitions: int
    skipped_partitions: int
    failed_partitions: tuple[str, ...]
    stored_records: int


def synchronize_notices(
    start: date,
    end: date,
    date_columns: Mapping[str, str],
    *,
    root: Path = Path("data/notices"),
    refresh_completed: bool = False,
    fetcher: Callable = fetch_wfs_features,
    progress: Callable[[SyncProgress], None] | None = None,
) -> SyncResult:
    """Download and durably merge notice layers month by month."""
    downloaded_partitions = 0
    skipped_partitions = 0
    failed_partitions: list[str] = []

    for interval in split_month_intervals(start, end):
        month = date(interval.start.year, interval.start.month, 1)
        for layer, type_name in NOTICE_LAYERS.items():
            partition = PartitionKey(layer, month.year, month.month)
            partition_label = f"{layer}/{month:%Y-%m}"
            date_column = date_columns.get(layer)
            if date_column is None:
                failed_partitions.append(f"{partition_label}: missing date column")
                continue
            if not refresh_completed and is_partition_complete(root, partition):
                skipped_partitions += 1
                continue

            def page_progress(
                page: int,
                page_rows: int,
                cumulative_rows: int,
                *,
                _layer: str = layer,
                _month: date = month,
            ) -> None:
                if progress is not None:
                    progress(
                        SyncProgress(
                            layer=_layer,
                            month=_month,
                            page=page,
                            page_rows=page_rows,
                            cumulative_rows=cumulative_rows,
                        )
                    )

            try:
                cql_filter = (
                    f"{date_column} >= '{interval.start.isoformat()}' "
                    f"AND {date_column} < '{interval.end_exclusive.isoformat()}'"
                )
                features = fetcher(
                    type_name,
                    max_features=None,
                    cql_filter=cql_filter,
                    cache_root=DEFAULT_CACHE_ROOT,
                    page_progress=page_progress,
                )
                if features:
                    notices = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
                else:
                    notices = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
                notices["_source_layer"] = layer
                notices["_date_col"] = date_column
                upsert_partition(
                    root,
                    partition,
                    notices,
                    identity_candidates=IDENTITY_CANDIDATES,
                )
                downloaded_partitions += 1
            except Exception as error:
                failed_partitions.append(f"{partition_label}: {error}")

    return SyncResult(
        downloaded_partitions=downloaded_partitions,
        skipped_partitions=skipped_partitions,
        failed_partitions=tuple(failed_partitions),
        stored_records=summarize_store(root).total_records,
    )

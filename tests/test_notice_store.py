from datetime import UTC, date, datetime
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point

from notice_store import (
    PartitionKey,
    is_partition_complete,
    partition_path,
    read_manifest,
    split_month_intervals,
    summarize_store,
    upsert_partition,
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


def test_read_manifest_returns_the_empty_store_document(tmp_path: Path):
    assert read_manifest(tmp_path) == {
        "format_version": 1,
        "last_sync_at": None,
        "partitions": {},
    }


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


def test_upsert_failure_preserves_existing_partition_and_manifest(tmp_path: Path, monkeypatch):
    key = PartitionKey("archive_notices", 2025, 3)
    original = notice_frame(
        [{"teatis_id": 1, "otsus": "old", "geometry": Point(24.0, 59.0)}]
    )
    updated = notice_frame(
        [{"teatis_id": 1, "otsus": "new", "geometry": Point(24.0, 59.0)}]
    )
    upsert_partition(tmp_path, key, original, identity_candidates=["teatis_id"])
    partition_bytes = partition_path(tmp_path, key).read_bytes()
    manifest_bytes = (tmp_path / "manifest.json").read_bytes()

    def fail_to_parquet(*args, **kwargs):
        raise OSError("simulated GeoParquet write failure")

    monkeypatch.setattr(gpd.GeoDataFrame, "to_parquet", fail_to_parquet)

    with pytest.raises(OSError, match="simulated GeoParquet write failure"):
        upsert_partition(tmp_path, key, updated, identity_candidates=["teatis_id"])

    assert partition_path(tmp_path, key).read_bytes() == partition_bytes
    assert (tmp_path / "manifest.json").read_bytes() == manifest_bytes


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

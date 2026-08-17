from datetime import date
from pathlib import Path

import geopandas as gpd

import notice_sync
from notice_store import PartitionKey, is_partition_complete, partition_path
from notice_sync import SyncProgress, synchronize_notices


def feature(feature_id, decision_date, **properties):
    return {
        "type": "Feature",
        "id": f"teatis.{feature_id}",
        "properties": {
            "teatis_id": feature_id,
            "otsuse_kp": decision_date,
            **properties,
        },
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


def test_synchronize_refreshes_completed_partition_with_upsert(tmp_path: Path):
    archive_responses = iter(
        [
            [
                feature(1, "2025-01-15T10:00:00Z", decision="old"),
                feature(2, "2025-01-20T10:00:00Z", decision="keep"),
            ],
            [
                feature(1, "2025-01-15T10:00:00Z", decision="new"),
                feature(3, "2025-01-25T10:00:00Z", decision="added"),
            ],
        ]
    )

    def fetcher(type_name, **kwargs):
        if type_name == "metsaregister:teatis_arhiiv":
            return next(archive_responses)
        return [feature(99, "2025-01-15T10:00:00Z")]

    date_columns = {
        "archive_notices": "otsuse_kp",
        "current_notices": "otsuse_kp",
    }
    synchronize_notices(
        date(2025, 1, 1), date(2025, 1, 31), date_columns, root=tmp_path, fetcher=fetcher
    )
    result = synchronize_notices(
        date(2025, 1, 1),
        date(2025, 1, 31),
        date_columns,
        root=tmp_path,
        fetcher=fetcher,
        refresh_completed=True,
    )
    stored = gpd.read_parquet(
        partition_path(tmp_path, PartitionKey("archive_notices", 2025, 1))
    ).sort_values("teatis_id")

    assert result.downloaded_partitions == 2
    assert stored[["teatis_id", "decision"]].to_dict("records") == [
        {"teatis_id": 1, "decision": "new"},
        {"teatis_id": 2, "decision": "keep"},
        {"teatis_id": 3, "decision": "added"},
    ]


def test_synchronize_adapts_page_progress_to_layer_and_month(tmp_path: Path):
    updates = []

    def fetcher(type_name, **kwargs):
        kwargs["page_progress"](1, 2, 2)
        return [feature(1, "2025-01-15T10:00:00Z")]

    synchronize_notices(
        date(2025, 1, 1),
        date(2025, 1, 31),
        {"archive_notices": "otsuse_kp", "current_notices": "otsuse_kp"},
        root=tmp_path,
        fetcher=fetcher,
        progress=updates.append,
    )

    assert updates[0] == SyncProgress(
        layer="archive_notices",
        month=date(2025, 1, 1),
        page=1,
        page_rows=2,
        cumulative_rows=2,
    )
    assert updates[1].layer == "current_notices"


def test_synchronize_stores_empty_completed_partitions(tmp_path: Path):
    def fetcher(type_name, **kwargs):
        return []

    result = synchronize_notices(
        date(2025, 1, 1),
        date(2025, 1, 31),
        {"archive_notices": "otsuse_kp", "current_notices": "otsuse_kp"},
        root=tmp_path,
        fetcher=fetcher,
    )

    assert result.downloaded_partitions == 2
    assert is_partition_complete(tmp_path, PartitionKey("archive_notices", 2025, 1))
    assert is_partition_complete(tmp_path, PartitionKey("current_notices", 2025, 1))


def test_synchronize_reports_missing_date_column_without_stopping_other_layers(tmp_path: Path):
    def fetcher(type_name, **kwargs):
        return [feature(1, "2025-01-15T10:00:00Z")]

    result = synchronize_notices(
        date(2025, 1, 1),
        date(2025, 1, 31),
        {"archive_notices": "otsuse_kp"},
        root=tmp_path,
        fetcher=fetcher,
    )

    assert result.downloaded_partitions == 1
    assert result.failed_partitions == ("current_notices/2025-01: missing date column",)


def test_synchronize_partial_month_refreshes_completed_partition(tmp_path: Path):
    calls = []

    def fetcher(type_name, **kwargs):
        calls.append((type_name, kwargs["cql_filter"], kwargs["force_refresh"]))
        return [feature(len(calls), "2025-01-15T10:00:00Z")]

    date_columns = {"archive_notices": "otsuse_kp", "current_notices": "otsuse_kp"}
    synchronize_notices(
        date(2025, 1, 1), date(2025, 1, 31), date_columns, root=tmp_path, fetcher=fetcher
    )
    calls.clear()

    result = synchronize_notices(
        date(2025, 1, 15), date(2025, 1, 16), date_columns, root=tmp_path, fetcher=fetcher
    )

    assert result.downloaded_partitions == 2
    assert result.skipped_partitions == 0
    assert calls == [
        (
            "metsaregister:teatis_arhiiv",
            "otsuse_kp >= '2025-01-01' AND otsuse_kp < '2025-02-01'",
            True,
        ),
        (
            "metsaregister:teatis",
            "otsuse_kp >= '2025-01-01' AND otsuse_kp < '2025-02-01'",
            True,
        ),
    ]


def test_synchronize_bypasses_wfs_cache_for_explicit_refresh(tmp_path: Path):
    force_refreshes = []

    def fetcher(type_name, **kwargs):
        force_refreshes.append(kwargs["force_refresh"])
        return [feature(len(force_refreshes), "2025-01-15T10:00:00Z")]

    synchronize_notices(
        date(2025, 1, 1),
        date(2025, 1, 31),
        {"archive_notices": "otsuse_kp", "current_notices": "otsuse_kp"},
        root=tmp_path,
        fetcher=fetcher,
        refresh_completed=True,
    )

    assert force_refreshes == [True, True]


def test_synchronize_records_completion_check_failure_and_continues(tmp_path: Path, monkeypatch):
    def completion_check(root, partition):
        if partition.layer == "archive_notices":
            raise OSError("manifest unreadable")
        return False

    calls = []

    def fetcher(type_name, **kwargs):
        calls.append(type_name)
        return [feature(1, "2025-01-15T10:00:00Z")]

    monkeypatch.setattr(notice_sync, "is_partition_complete", completion_check)

    result = synchronize_notices(
        date(2025, 1, 1),
        date(2025, 1, 31),
        {"archive_notices": "otsuse_kp", "current_notices": "otsuse_kp"},
        root=tmp_path,
        fetcher=fetcher,
    )

    assert result.downloaded_partitions == 1
    assert result.failed_partitions == ("archive_notices/2025-01: manifest unreadable",)
    assert calls == ["metsaregister:teatis"]

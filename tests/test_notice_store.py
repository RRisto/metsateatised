from datetime import UTC, date, datetime
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import geopandas as gpd
import pytest
from shapely.geometry import Point

import notice_store
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


def test_manifest_write_failure_preserves_existing_partition_and_manifest(
    tmp_path: Path, monkeypatch
):
    key = PartitionKey("archive_notices", 2025, 3)
    original = notice_frame(
        [{"teatis_id": 1, "otsus": "old", "geometry": Point(24.0, 59.0)}]
    )
    refreshed = notice_frame(
        [{"teatis_id": 1, "otsus": "new", "geometry": Point(24.0, 59.0)}]
    )
    upsert_partition(tmp_path, key, original, identity_candidates=["teatis_id"])
    partition_bytes = partition_path(tmp_path, key).read_bytes()
    manifest_bytes = (tmp_path / "manifest.json").read_bytes()

    def fail_manifest_serialization(*args, **kwargs):
        raise OSError("simulated manifest write failure")

    monkeypatch.setattr(notice_store.json, "dump", fail_manifest_serialization)

    with pytest.raises(OSError, match="simulated manifest write failure"):
        upsert_partition(tmp_path, key, refreshed, identity_candidates=["teatis_id"])

    assert partition_path(tmp_path, key).read_bytes() == partition_bytes
    assert (tmp_path / "manifest.json").read_bytes() == manifest_bytes
    assert is_partition_complete(tmp_path, key)


def test_upsert_uses_geometry_for_rows_with_missing_identity_values(tmp_path: Path):
    key = PartitionKey("archive_notices", 2025, 3)
    first = notice_frame(
        [
            {"teatis_id": None, "otsus": "north", "geometry": Point(24.0, 59.0)},
            {"otsus": "west", "geometry": Point(25.0, 58.0)},
            {"teatis_id": 1, "otsus": "old", "geometry": Point(26.0, 57.0)},
        ]
    )
    refreshed = notice_frame(
        [
            {"teatis_id": None, "otsus": "south", "geometry": Point(27.0, 56.0)},
            {"teatis_id": 1, "otsus": "new", "geometry": Point(26.0, 57.0)},
        ]
    )

    count = upsert_partition(tmp_path, key, first, identity_candidates=["teatis_id"])
    count = upsert_partition(tmp_path, key, refreshed, identity_candidates=["teatis_id"])
    stored = gpd.read_parquet(partition_path(tmp_path, key))

    assert count == 4
    assert set(stored["otsus"]) == {"north", "west", "south", "new"}


def test_upsert_uses_geometry_for_blank_identity_values(tmp_path: Path):
    """Repeated blank IDs are missing values, not one shared stable identity."""
    key = PartitionKey("archive_notices", 2025, 3)
    incoming = notice_frame(
        [
            {"teatis_id": "", "otsus": "north", "geometry": Point(24.0, 59.0)},
            {"teatis_id": "   ", "otsus": "west", "geometry": Point(25.0, 58.0)},
            {"teatis_id": "", "otsus": "south", "geometry": Point(27.0, 56.0)},
        ]
    )

    count = upsert_partition(tmp_path, key, incoming, identity_candidates=["teatis_id"])
    stored = gpd.read_parquet(partition_path(tmp_path, key))

    assert count == 3
    assert set(stored["otsus"]) == {"north", "west", "south"}


def test_manifest_serialization_failure_removes_partial_temporary_file(
    tmp_path: Path, monkeypatch
):
    """A json.dump failure must not strand a partial manifest-*.json file."""
    key = PartitionKey("archive_notices", 2025, 3)
    incoming = notice_frame(
        [{"teatis_id": 1, "otsus": "new", "geometry": Point(24.0, 59.0)}]
    )

    def fail_manifest_serialization(*args, **kwargs):
        raise OSError("simulated manifest serialization failure")

    monkeypatch.setattr(notice_store.json, "dump", fail_manifest_serialization)

    with pytest.raises(OSError, match="simulated manifest serialization failure"):
        upsert_partition(tmp_path, key, incoming, identity_candidates=["teatis_id"])

    assert not list(tmp_path.glob("manifest-*.json"))


def test_interrupted_partition_publication_is_recovered_on_next_store_access(
    tmp_path: Path, monkeypatch
):
    """An interruption after partition replace must restore the manifest-matched old file."""

    class SimulatedInterruption(BaseException):
        pass

    key = PartitionKey("archive_notices", 2025, 3)
    original = notice_frame(
        [{"teatis_id": 1, "otsus": "old", "geometry": Point(24.0, 59.0)}]
    )
    refreshed = notice_frame(
        [{"teatis_id": 1, "otsus": "new", "geometry": Point(24.0, 59.0)}]
    )
    upsert_partition(tmp_path, key, original, identity_candidates=["teatis_id"])
    old_partition = partition_path(tmp_path, key).read_bytes()
    old_manifest = (tmp_path / "manifest.json").read_bytes()
    real_replace = Path.replace

    def interrupt_manifest_replace(source, target):
        if source.name.startswith("manifest-") and Path(target) == tmp_path / "manifest.json":
            raise SimulatedInterruption
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", interrupt_manifest_replace)

    with pytest.raises(SimulatedInterruption):
        upsert_partition(tmp_path, key, refreshed, identity_candidates=["teatis_id"])

    read_manifest(tmp_path)

    assert partition_path(tmp_path, key).read_bytes() == old_partition
    assert (tmp_path / "manifest.json").read_bytes() == old_manifest
    assert not list(tmp_path.rglob("*.previous.parquet"))
    assert not list(tmp_path.glob(".notice-store-transaction*.json"))


def test_concurrent_upserts_preserve_merged_rows(tmp_path: Path, monkeypatch):
    """Two same-store sessions must not both merge from the same stale partition snapshot."""
    key = PartitionKey("archive_notices", 2025, 3)
    upsert_partition(
        tmp_path,
        key,
        notice_frame([{"teatis_id": 1, "geometry": Point(24.0, 59.0)}]),
        identity_candidates=["teatis_id"],
    )
    first_writer_inside = threading.Event()
    second_writer_inside = threading.Event()
    writer_count = 0
    writer_count_lock = threading.Lock()
    real_to_parquet = gpd.GeoDataFrame.to_parquet

    def deliberately_overlap_writers(frame, *args, **kwargs):
        nonlocal writer_count
        with writer_count_lock:
            writer_count += 1
            current_writer = writer_count
        if current_writer == 1:
            first_writer_inside.set()
            second_writer_inside.wait(0.3)
        else:
            second_writer_inside.set()
        return real_to_parquet(frame, *args, **kwargs)

    monkeypatch.setattr(gpd.GeoDataFrame, "to_parquet", deliberately_overlap_writers)

    def add_notice(notice_id, longitude):
        return upsert_partition(
            tmp_path,
            key,
            notice_frame([{"teatis_id": notice_id, "geometry": Point(longitude, 59.0)}]),
            identity_candidates=["teatis_id"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(add_notice, 2, 25.0)
        assert first_writer_inside.wait(1)
        second = executor.submit(add_notice, 3, 26.0)
        first.result(timeout=5)
        second.result(timeout=5)

    stored = gpd.read_parquet(partition_path(tmp_path, key))

    assert set(stored["teatis_id"]) == {1, 2, 3}


def test_concurrent_upserts_preserve_all_manifest_entries(tmp_path: Path, monkeypatch):
    """Two partitions published from concurrent sessions must not overwrite manifest entries."""
    keys = [
        PartitionKey("archive_notices", 2025, 3),
        PartitionKey("current_notices", 2025, 3),
    ]
    first_writer_inside = threading.Event()
    second_writer_inside = threading.Event()
    writer_count = 0
    writer_count_lock = threading.Lock()
    real_to_parquet = gpd.GeoDataFrame.to_parquet

    def deliberately_overlap_writers(frame, *args, **kwargs):
        nonlocal writer_count
        with writer_count_lock:
            writer_count += 1
            current_writer = writer_count
        if current_writer == 1:
            first_writer_inside.set()
            second_writer_inside.wait(0.3)
        else:
            second_writer_inside.set()
        return real_to_parquet(frame, *args, **kwargs)

    monkeypatch.setattr(gpd.GeoDataFrame, "to_parquet", deliberately_overlap_writers)

    def write_partition(key, notice_id):
        return upsert_partition(
            tmp_path,
            key,
            notice_frame([{"teatis_id": notice_id, "geometry": Point(24.0, 59.0)}]),
            identity_candidates=["teatis_id"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write_partition, keys[0], 1)
        assert first_writer_inside.wait(1)
        second = executor.submit(write_partition, keys[1], 2)
        first.result(timeout=5)
        second.result(timeout=5)

    assert set(read_manifest(tmp_path)["partitions"]) == {
        "archive_notices/2025-03",
        "current_notices/2025-03",
    }


def test_live_store_lock_times_out_without_breaking_owner(tmp_path: Path):
    """A second process must time out rather than remove a lock held by a live process."""
    script = """
import sys
from pathlib import Path
from notice_store import _store_lock

try:
    with _store_lock(Path(sys.argv[1]), timeout=0.05):
        pass
except TimeoutError:
    raise SystemExit(23)
raise SystemExit(24)
"""

    with notice_store._store_lock(tmp_path, timeout=1):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            cwd=Path(__file__).parents[1],
            check=False,
        )
        assert result.returncode == 23
        assert (tmp_path / ".notice-store.lock").exists()

    assert not (tmp_path / ".notice-store.lock").exists()


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


def test_summarize_store_parses_manifest_only_once(tmp_path: Path, monkeypatch):
    """Summary cost must remain linear instead of reparsing the full manifest per partition."""
    for layer in ("archive_notices", "current_notices"):
        upsert_partition(
            tmp_path,
            PartitionKey(layer, 2025, 3),
            notice_frame([{"teatis_id": layer, "geometry": Point(24.0, 59.0)}]),
            identity_candidates=["teatis_id"],
        )

    parse_count = 0
    real_loads = notice_store.json.loads

    def count_manifest_parse(*args, **kwargs):
        nonlocal parse_count
        parse_count += 1
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(notice_store.json, "loads", count_manifest_parse)

    summary = summarize_store(tmp_path)

    assert summary.completed_partitions == 2
    assert parse_count == 1

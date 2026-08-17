"""Durable, monthly GeoParquet storage for raw forest notices."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq


@dataclass(frozen=True)
class PartitionKey:
    layer: str
    year: int
    month: int


@dataclass(frozen=True)
class MonthInterval:
    start: date
    end_exclusive: date


@dataclass(frozen=True)
class StoreSummary:
    total_records: int
    completed_partitions: int
    first_date: date | None
    last_date: date | None
    last_sync_at: datetime | None


def split_month_intervals(start: date, end: date) -> list[MonthInterval]:
    """Split an inclusive date range into adjacent calendar-month intervals."""
    if start > end:
        raise ValueError("start must not be after end")

    end_exclusive = end + timedelta(days=1)
    intervals = []
    cursor = start
    while cursor < end_exclusive:
        next_month = date(
            cursor.year + (cursor.month == 12),
            1 if cursor.month == 12 else cursor.month + 1,
            1,
        )
        interval_end = min(next_month, end_exclusive)
        intervals.append(MonthInterval(cursor, interval_end))
        cursor = interval_end
    return intervals


def partition_path(root: Path, key: PartitionKey) -> Path:
    return root / key.layer / f"year={key.year:04d}" / f"month={key.month:02d}" / "notices.parquet"


def read_manifest(root: Path) -> dict:
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return {"format_version": 1, "last_sync_at": None, "partitions": {}}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _manifest_key(key: PartitionKey) -> str:
    return f"{key.layer}/{key.year:04d}-{key.month:02d}"


def is_partition_complete(root: Path, key: PartitionKey) -> bool:
    entry = read_manifest(root).get("partitions", {}).get(_manifest_key(key), {})
    return entry.get("status") == "complete" and partition_path(root, key).exists()


def _to_wgs84(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if frame.crs is None:
        return frame.set_crs("EPSG:4326", allow_override=True)
    return frame.to_crs("EPSG:4326")


def _read_partition(path: Path) -> gpd.GeoDataFrame:
    """Read exactly one GeoParquet file without inferring Hive directory fields."""
    return gpd.GeoDataFrame.from_arrow(pq.ParquetFile(path).read())


def _identity_field(
    stored: gpd.GeoDataFrame | None,
    incoming: gpd.GeoDataFrame,
    candidates: Sequence[str],
) -> str:
    columns = set(incoming.columns)
    if stored is not None:
        columns.update(stored.columns)
    return next((candidate for candidate in candidates if candidate in columns), "geometry_wkb")


def _schema_fingerprint(frame: gpd.GeoDataFrame) -> str:
    pairs = sorted(f"{column}:{dtype}" for column, dtype in frame.dtypes.items())
    return sha256("\n".join(pairs).encode("utf-8")).hexdigest()


def _observed_bounds(frame: gpd.GeoDataFrame) -> tuple[str | None, str | None]:
    dates = []
    if "_date_col" not in frame.columns:
        return None, None

    for _, row in frame.iterrows():
        date_column = row["_date_col"]
        if isinstance(date_column, str) and date_column in frame.columns:
            parsed = pd.to_datetime(row[date_column], utc=True, errors="coerce")
            if not pd.isna(parsed):
                dates.append(parsed.date())
    if not dates:
        return None, None
    return min(dates).isoformat(), max(dates).isoformat()


def _utc_timestamp(now: datetime | None) -> datetime:
    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _write_manifest(root: Path, manifest: dict) -> None:
    temporary_path = _prepare_manifest(root, manifest)
    try:
        temporary_path.replace(root / "manifest.json")
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _prepare_manifest(root: Path, manifest: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=root, prefix="manifest-", suffix=".json", delete=False
    ) as temporary:
        json.dump(manifest, temporary, indent=2, sort_keys=True)
        return Path(temporary.name)


def _deduplication_keys(frame: gpd.GeoDataFrame, identity_field: str) -> list[tuple]:
    geometry_keys = frame.geometry.to_wkb().tolist()
    if identity_field == "geometry_wkb":
        return [
            ("geometry_wkb", geometry_key) if geometry_key is not None else ("row", index)
            for index, geometry_key in enumerate(geometry_keys)
        ]

    return [
        ("identity", value)
        if not pd.isna(value)
        else (("geometry_wkb", geometry_key) if geometry_key is not None else ("row", index))
        for index, (value, geometry_key) in enumerate(
            zip(frame[identity_field], geometry_keys, strict=True)
        )
    ]


def _publish_partition_and_manifest(
    destination: Path,
    temporary_partition: Path,
    root: Path,
    manifest: dict,
) -> None:
    """Publish partition and manifest together, restoring the old partition on failure."""
    backup_partition = destination.parent / f"notices-{uuid4().hex}.previous.parquet"
    had_destination = destination.exists()
    published_partition = False
    temporary_manifest: Path | None = None

    try:
        temporary_manifest = _prepare_manifest(root, manifest)
        if had_destination:
            destination.replace(backup_partition)
        temporary_partition.replace(destination)
        published_partition = True
        temporary_manifest.replace(root / "manifest.json")
    except Exception:
        if published_partition and destination.exists():
            destination.unlink()
        if backup_partition.exists():
            backup_partition.replace(destination)
        raise
    else:
        if backup_partition.exists():
            backup_partition.unlink()
    finally:
        if temporary_partition.exists():
            temporary_partition.unlink()
        if temporary_manifest is not None and temporary_manifest.exists():
            temporary_manifest.unlink()


def upsert_partition(
    root: Path,
    key: PartitionKey,
    incoming: gpd.GeoDataFrame,
    *,
    identity_candidates: Sequence[str],
    now: datetime | None = None,
) -> int:
    """Merge new notice rows into one durable monthly partition."""
    destination = partition_path(root, key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stored = _read_partition(destination) if destination.exists() else None
    stored_wgs84 = _to_wgs84(stored) if stored is not None else None
    incoming_wgs84 = _to_wgs84(incoming)
    identity_field = _identity_field(stored_wgs84, incoming_wgs84, identity_candidates)

    frames = [frame for frame in (stored_wgs84, incoming_wgs84) if frame is not None]
    merged = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True, sort=False), geometry="geometry", crs="EPSG:4326"
    )
    temporary_identity = "__notice_store_identity__"
    while temporary_identity in merged.columns:
        temporary_identity = f"_{temporary_identity}"
    merged[temporary_identity] = _deduplication_keys(merged, identity_field)
    merged = merged.drop_duplicates(subset=[temporary_identity], keep="last")
    merged = merged.drop(columns=[temporary_identity])
    merged = gpd.GeoDataFrame(merged, geometry="geometry", crs="EPSG:4326")

    observed_start, observed_end = _observed_bounds(merged)
    timestamp = _utc_timestamp(now)
    manifest = read_manifest(root)
    manifest.setdefault("format_version", 1)
    manifest.setdefault("partitions", {})
    manifest["last_sync_at"] = timestamp.isoformat()
    manifest["partitions"][_manifest_key(key)] = {
        "status": "complete",
        "record_count": len(merged),
        "identity_field": identity_field,
        "observed_start": observed_start,
        "observed_end": observed_end,
        "schema_fingerprint": _schema_fingerprint(merged),
        "updated_at": timestamp.isoformat(),
    }

    temporary_partition = destination.parent / f"notices-{uuid4().hex}.parquet"
    try:
        merged.to_parquet(temporary_partition)
        verified = gpd.read_parquet(temporary_partition)
        if (
            len(verified) != len(merged)
            or verified.crs is None
            or not verified.crs.equals("EPSG:4326")
        ):
            raise ValueError("temporary GeoParquet validation failed")
    except Exception:
        if temporary_partition.exists():
            temporary_partition.unlink()
        raise
    _publish_partition_and_manifest(destination, temporary_partition, root, manifest)
    return len(merged)


def summarize_store(root: Path) -> StoreSummary:
    manifest = read_manifest(root)
    total_records = 0
    completed_partitions = 0
    observed_dates = []
    sync_times = []

    for manifest_key, entry in manifest.get("partitions", {}).items():
        if entry.get("status") != "complete":
            continue
        layer, period = manifest_key.rsplit("/", 1)
        year, month = (int(value) for value in period.split("-", 1))
        if not is_partition_complete(root, PartitionKey(layer, year, month)):
            continue
        completed_partitions += 1
        total_records += entry.get("record_count", 0)
        for field in ("observed_start", "observed_end"):
            value = entry.get(field)
            if value is not None:
                observed_dates.append(date.fromisoformat(value))
        updated_at = entry.get("updated_at")
        if updated_at is not None:
            sync_times.append(datetime.fromisoformat(updated_at))

    return StoreSummary(
        total_records=total_records,
        completed_partitions=completed_partitions,
        first_date=min(observed_dates) if observed_dates else None,
        last_date=max(observed_dates) if observed_dates else None,
        last_sync_at=max(sync_times) if sync_times else None,
    )

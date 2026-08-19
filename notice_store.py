"""Durable, monthly GeoParquet storage for raw forest notices."""

import json
import os
import socket
import time
from collections.abc import Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq

STORE_LOCK_TIMEOUT_SECONDS = 10.0
STORE_LOCK_POLL_SECONDS = 0.05
STORE_LOCK_FILENAME = ".notice-store.lock"
TRANSACTION_FILENAME = ".notice-store-transaction.json"


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


def _read_manifest_unlocked(root: Path) -> dict:
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return {"format_version": 1, "last_sync_at": None, "partitions": {}}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        return getattr(error, "winerror", None) != 87
    return True


def _remove_stale_lock(lock_path: Path) -> bool:
    try:
        owner = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if owner.get("hostname") != socket.gethostname():
        return False
    try:
        pid = int(owner["pid"])
    except (KeyError, TypeError, ValueError):
        return False
    if _process_is_running(pid):
        return False
    with suppress(FileNotFoundError):
        lock_path.unlink()
    return True


@contextmanager
def _store_lock(
    root: Path,
    *,
    timeout: float = STORE_LOCK_TIMEOUT_SECONDS,
):
    """Hold a cross-process store lock, waiting only for a bounded duration."""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / STORE_LOCK_FILENAME
    deadline = time.monotonic() + timeout
    owner_document = json.dumps(
        {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "created_at": datetime.now(UTC).isoformat(),
        },
        sort_keys=True,
    ).encode("utf-8")

    while True:
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _remove_stale_lock(lock_path):
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"timed out waiting for notice store lock: {lock_path}"
                ) from None
            time.sleep(min(STORE_LOCK_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
            continue
        try:
            os.write(descriptor, owner_document)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            lock_path.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
            break

    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def _transaction_member(root: Path, relative_path: str) -> Path:
    resolved_root = root.resolve()
    candidate = (root / Path(relative_path)).resolve()
    if candidate != resolved_root and resolved_root not in candidate.parents:
        raise ValueError("notice store transaction path escapes the store root")
    return candidate


def _write_transaction(root: Path, transaction: dict) -> Path:
    path = root / TRANSACTION_FILENAME
    document = json.dumps(transaction, indent=2, sort_keys=True).encode("utf-8")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(descriptor, document)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _recover_pending_transaction(root: Path) -> None:
    transaction_path = root / TRANSACTION_FILENAME
    if not transaction_path.exists():
        return
    try:
        transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
        destination = _transaction_member(root, transaction["destination"])
        temporary_partition = _transaction_member(root, transaction["temporary_partition"])
        backup_partition = _transaction_member(root, transaction["backup_partition"])
        temporary_manifest = _transaction_member(root, transaction["temporary_manifest"])
        had_destination = bool(transaction["had_destination"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("invalid notice store transaction state") from error

    if temporary_manifest.exists():
        if had_destination:
            if backup_partition.exists():
                destination.unlink(missing_ok=True)
                backup_partition.replace(destination)
            elif not destination.exists():
                raise RuntimeError("cannot recover the previous notice partition")
        elif not temporary_partition.exists():
            destination.unlink(missing_ok=True)
    elif not destination.exists():
        raise RuntimeError("published notice manifest has no matching partition")

    temporary_partition.unlink(missing_ok=True)
    temporary_manifest.unlink(missing_ok=True)
    backup_partition.unlink(missing_ok=True)
    transaction_path.unlink()


def read_manifest(root: Path) -> dict:
    with _store_lock(root):
        _recover_pending_transaction(root)
        return _read_manifest_unlocked(root)


def _manifest_key(key: PartitionKey) -> str:
    return f"{key.layer}/{key.year:04d}-{key.month:02d}"


def is_partition_complete(root: Path, key: PartitionKey) -> bool:
    with _store_lock(root):
        _recover_pending_transaction(root)
        manifest = _read_manifest_unlocked(root)
        entry = manifest.get("partitions", {}).get(_manifest_key(key), {})
        return entry.get("status") == "complete" and partition_path(root, key).exists()


def rebuild_manifest(
    root: Path,
    *,
    identity_candidates: Sequence[str],
) -> dict:
    """Rebuild the manifest from already-published canonical partitions."""
    root = root.resolve()
    with _store_lock(root):
        _recover_pending_transaction(root)
        partitions: dict[str, dict] = {}
        sync_times: list[datetime] = []

        for path in sorted(root.glob("*/year=*/month=*/notices.parquet")):
            try:
                layer = path.parents[2].name
                year = int(path.parents[1].name.removeprefix("year="))
                month = int(path.parent.name.removeprefix("month="))
                key = PartitionKey(layer, year, month)
            except ValueError as error:
                raise ValueError(f"invalid notice partition path: {path}") from error
            if not 1 <= month <= 12 or partition_path(root, key) != path:
                raise ValueError(f"invalid notice partition path: {path}")

            frame = _to_wgs84(_read_partition(path))
            observed_start, observed_end = _observed_bounds(frame)
            updated_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            sync_times.append(updated_at)
            partitions[_manifest_key(key)] = {
                "status": "complete",
                "record_count": len(frame),
                "identity_field": _identity_field(None, frame, identity_candidates),
                "observed_start": observed_start,
                "observed_end": observed_end,
                "schema_fingerprint": _schema_fingerprint(frame),
                "updated_at": updated_at.isoformat(),
            }

        manifest = {
            "format_version": 1,
            "last_sync_at": max(sync_times).isoformat() if sync_times else None,
            "partitions": partitions,
        }
        _write_manifest(root, manifest)
        return manifest


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
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=root,
            prefix="manifest-",
            suffix=".json",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(manifest, temporary, indent=2, sort_keys=True)
        return temporary_path
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _deduplication_keys(frame: gpd.GeoDataFrame, identity_field: str) -> list[tuple]:
    geometry_keys = frame.geometry.to_wkb().tolist()
    if identity_field == "geometry_wkb":
        return [
            ("geometry_wkb", geometry_key) if geometry_key is not None else ("row", index)
            for index, geometry_key in enumerate(geometry_keys)
        ]

    return [
        ("identity", value)
        if not pd.isna(value) and not (isinstance(value, str) and not value.strip())
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
    """Publish with a journal that can restore an interrupted prior partition."""
    backup_partition = destination.parent / f"notices-{uuid4().hex}.previous.parquet"
    had_destination = destination.exists()
    try:
        temporary_manifest = _prepare_manifest(root, manifest)
    except BaseException:
        temporary_partition.unlink(missing_ok=True)
        raise

    transaction = {
        "destination": destination.relative_to(root).as_posix(),
        "temporary_partition": temporary_partition.relative_to(root).as_posix(),
        "backup_partition": backup_partition.relative_to(root).as_posix(),
        "temporary_manifest": temporary_manifest.relative_to(root).as_posix(),
        "had_destination": had_destination,
    }
    try:
        transaction_path = _write_transaction(root, transaction)
    except BaseException:
        temporary_partition.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        raise

    try:
        if had_destination:
            destination.replace(backup_partition)
        temporary_partition.replace(destination)
        temporary_manifest.replace(root / "manifest.json")
    except Exception:
        _recover_pending_transaction(root)
        raise

    backup_partition.unlink(missing_ok=True)
    temporary_partition.unlink(missing_ok=True)
    temporary_manifest.unlink(missing_ok=True)
    transaction_path.unlink()


def upsert_partition(
    root: Path,
    key: PartitionKey,
    incoming: gpd.GeoDataFrame,
    *,
    identity_candidates: Sequence[str],
    now: datetime | None = None,
) -> int:
    """Merge new notice rows into one durable monthly partition."""
    root = root.resolve()
    with _store_lock(root):
        _recover_pending_transaction(root)
        destination = partition_path(root, key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stored = _read_partition(destination) if destination.exists() else None
        stored_wgs84 = _to_wgs84(stored) if stored is not None else None
        incoming_wgs84 = _to_wgs84(incoming)
        identity_field = _identity_field(stored_wgs84, incoming_wgs84, identity_candidates)

        frames = [frame for frame in (stored_wgs84, incoming_wgs84) if frame is not None]
        merged = gpd.GeoDataFrame(
            pd.concat(frames, ignore_index=True, sort=False),
            geometry="geometry",
            crs="EPSG:4326",
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
        manifest = _read_manifest_unlocked(root)
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
            temporary_partition.unlink(missing_ok=True)
            raise
        _publish_partition_and_manifest(destination, temporary_partition, root, manifest)
        return len(merged)


def summarize_store(root: Path) -> StoreSummary:
    with _store_lock(root):
        _recover_pending_transaction(root)
        manifest = _read_manifest_unlocked(root)
        total_records = 0
        completed_partitions = 0
        observed_dates = []
        sync_times = []

        for manifest_key, entry in manifest.get("partitions", {}).items():
            if entry.get("status") != "complete":
                continue
            layer, period = manifest_key.rsplit("/", 1)
            year, month = (int(value) for value in period.split("-", 1))
            key = PartitionKey(layer, year, month)
            if not partition_path(root, key).exists():
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

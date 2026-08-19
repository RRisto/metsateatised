"""Discovery, storage, and notebook loading for processed forest notices."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq

from notice_store import PartitionKey, partition_path

IDENTITY_CANDIDATES = ("teatis_id", "teatise_id", "id", "dokumendi_id", "teatise_nr")


@dataclass(frozen=True, order=True)
class MonthKey:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise ValueError(f"invalid month: {self.month}")


@dataclass(frozen=True)
class RawMonth:
    key: MonthKey
    partition_keys: tuple[PartitionKey, ...]
    input_fingerprint: str


@dataclass(frozen=True)
class ProcessedMonthEntry:
    key: MonthKey
    notice_rows: int
    species_rows: int
    input_fingerprint: str
    model_version: str


def _parse_manifest_key(value: str) -> PartitionKey:
    try:
        layer, period = value.rsplit("/", 1)
        year_text, month_text = period.split("-", 1)
        key = PartitionKey(layer, int(year_text), int(month_text))
        MonthKey(key.year, key.month)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid raw manifest partition key: {value!r}") from error
    return key


def discover_raw_months(raw_root: Path) -> tuple[RawMonth, ...]:
    """Discover complete canonical raw inputs without mutating the raw store."""
    manifest_path = raw_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    grouped: dict[MonthKey, list[tuple[PartitionKey, dict]]] = {}

    for manifest_key, entry in manifest.get("partitions", {}).items():
        if entry.get("status") != "complete":
            continue
        key = _parse_manifest_key(manifest_key)
        if not partition_path(raw_root, key).is_file():
            continue
        grouped.setdefault(MonthKey(key.year, key.month), []).append((key, entry))

    months = []
    for month_key in sorted(grouped):
        selected = sorted(grouped[month_key], key=lambda item: item[0].layer)
        fingerprint_document = {
            f"{key.layer}/{key.year:04d}-{key.month:02d}": entry
            for key, entry in selected
        }
        serialized = json.dumps(
            fingerprint_document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        months.append(
            RawMonth(
                key=month_key,
                partition_keys=tuple(key for key, _entry in selected),
                input_fingerprint=sha256(serialized.encode("utf-8")).hexdigest(),
            )
        )
    return tuple(months)


def _to_wgs84(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if frame.crs is None:
        return frame.set_crs("EPSG:4326", allow_override=True)
    return frame.to_crs("EPSG:4326")


def _read_geoparquet(path: Path) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame.from_arrow(pq.ParquetFile(path).read())


def _identity_field(frame: gpd.GeoDataFrame) -> str | None:
    return next(
        (candidate for candidate in IDENTITY_CANDIDATES if candidate in frame.columns),
        None,
    )


def _deduplication_keys(frame: gpd.GeoDataFrame, identity_field: str | None) -> list[tuple]:
    geometries = frame.geometry.to_wkb().tolist()
    values = [None] * len(frame) if identity_field is None else frame[identity_field].tolist()

    keys = []
    for index, (value, geometry) in enumerate(zip(values, geometries, strict=True)):
        has_identity = identity_field is not None and not pd.isna(value)
        if has_identity and not (isinstance(value, str) and not value.strip()):
            keys.append(("identity", value))
        elif geometry is not None:
            keys.append(("geometry_wkb", geometry))
        else:
            keys.append(("row", index))
    return keys


def read_raw_month(raw_root: Path, raw_month: RawMonth) -> gpd.GeoDataFrame:
    """Read and deduplicate the raw layers belonging to one calendar month."""
    frames = [
        _to_wgs84(_read_geoparquet(partition_path(raw_root, key)))
        for key in raw_month.partition_keys
    ]
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    combined = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True, sort=False),
        geometry="geometry",
        crs="EPSG:4326",
    )
    temporary_key = "__processed_notice_identity__"
    while temporary_key in combined.columns:
        temporary_key = f"_{temporary_key}"
    combined[temporary_key] = _deduplication_keys(combined, _identity_field(combined))
    combined = combined.drop_duplicates(subset=temporary_key, keep="last")
    combined = combined.drop(columns=temporary_key).reset_index(drop=True)
    return gpd.GeoDataFrame(combined, geometry="geometry", crs="EPSG:4326")


def _month_label(key: MonthKey) -> str:
    return f"{key.year:04d}-{key.month:02d}"


def _processed_paths(root: Path, key: MonthKey) -> tuple[Path, Path]:
    directory = root / f"year={key.year:04d}" / f"month={key.month:02d}"
    return directory / "notices.parquet", directory / "species.parquet"


def _read_processed_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.exists():
        return {"format_version": 1, "last_updated_at": None, "months": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_fingerprint(frame: pd.DataFrame) -> str:
    pairs = sorted(f"{column}:{dtype}" for column, dtype in frame.dtypes.items())
    return sha256("\n".join(pairs).encode("utf-8")).hexdigest()


def _observed_bounds(frame: gpd.GeoDataFrame) -> tuple[str | None, str | None]:
    observed: list[date] = []
    if "_date_col" not in frame.columns:
        return None, None
    for _, row in frame.iterrows():
        column = row["_date_col"]
        if isinstance(column, str) and column in frame.columns:
            parsed = pd.to_datetime(row[column], utc=True, errors="coerce")
            if not pd.isna(parsed):
                observed.append(parsed.date())
    if not observed:
        return None, None
    return min(observed).isoformat(), max(observed).isoformat()


def is_month_current(root: Path, raw_month: RawMonth, model_version: str) -> bool:
    manifest = _read_processed_manifest(root)
    entry = manifest.get("months", {}).get(_month_label(raw_month.key), {})
    notices_path, species_path = _processed_paths(root, raw_month.key)
    return (
        entry.get("status") == "complete"
        and entry.get("input_fingerprint") == raw_month.input_fingerprint
        and entry.get("model_version") == model_version
        and notices_path.is_file()
        and species_path.is_file()
    )


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


def publish_processed_month(
    root: Path,
    key: MonthKey,
    notices: gpd.GeoDataFrame,
    species: pd.DataFrame,
    *,
    input_fingerprint: str,
    model_version: str,
    now: datetime | None = None,
) -> ProcessedMonthEntry:
    """Atomically publish one verified notice/species artifact pair and its manifest."""
    root = root.resolve()
    notices_path, species_path = _processed_paths(root, key)
    notices_path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    temporary_notices = notices_path.with_name(f"notices-{token}.parquet")
    temporary_species = species_path.with_name(f"species-{token}.parquet")
    backup_notices = notices_path.with_name(f"notices-{token}.previous.parquet")
    backup_species = species_path.with_name(f"species-{token}.previous.parquet")
    temporary_manifest: Path | None = None
    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)

    try:
        notices.to_parquet(temporary_notices)
        species.to_parquet(temporary_species)
        verified_notices = _read_geoparquet(temporary_notices)
        verified_species = pq.ParquetFile(temporary_species).read().to_pandas()
        if len(verified_notices) != len(notices) or not verified_notices.crs.equals("EPSG:4326"):
            raise ValueError("processed notice artifact validation failed")
        if len(verified_species) != len(species):
            raise ValueError("processed species artifact validation failed")

        observed_start, observed_end = _observed_bounds(notices)
        manifest = _read_processed_manifest(root)
        manifest.setdefault("format_version", 1)
        manifest.setdefault("months", {})
        manifest["last_updated_at"] = timestamp.isoformat()
        manifest["months"][_month_label(key)] = {
            "status": "complete",
            "input_fingerprint": input_fingerprint,
            "model_version": model_version,
            "notice_rows": len(notices),
            "species_rows": len(species),
            "observed_start": observed_start,
            "observed_end": observed_end,
            "notice_schema_fingerprint": _schema_fingerprint(notices),
            "species_schema_fingerprint": _schema_fingerprint(species),
            "updated_at": timestamp.isoformat(),
            "notices_path": notices_path.relative_to(root).as_posix(),
            "species_path": species_path.relative_to(root).as_posix(),
        }
        temporary_manifest = _prepare_manifest(root, manifest)

        if notices_path.exists():
            notices_path.replace(backup_notices)
        if species_path.exists():
            species_path.replace(backup_species)
        try:
            temporary_notices.replace(notices_path)
            temporary_species.replace(species_path)
            temporary_manifest.replace(root / "manifest.json")
        except BaseException:
            notices_path.unlink(missing_ok=True)
            species_path.unlink(missing_ok=True)
            if backup_notices.exists():
                backup_notices.replace(notices_path)
            if backup_species.exists():
                backup_species.replace(species_path)
            raise
    finally:
        for path in (
            temporary_notices,
            temporary_species,
            backup_notices,
            backup_species,
            temporary_manifest,
        ):
            if path is not None:
                path.unlink(missing_ok=True)

    return ProcessedMonthEntry(
        key=key,
        notice_rows=len(notices),
        species_rows=len(species),
        input_fingerprint=input_fingerprint,
        model_version=model_version,
    )


def _selected_entries(root: Path, start: MonthKey | None, end: MonthKey | None) -> list[dict]:
    selected = []
    for label, entry in sorted(_read_processed_manifest(root).get("months", {}).items()):
        year_text, month_text = label.split("-", 1)
        key = MonthKey(int(year_text), int(month_text))
        if entry.get("status") != "complete":
            continue
        if start is not None and key < start:
            continue
        if end is not None and key > end:
            continue
        selected.append(entry)
    return selected


def load_processed_notices(
    root: Path,
    start: MonthKey | None = None,
    end: MonthKey | None = None,
    columns: Sequence[str] | None = None,
) -> gpd.GeoDataFrame:
    frames = [
        gpd.GeoDataFrame.from_arrow(
            pq.ParquetFile(root / entry["notices_path"]).read(columns=columns)
        )
        for entry in _selected_entries(root, start, end)
    ]
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True, sort=False),
        geometry="geometry",
        crs="EPSG:4326",
    )


def load_processed_species(
    root: Path,
    start: MonthKey | None = None,
    end: MonthKey | None = None,
    columns: Sequence[str] | None = None,
) -> pd.DataFrame:
    frames = [
        pq.ParquetFile(root / entry["species_path"]).read(columns=columns).to_pandas()
        for entry in _selected_entries(root, start, end)
    ]
    return (
        pd.concat(frames, ignore_index=True, sort=False)
        if frames
        else pd.DataFrame(columns=columns)
    )

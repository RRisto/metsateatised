import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from notice_store import PartitionKey, upsert_partition
from processed_notice_store import (
    MonthKey,
    RawMonth,
    discover_raw_months,
    is_month_current,
    load_processed_notices,
    load_processed_species,
    publish_processed_month,
    read_raw_month,
)


def _frame(rows):
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def _raw_store(root: Path) -> Path:
    upsert_partition(
        root,
        PartitionKey("archive_notices", 2025, 1),
        _frame(
            [
                {"teatis_id": 1, "source": "archive", "geometry": Point(24.0, 59.0)},
                {"teatis_id": 2, "source": "archive", "geometry": Point(25.0, 58.0)},
            ]
        ),
        identity_candidates=["teatis_id"],
    )
    upsert_partition(
        root,
        PartitionKey("current_notices", 2025, 1),
        _frame(
            [
                {"teatis_id": 2, "source": "current", "geometry": Point(25.0, 58.0)},
                {"teatis_id": 3, "source": "current", "geometry": Point(26.0, 57.0)},
            ]
        ),
        identity_candidates=["teatis_id"],
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["partitions"]["archive_notices/2025-02"] = {
        "status": "incomplete",
        "record_count": 1,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_discover_raw_months_groups_layers_and_ignores_incomplete_entries(tmp_path: Path):
    raw_root = _raw_store(tmp_path)

    months = discover_raw_months(raw_root)

    assert [month.key for month in months] == [MonthKey(2025, 1)]
    assert [key.layer for key in months[0].partition_keys] == [
        "archive_notices",
        "current_notices",
    ]
    assert len(months[0].input_fingerprint) == 64


def test_read_raw_month_combines_layers_and_prefers_current_identity(tmp_path: Path):
    raw_root = _raw_store(tmp_path)
    before = {
        path.relative_to(raw_root): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in raw_root.rglob("*")
        if path.is_file()
    }

    frame = read_raw_month(raw_root, discover_raw_months(raw_root)[0])

    assert frame["teatis_id"].tolist() == [1, 2, 3]
    assert frame.set_index("teatis_id").loc[2, "source"] == "current"
    assert frame.crs.equals("EPSG:4326")
    after = {
        path.relative_to(raw_root): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in raw_root.rglob("*")
        if path.is_file()
    }
    assert after == before


def _calculated_outputs():
    notices = _frame(
        [
            {
                "teatis_id": 1,
                "area_ha": 2.5,
                "standing_live_biomass_tco2": 30.0,
                "geometry": Point(24.0, 59.0),
            }
        ]
    )
    species = pd.DataFrame(
        [{"teatis_id": 1, "species_code": "KS", "biomass_tco2": 30.0}]
    )
    return notices, species


def test_publish_processed_month_writes_outputs_and_current_manifest(tmp_path: Path):
    notices, species = _calculated_outputs()

    entry = publish_processed_month(
        tmp_path,
        MonthKey(2025, 1),
        notices,
        species,
        input_fingerprint="raw-v1",
        model_version="model-v1",
    )

    raw_month = RawMonth(MonthKey(2025, 1), (), "raw-v1")
    assert entry.notice_rows == 1
    assert entry.species_rows == 1
    assert is_month_current(tmp_path, raw_month, "model-v1") is True
    assert is_month_current(tmp_path, raw_month, "model-v2") is False
    assert is_month_current(
        tmp_path, RawMonth(MonthKey(2025, 1), (), "raw-v2"), "model-v1"
    ) is False


def test_failed_species_write_preserves_previous_outputs_and_manifest(
    tmp_path: Path, monkeypatch
):
    notices, species = _calculated_outputs()
    publish_processed_month(
        tmp_path,
        MonthKey(2025, 1),
        notices,
        species,
        input_fingerprint="raw-v1",
        model_version="model-v1",
    )
    tracked = sorted(path for path in tmp_path.rglob("*") if path.is_file())
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tracked}

    def fail_species_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_species_write)
    with pytest.raises(OSError, match="disk full"):
        publish_processed_month(
            tmp_path,
            MonthKey(2025, 1),
            notices,
            species,
            input_fingerprint="raw-v2",
            model_version="model-v1",
        )

    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_notebook_loaders_project_columns_from_complete_months(tmp_path: Path):
    notices, species = _calculated_outputs()
    publish_processed_month(
        tmp_path,
        MonthKey(2025, 1),
        notices,
        species,
        input_fingerprint="raw-v1",
        model_version="model-v1",
    )

    loaded_notices = load_processed_notices(
        tmp_path, columns=["teatis_id", "geometry"]
    )
    loaded_species = load_processed_species(
        tmp_path, columns=["teatis_id", "species_code", "biomass_tco2"]
    )

    assert list(loaded_notices.columns) == ["teatis_id", "geometry"]
    assert list(loaded_species.columns) == ["teatis_id", "species_code", "biomass_tco2"]

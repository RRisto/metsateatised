from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from notice_preprocessing import preprocess_notices
from notice_store import PartitionKey, upsert_partition
from processed_notice_store import MonthKey, load_processed_notices


def _raw_month(root: Path, year: int, month: int, notice_id: int) -> None:
    frame = gpd.GeoDataFrame(
        [{"teatis_id": notice_id, "geometry": Point(24.0, 59.0)}],
        geometry="geometry",
        crs="EPSG:4326",
    )
    upsert_partition(
        root,
        PartitionKey("archive_notices", year, month),
        frame,
        identity_candidates=["teatis_id"],
    )


def test_preprocess_notices_processes_each_month_once_and_resumes(tmp_path: Path):
    raw_root = tmp_path / "raw"
    processed_root = tmp_path / "processed"
    _raw_month(raw_root, 2025, 1, 1)
    calls = []

    def resolve(notices, **_kwargs):
        calls.append(("stands", tuple(notices["teatis_id"])))
        return gpd.GeoDataFrame(
            [{"id": 7, "geometry": Point(24.0, 59.0)}],
            geometry="geometry",
            crs="EPSG:4326",
        )

    def calculate(notices, _stands, **_kwargs):
        calls.append(("calculate", tuple(notices["teatis_id"])))
        results = notices.copy()
        results["standing_live_biomass_tco2"] = 30.0
        species = pd.DataFrame(
            [{"teatis_id": 1, "species_code": "KS", "biomass_tco2": 30.0}]
        )
        return results, species

    first = preprocess_notices(
        raw_root,
        processed_root,
        model_version="model-v1",
        resolve_stands=resolve,
        calculate=calculate,
    )
    second = preprocess_notices(
        raw_root,
        processed_root,
        model_version="model-v1",
        resolve_stands=resolve,
        calculate=calculate,
    )

    assert first.completed == (MonthKey(2025, 1),)
    assert second.skipped == (MonthKey(2025, 1),)
    assert calls == [("stands", (1,)), ("calculate", (1,))]
    assert len(load_processed_notices(processed_root)) == 1


def test_preprocess_notices_continues_after_a_failed_month(tmp_path: Path):
    raw_root = tmp_path / "raw"
    processed_root = tmp_path / "processed"
    _raw_month(raw_root, 2025, 1, 1)
    _raw_month(raw_root, 2025, 2, 2)

    def calculate(notices, _stands, **_kwargs):
        notice_id = int(notices.iloc[0]["teatis_id"])
        if notice_id == 1:
            raise RuntimeError("detail service unavailable")
        return notices, pd.DataFrame([{"teatis_id": notice_id}])

    result = preprocess_notices(
        raw_root,
        processed_root,
        model_version="model-v1",
        resolve_stands=lambda notices, **_kwargs: notices,
        calculate=calculate,
    )

    assert result.completed == (MonthKey(2025, 2),)
    assert result.failed[0].month == MonthKey(2025, 1)
    assert result.failed[0].message == "detail service unavailable"

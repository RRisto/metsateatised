from datetime import date, timedelta

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

import app
from carbon import VolumeBasis
from stand_model import aggregate_increment, classify_spatial_coverage


def test_increment_is_scaled_to_overlap_and_area_weighted():
    """Ignoring overlap area would misstate the notice's current annual increment."""
    rows = [
        {"overlap_ha": 1.0, "increment_m3_ha_y": 4.0},
        {"overlap_ha": 3.0, "increment_m3_ha_y": 8.0},
    ]

    result = aggregate_increment(rows)

    assert result.current_increment_on_overlap_m3_y == 28.0
    assert result.current_increment_m3_ha_y == 7.0


def test_increment_aggregation_does_not_treat_missing_rates_as_zero():
    """Missing inventory increment must remain unknown rather than become a zero-growth claim."""
    result = aggregate_increment([{"overlap_ha": 1.0, "increment_m3_ha_y": None}])

    assert result.current_increment_on_overlap_m3_y is None
    assert result.current_increment_m3_ha_y is None


def test_increment_aggregation_marks_partial_overlap_as_incomplete():
    """Dropping unknown increment area would present a partial rate as a complete notice result."""
    result = aggregate_increment(
        [
            {"overlap_ha": 1.0, "increment_m3_ha_y": 4.0},
            {"overlap_ha": 3.0, "increment_m3_ha_y": None},
        ]
    )

    assert result.current_increment_on_overlap_m3_y is None
    assert result.current_increment_m3_ha_y is None
    assert result.current_increment_coverage_pct == 25.0
    assert result.current_increment_is_complete is False


def test_spatial_coverage_uses_existing_thresholds_without_overall_score():
    """Changing the 90/50 percent bands would alter the coverage dimension's meaning."""
    assert classify_spatial_coverage(90.0) == "hea"
    assert classify_spatial_coverage(50.0) == "osaline"
    assert classify_spatial_coverage(49.9) == "nõrk"


def _notices_and_stands(*, stand_rows, stand_geometries):
    notice = gpd.GeoDataFrame(
        {"teatis_id": [1]},
        geometry=[box(500000, 6500000, 500200, 6500200)],
        crs="EPSG:3301",
    )
    stands = gpd.GeoDataFrame(stand_rows, geometry=stand_geometries, crs="EPSG:3301")
    return notice, stands


def _stand_row(stand_id, inventory_date, increment):
    return {
        "id": stand_id,
        "invent_kp": inventory_date.isoformat(),
        "pindala": 2.0,
        "tagavara_1_ha": 30.0,
        "juurdekasv": increment,
    }


def test_analyze_exposes_independent_quality_and_complete_increment_metrics(monkeypatch):
    """Omitting any requested analysis field would hide a distinct inventory limitation."""
    today = date.today()
    notices, stands = _notices_and_stands(
        stand_rows=[_stand_row(1, today - timedelta(days=365), 4.0)],
        stand_geometries=[box(500000, 6500000, 500100, 6500200)],
    )
    monkeypatch.setattr(
        app,
        "fetch_stand_details",
        lambda *_args, **_kwargs: [
            {
                "_stand_id": 1,
                "elemendid": [
                    {
                        "puuliigiKood": "KS",
                        "osakaal": 100,
                        "tagavara": 20,
                        "vanus": 40,
                        "jooksevVanus": 45,
                    }
                ],
            }
        ],
    )

    result, _ = app.analyze(notices, stands)
    row = result.iloc[0]

    assert {
        "spatial_coverage_pct",
        "spatial_coverage_quality",
        "inventory_age_years",
        "inventory_recency",
        "volume_source_quality",
        "current_increment_m3_ha_y",
        "current_increment_on_overlap_m3_y",
        "mean_current_age_years",
    } <= set(result.columns)
    assert row.spatial_coverage_pct == pytest.approx(50.0)
    assert row.spatial_coverage_quality == "osaline"
    assert row.inventory_recency == "väga hea"
    assert row.volume_source_quality == VolumeBasis.DETAIL_SPECIES_STOCK.value
    assert row.current_increment_m3_ha_y == pytest.approx(4.0)
    assert row.current_increment_on_overlap_m3_y == pytest.approx(8.0)
    assert row.current_increment_coverage_pct == pytest.approx(100.0)
    assert bool(row.current_increment_is_complete) is True
    assert row.mean_age == pytest.approx(40.0)
    assert row.mean_current_age_years == pytest.approx(45.0)


def test_analyze_uses_oldest_inventory_for_age_and_marks_partial_increment(monkeypatch):
    """Mixing an oldest date with an averaged age would make the notice timing contradictory."""
    today = date.today()
    oldest_date = today - timedelta(days=365 * 4)
    newest_date = today - timedelta(days=365)
    notices, stands = _notices_and_stands(
        stand_rows=[
            _stand_row(1, newest_date, 4.0),
            _stand_row(2, oldest_date, None),
        ],
        stand_geometries=[
            box(500000, 6500000, 500100, 6500200),
            box(500100, 6500000, 500200, 6500200),
        ],
    )
    monkeypatch.setattr(
        app,
        "fetch_stand_details",
        lambda *_args, **_kwargs: [
            {
                "_stand_id": 1,
                "elemendid": [
                    {
                        "puuliigiKood": "KS",
                        "osakaal": 100,
                        "tagavara": 20,
                        "jooksevVanus": 50,
                    }
                ],
            },
            {
                "_stand_id": 2,
                "elemendid": [{"puuliigiKood": "KU", "osakaal": 100, "jooksevVanus": 80}],
            },
        ],
    )

    result, _ = app.analyze(notices, stands)
    row = result.iloc[0]

    assert row.inventory_date == oldest_date
    assert row.inventory_age_years == pytest.approx((today - oldest_date).days / 365.25)
    assert row.inventory_recency == "hea"
    assert row.volume_source_quality == " + ".join(
        [
            VolumeBasis.DETAIL_SPECIES_STOCK.value,
            VolumeBasis.WFS_STAND_STOCK_ALLOCATED.value,
        ]
    )
    assert pd.isna(row.current_increment_on_overlap_m3_y)
    assert pd.isna(row.current_increment_m3_ha_y)
    assert row.current_increment_coverage_pct == pytest.approx(50.0)
    assert bool(row.current_increment_is_complete) is False
    assert row.mean_current_age_years == pytest.approx(68.0)


def test_analyze_no_intersection_exposes_unknown_inventory_dimensions():
    """A non-intersection must not receive fabricated inventory or increment values."""
    today = date.today()
    notices, stands = _notices_and_stands(
        stand_rows=[_stand_row(1, today - timedelta(days=365), 4.0)],
        stand_geometries=[box(501000, 6501000, 501100, 6501200)],
    )

    result, _ = app.analyze(notices, stands)
    row = result.iloc[0]

    assert row.spatial_coverage_pct == 0.0
    assert row.spatial_coverage_quality == "nõrk"
    assert row.inventory_recency == "teadmata"
    assert row.volume_source_quality == VolumeBasis.UNKNOWN.value
    assert pd.isna(row.mean_age)
    assert pd.isna(row.mean_current_age_years)
    assert pd.isna(row.current_increment_m3_ha_y)
    assert pd.isna(row.current_increment_on_overlap_m3_y)

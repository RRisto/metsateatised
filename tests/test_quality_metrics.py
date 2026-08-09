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
    assert row.volume_source_quality == VolumeBasis.WFS_STAND_STOCK_ALLOCATED.value
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
    assert row.volume_source_quality == VolumeBasis.WFS_STAND_STOCK_ALLOCATED.value
    assert pd.isna(row.current_increment_on_overlap_m3_y)
    assert pd.isna(row.current_increment_m3_ha_y)
    assert row.current_increment_coverage_pct == pytest.approx(50.0)
    assert bool(row.current_increment_is_complete) is False
    assert row.mean_current_age_years == pytest.approx(65.0)


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


def test_analyze_ignores_boundary_only_intersections_before_detail_fetch(monkeypatch):
    """A line-only touch must follow the no-intersection path and fetch no stand detail."""
    notices, stands = _notices_and_stands(
        stand_rows=[_stand_row(2, date(2020, 1, 1), 9.0)],
        stand_geometries=[box(500200, 6500000, 500300, 6500200)],
    )

    def unexpected_fetch(*_args, **_kwargs):
        pytest.fail("boundary-only stand detail must not be fetched")

    monkeypatch.setattr(app, "fetch_stand_details", unexpected_fetch)

    result, species = app.analyze(notices, stands)
    row = result.iloc[0]

    assert species.empty
    assert row.spatial_coverage_pct == 0.0
    assert row.inventory_date is None
    assert row.inventory_recency == "teadmata"


def test_analyze_excludes_boundary_neighbour_from_mixed_inventory_metrics(monkeypatch):
    """A zero-area neighbour must not contaminate a real overlap's dates or completeness."""
    overlap_date = date(2025, 1, 1)
    notices, stands = _notices_and_stands(
        stand_rows=[
            _stand_row(1, overlap_date, 4.0),
            _stand_row(2, date(2010, 1, 1), None),
        ],
        stand_geometries=[
            box(500000, 6500000, 500100, 6500200),
            box(500200, 6500000, 500300, 6500200),
        ],
    )
    fetched_ids = []

    def fetch_details(stand_ids, **_kwargs):
        fetched_ids.extend(stand_ids)
        return [
            {
                "_stand_id": 1,
                "elemendid": [
                    {
                        "id": 11,
                        "rindeKood": "1",
                        "puuliigiKood": "KS",
                        "osakaal": 100,
                        "tagavara": 30,
                    }
                ],
            }
        ]

    monkeypatch.setattr(app, "fetch_stand_details", fetch_details)

    result, _ = app.analyze(notices, stands)
    row = result.iloc[0]

    assert fetched_ids == [1]
    assert row.inventory_date == overlap_date
    assert row.current_increment_m3_ha_y == 4.0
    assert row.current_increment_coverage_pct == 100.0


def test_analyze_weights_duplicate_species_ages_by_source_record(monkeypatch):
    """Joining ages by species code would assign the second-layer KU age to both KU rows."""
    notices, stands = _notices_and_stands(
        stand_rows=[
            {
                **_stand_row(6522173, date(2012, 1, 1), 3.0),
                "tagavara_1_ha": 228,
                "tagavara_2_ha": 16,
                "tagavara_y_ha": 0,
            }
        ],
        stand_geometries=[box(500000, 6500000, 500200, 6500200)],
    )
    detail_rows = [
        (20048903, "1", "KU", 54, 123, 178, 191),
        (20048901, "1", "MA", 31, 71, 178, 191),
        (20048905, "1", "KS", 11, 25, 148, 161),
        (20048899, "1", "HB", 4, 9, 148, 161),
        (20048897, "2", "KU", 100, 16, 81, 94),
    ]
    monkeypatch.setattr(
        app,
        "fetch_stand_details",
        lambda *_args, **_kwargs: [
            {
                "_stand_id": 6522173,
                "elemendid": [
                    {
                        "id": record_id,
                        "eraldisId": 6522173,
                        "rindeKood": stratum,
                        "puuliigiKood": species,
                        "osakaal": share,
                        "tagavara": stock,
                        "vanus": inventory_age,
                        "jooksevVanus": current_age,
                    }
                    for (
                        record_id,
                        stratum,
                        species,
                        share,
                        stock,
                        inventory_age,
                        current_age,
                    ) in detail_rows
                ],
            }
        ],
    )

    result, species = app.analyze(notices, stands)
    row = result.iloc[0]

    assert row.mean_age == pytest.approx(
        (123 * 178 + 71 * 178 + 25 * 148 + 9 * 148 + 16 * 81) / 244
    )
    assert row.mean_current_age_years == pytest.approx(
        (123 * 191 + 71 * 191 + 25 * 161 + 9 * 161 + 16 * 94) / 244
    )
    ku_rows = species[(species["estimate_scope"] == "standing") & (species["species_code"] == "KU")]
    assert list(ku_rows["age"]) == [178, 81]

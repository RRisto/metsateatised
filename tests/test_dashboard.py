from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import folium
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon
from streamlit.testing.v1 import AppTest

from notice_store import StoreSummary
from notice_sync import SyncProgress, SyncResult


def dashboard_result_fixture():
    return gpd.GeoDataFrame(
        {
            "teatis_id": [3973677.0],
            "raiutav_maht": [100.0],
            "area_ha": [2.4],
            "standing_live_biomass_tco2": [300.0],
            "standing_live_biomass_tco2_ha": [125.0],
            "planned_harvest_biomass_tco2": [120.0],
            "standing_volume_basis": ["eraldise tagavara + liigiosakaal"],
            "planned_harvest_volume_basis": ["raiemahu põhine hinnang"],
            "dominant_species": ["Kask"],
            "mean_age": [45.0],
            "mean_current_age_years": [50.0],
            "spatial_coverage_pct": [95.0],
            "spatial_coverage_quality": ["hea"],
            "volume_source_quality": ["eraldise tagavara + liigiosakaal"],
            "inventory_date": ["2021-07-29"],
            "inventory_age_years": [5.03],
            "inventory_recency": ["hea"],
            "current_increment_m3_ha_y": [7.4],
            "current_increment_on_overlap_m3_y": [17.8],
            "current_increment_covered_area_ha": [2.4],
            "current_increment_coverage_pct": [100.0],
            "current_increment_is_complete": [True],
            "standing_biomass_is_complete": [True],
            "planned_harvest_biomass_is_complete": [True],
        },
        geometry=[Polygon([(24.47, 58.75), (24.48, 58.75), (24.48, 58.76)])],
        crs="EPSG:4326",
    )


def _rendered_text(app):
    element_groups = (app.caption, app.info, app.markdown, app.subheader)
    return "\n".join(str(element.value) for group in element_groups for element in group)


def raw_sync_sample_feature():
    return {
        "type": "Feature",
        "properties": {"kuupaev": "2026-08-01"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[24.47, 58.75], [24.48, 58.75], [24.48, 58.76], [24.47, 58.75]]],
        },
    }


def test_empty_store_sync_start_is_ten_calendar_years_earlier_on_leap_day():
    """Fixed-day subtraction picks 2014-03-01 instead of the required calendar date."""
    import app as dashboard_app

    assert dashboard_app.default_notice_sync_start(date(2024, 2, 29), None) == date(2014, 2, 28)


def test_notice_sync_progress_pulse_never_claims_completion():
    """A full progress bar falsely implies a partition completed when its page total is unknown."""
    import app as dashboard_app

    assert 0.0 < dashboard_app.notice_sync_progress_pulse(11) < 1.0


def test_raw_sync_renders_the_updated_store_summary():
    """Retaining the initial summary after a sync shows stale coverage and record totals."""
    initial_summary = StoreSummary(0, 0, None, None, None)
    updated_summary = StoreSummary(
        7,
        2,
        date(2026, 1, 1),
        date(2026, 1, 31),
        datetime(2026, 2, 1, tzinfo=UTC),
    )
    with (
        patch(
            "notice_store.summarize_store",
            side_effect=[initial_summary, initial_summary, updated_summary],
        ) as summarize,
        patch("notice_sync.synchronize_notices", return_value=SyncResult(2, 0, (), 7)),
        patch("wfs.fetch_wfs_features", return_value=[raw_sync_sample_feature()]),
    ):
        app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
        app.run(timeout=20)
        next(item for item in app.button if item.label == "Laadi/uuenda metsateatised").click()
        app.run(timeout=20)

    assert summarize.call_count == 3
    assert "Salvestatud kirjeid: 7" in _rendered_text(app)
    assert "Valmis kuupartitsioone: 2" in _rendered_text(app)


def test_raw_sync_failure_clears_progress_and_status_containers():
    """A service failure must not leave the transient processing UI visible."""

    def fail_after_progress(*args, **kwargs):
        kwargs["progress"](
            SyncProgress("archive_notices", date(2026, 1, 1), 1, 10, 10)
        )
        raise RuntimeError("test synchronization failure")

    with (
        patch("notice_sync.synchronize_notices", side_effect=fail_after_progress),
        patch("wfs.fetch_wfs_features", return_value=[raw_sync_sample_feature()]),
    ):
        app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
        app.run(timeout=20)
        next(item for item in app.button if item.label == "Laadi/uuenda metsateatised").click()
        app.run(timeout=20)

    assert not app.get("progress")
    assert "Töötlen archive_notices kihti" not in _rendered_text(app)
    assert not app.exception
    assert any("test synchronization failure" in error.value for error in app.error)


def test_dashboard_exposes_raw_notice_synchronization_controls():
    """Removing the raw-data controls would leave store synchronization inaccessible."""
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")

    app.run(timeout=20)

    assert not app.exception
    assert any(item.label == "Sünkroonimise algus" for item in app.date_input)
    assert any(item.label == "Sünkroonimise lõpp" for item in app.date_input)
    assert any(
        item.label == "Uuenda ka juba laaditud kattuvaid kuid" for item in app.checkbox
    )
    assert any(item.label == "Laadi/uuenda metsateatised" for item in app.button)


def test_raw_notice_synchronization_does_not_populate_analysis_results():
    """Calling raw synchronization must not enter the load-and-analyze workflow."""
    sample_feature = {
        "type": "Feature",
        "properties": {"kuupaev": "2026-08-01"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[24.47, 58.75], [24.48, 58.75], [24.48, 58.76], [24.47, 58.75]]],
        },
    }
    with (
        patch("notice_sync.synchronize_notices", return_value=SyncResult(0, 0, (), 0)) as sync,
        patch("wfs.fetch_wfs_features", return_value=[sample_feature]),
    ):
        app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
        app.run(timeout=20)

        next(
            item for item in app.button if item.label == "Laadi/uuenda metsateatised"
        ).click()
        app.run(timeout=20)

    sync.assert_called_once()
    assert "results" not in app.session_state


def test_distinct_carbon_and_inventory_metrics_reach_streamlit_dashboard():
    """Reading any legacy aggregate would crash against the new result contract."""
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.session_state["results"] = dashboard_result_fixture()
    app.session_state["species_df"] = pd.DataFrame(
        {
            "estimate_scope": ["standing", "planned_harvest"],
            "species": ["Kask", "Kask"],
            "volume_m3": [250.0, 100.0],
            "biomass_tco2": [300.0, 120.0],
        }
    )

    app.run(timeout=20)

    assert not app.exception
    assert app.metric[2].label == "Elusbiomassi süsinikuvaru"
    assert app.metric[2].value == "300 t CO₂e"
    assert app.metric[3].label == "Kavandatava raiemahu biomass"
    assert app.metric[3].value == "120 t CO₂e"

    rendered_text = _rendered_text(app)
    assert "Elusbiomassi süsinikuvaru ja kavandatava raiemahu biomass teatise kaupa" in [
        subheader.value for subheader in app.subheader
    ]
    assert any(
        "Elusbiomassi süsinikuvaru ja kavandatava raiemahu biomass "
        "ei ole heite ega kliimamõju hinnangud." in caption.value
        for caption in app.caption
    )
    assert all(
        "mitte veel täielik lageraie kliimamõju" not in caption.value for caption in app.caption
    )
    assert "eraldise tagavara + liigiosakaal" in rendered_text
    assert "raiemahu põhine hinnang" in rendered_text
    assert "2021-07-29" in rendered_text
    assert "hea" in rendered_text
    assert "7.4 m³/ha/a" in rendered_text
    assert "17.8 m³/a" in rendered_text
    assert "See ei ole heite hinnang." in rendered_text

    summary = app.dataframe[0].value
    assert summary.loc[0, "mean_inventory_age_years"] == pytest.approx(45.0)
    assert summary.loc[0, "mean_current_age_years"] == pytest.approx(50.0)
    assert summary.loc[0, "standing_live_biomass_tco2"] == pytest.approx(300.0)
    assert summary.loc[0, "planned_harvest_biomass_tco2"] == pytest.approx(120.0)


def test_export_schema_is_explicit_and_excludes_legacy_aggregates():
    """Selecting all result columns would leak stale ambiguous fields from cached results."""
    import app as dashboard_app

    results = dashboard_result_fixture()
    results["carbon_co2e_t"] = 999.0
    results["carbon_t_per_ha"] = 999.0
    results["calculation_basis"] = "legacy"
    results["data_quality"] = "legacy"

    exported = dashboard_app.build_export_table(results)

    assert list(exported.columns) == [
        "teatis_id",
        "raiutav_maht",
        "area_ha",
        "dominant_species",
        "mean_inventory_age_years",
        "mean_current_age_years",
        "standing_live_biomass_tco2",
        "standing_live_biomass_tco2_ha",
        "planned_harvest_biomass_tco2",
        "standing_volume_basis",
        "planned_harvest_volume_basis",
        "standing_biomass_is_complete",
        "planned_harvest_biomass_is_complete",
        "inventory_date",
        "inventory_age_years",
        "inventory_recency",
        "spatial_coverage_pct",
        "spatial_coverage_quality",
        "volume_source_quality",
        "current_increment_m3_ha_y",
        "current_increment_on_overlap_m3_y",
        "current_increment_covered_area_ha",
        "current_increment_coverage_pct",
        "current_increment_is_complete",
    ]
    assert exported.loc[0, "mean_inventory_age_years"] == pytest.approx(45.0)
    assert exported.loc[0, "mean_current_age_years"] == pytest.approx(50.0)


def test_map_popup_keeps_biomass_quantities_and_sources_separate():
    """Dropping either estimate or provenance would make map interpretation ambiguous."""
    import app as dashboard_app

    rendered_map = dashboard_app.make_map(dashboard_result_fixture())
    geojson_layer = next(
        child for child in rendered_map._children.values() if isinstance(child, folium.GeoJson)
    )
    popup = geojson_layer.data["features"][0]["properties"]["_popup"]

    assert "Elusbiomassi süsinikuvaru: 300 t CO₂e" in popup
    assert "Kavandatava raiemahu biomass: 120 t CO₂e" in popup
    assert "Elusbiomassi mahu alus: eraldise tagavara + liigiosakaal" in popup
    assert "Kavandatava raiemahu alus: raiemahu põhine hinnang" in popup


def test_cutting_type_map_uses_categorical_colors_and_legend():
    """Using carbon thresholds in cutting-type mode would misrepresent the selected dimension."""
    import app as dashboard_app

    results = gpd.GeoDataFrame(
        pd.concat(
            [dashboard_result_fixture(), dashboard_result_fixture(), dashboard_result_fixture()],
            ignore_index=True,
        ),
        geometry="geometry",
        crs="EPSG:4326",
    )
    results["raie_liik"] = ["Harvendusraie", "Lageraie", None]

    rendered_map = dashboard_app.make_map(results, color_mode="Raieliik").get_root().render()

    assert "Raieliik" in rendered_map
    assert "Harvendusraie" in rendered_map
    assert "Lageraie" in rendered_map
    assert "Puudub / teadmata" in rendered_map
    assert "#1f78b4" in rendered_map
    assert "#33a02c" in rendered_map
    assert "#777777" in rendered_map


def test_dashboard_offers_map_color_mode_selector():
    """Without a mode selector users could not request cutting-type coloring."""
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.session_state["results"] = dashboard_result_fixture()
    app.session_state["species_df"] = pd.DataFrame()

    app.run(timeout=20)

    assert not app.exception
    selector = next(selectbox for selectbox in app.selectbox if selectbox.label == "Kaardi värv")
    assert selector.options == ["Süsinikuvaru", "Raieliik"]


def test_dashboard_offers_forced_recalculation_control():
    """Without an override users could not refresh one persisted analysis on demand."""
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")

    app.run(timeout=20)

    assert not app.exception
    assert any(
        checkbox.label == "Arvuta uuesti (eirab salvestatud tulemust)"
        for checkbox in app.checkbox
    )


def test_dashboard_allows_up_to_100000_notices_per_layer():
    """A 50,000-record widget cap prevents selecting the requested ten-year volume."""
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")

    app.run(timeout=20)

    assert not app.exception
    limit = next(
        item for item in app.number_input if item.label == "Maksimaalne kirjete arv kihist"
    )
    assert limit.max == 100_000


def test_map_batches_all_polygons_into_one_geojson_layer():
    """Creating one Leaflet layer per polygon makes large result maps slow to serialize."""
    import app as dashboard_app

    results = gpd.GeoDataFrame(
        pd.concat([dashboard_result_fixture(), dashboard_result_fixture()], ignore_index=True),
        geometry="geometry",
        crs="EPSG:4326",
    )
    results["teatis_id"] = [3973677.0, 3973678.0]

    rendered_map = dashboard_app.make_map(results)
    geojson_layers = [
        child for child in rendered_map._children.values() if isinstance(child, folium.GeoJson)
    ]

    assert len(geojson_layers) == 1
    rendered_html = rendered_map.get_root().render()
    assert "3973677" in rendered_html
    assert "3973678" in rendered_html


def test_dashboard_increment_caption_uses_area_weighted_rate():
    """Averaging notice-level rates equally would contradict the displayed total increment."""
    results = gpd.GeoDataFrame(
        pd.concat([dashboard_result_fixture(), dashboard_result_fixture()], ignore_index=True),
        geometry="geometry",
        crs="EPSG:4326",
    )
    results["teatis_id"] = [1, 2]
    results["area_ha"] = [1.0, 3.0]
    results["current_increment_m3_ha_y"] = [4.0, 8.0]
    results["current_increment_on_overlap_m3_y"] = [4.0, 24.0]
    results["current_increment_covered_area_ha"] = [1.0, 3.0]

    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.session_state["results"] = results
    app.session_state["species_df"] = pd.DataFrame()

    app.run(timeout=20)

    assert not app.exception
    rendered_text = _rendered_text(app)
    assert "7.0 m³/ha/a" in rendered_text
    assert "28.0 m³/a" in rendered_text

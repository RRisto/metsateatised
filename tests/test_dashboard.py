from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon
from streamlit.testing.v1 import AppTest


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
            "current_increment_coverage_pct": [100.0],
            "current_increment_is_complete": [True],
        },
        geometry=[Polygon([(24.47, 58.75), (24.48, 58.75), (24.48, 58.76)])],
        crs="EPSG:4326",
    )


def _rendered_text(app):
    element_groups = (app.caption, app.info, app.markdown, app.subheader)
    return "\n".join(str(element.value) for group in element_groups for element in group)


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
        "inventory_date",
        "inventory_age_years",
        "inventory_recency",
        "spatial_coverage_pct",
        "spatial_coverage_quality",
        "volume_source_quality",
        "current_increment_m3_ha_y",
        "current_increment_on_overlap_m3_y",
        "current_increment_coverage_pct",
        "current_increment_is_complete",
    ]
    assert exported.loc[0, "mean_inventory_age_years"] == pytest.approx(45.0)
    assert exported.loc[0, "mean_current_age_years"] == pytest.approx(50.0)


def test_map_popup_keeps_biomass_quantities_and_sources_separate():
    """Dropping either estimate or provenance would make map interpretation ambiguous."""
    import app as dashboard_app

    rendered_map = dashboard_app.make_map(dashboard_result_fixture()).get_root().render()

    assert "Elusbiomassi süsinikuvaru: 300 t CO₂e" in rendered_map
    assert "Kavandatava raiemahu biomass: 120 t CO₂e" in rendered_map
    assert "Elusbiomassi mahu alus: eraldise tagavara + liigiosakaal" in rendered_map
    assert "Kavandatava raiemahu alus: raiemahu põhine hinnang" in rendered_map

from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Polygon
from streamlit.testing.v1 import AppTest

from carbon import estimate_intersection_from_notice_volume, parse_detail


def fallback_result_fixture():
    detail = parse_detail(
        {
            "_stand_id": 12148722,
            "elemendid": [
                {"puuliigiKood": "KS", "osakaal": 80, "vanus": 45},
                {"puuliigiKood": "KU", "osakaal": 10, "vanus": 50},
                {"puuliigiKood": "HB", "osakaal": 5, "vanus": 45},
                {"puuliigiKood": "LM", "osakaal": 5, "vanus": 45},
            ],
        }
    )
    allocated = estimate_intersection_from_notice_volume(
        notice_volume_m3=18.0,
        overlap_ha=0.4,
        total_overlap_ha=0.4,
        species_rows=detail["species_rows"],
    )
    carbon_co2e_t = sum(row["carbon_co2e_t"] for row in allocated)

    return gpd.GeoDataFrame(
        {
            "teatis_id": [3973677.0],
            "raiutav_maht": [18.0],
            "area_ha": [0.4],
            "carbon_co2e_t": [carbon_co2e_t],
            "carbon_t_per_ha": [carbon_co2e_t / 0.4],
            "estimated_stem_volume_m3": [18.0],
            "inventory_coverage_pct": [100.0],
            "dominant_species": ["Kask"],
            "mean_age": [45.0],
            "calculation_basis": ["raiemahu põhine hinnang"],
            "data_quality": ["raiemahu põhine"],
        },
        geometry=[Polygon([(24.47, 58.75), (24.48, 58.75), (24.48, 58.76)])],
        crs="EPSG:4326",
    )


def test_fallback_carbon_reaches_streamlit_dashboard():
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.session_state["results"] = fallback_result_fixture()
    app.session_state["species_df"] = pd.DataFrame(
        {
            "species": ["Kask"],
            "volume_m3": [14.4],
            "carbon_co2e_t": [17.5032],
        }
    )

    app.run(timeout=20)

    assert not app.exception
    assert app.metric[2].value == "21 t CO₂e"
    assert app.metric[3].value == "52 t CO₂e/ha"
    assert any("1 teatise süsinik on hinnatud raiutava mahu" in info.value for info in app.info)
    assert app.dataframe[0].value.loc[0, "carbon_co2e_t"] == pytest.approx(20.9352)
    assert app.dataframe[0].value.loc[0, "calculation_basis"] == "raiemahu põhine hinnang"

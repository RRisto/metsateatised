import sys

import geopandas as gpd
from shapely.geometry import box


def test_analysis_module_imports_without_streamlit():
    sys.modules.pop("notice_analysis", None)

    import notice_analysis

    assert "streamlit" not in notice_analysis.__dict__
    assert callable(notice_analysis.analyze_notices)
    assert callable(notice_analysis.fetch_stand_details)


def test_analyze_notices_uses_injected_detail_loader():
    from notice_analysis import analyze_notices

    notices = gpd.GeoDataFrame(
        {"teatis_id": [1]},
        geometry=[box(500000, 6500000, 500200, 6500200)],
        crs="EPSG:3301",
    )
    stands = gpd.GeoDataFrame(
        {"id": [7], "tagavara_1_ha": [30.0]},
        geometry=[box(500000, 6500000, 500100, 6500200)],
        crs="EPSG:3301",
    )
    requested = []

    def load_details(stand_ids, **_kwargs):
        requested.append(stand_ids)
        return []

    results, _ = analyze_notices(notices, stands, detail_loader=load_details)

    assert requested == [(7,)]
    assert len(results) == 1

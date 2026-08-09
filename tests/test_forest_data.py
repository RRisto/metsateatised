from unittest.mock import Mock

import geopandas as gpd
from shapely.geometry import Polygon

from forest_data import load_stands_for_notices, stand_cql_filter


def polygon(x):
    return Polygon([(x, 58.0), (x + 0.001, 58.0), (x + 0.001, 58.001)])


def test_stand_cql_filter_escapes_cadastral_reference():
    assert (
        stand_cql_filter("50404:002:O'NEIL", 2)
        == "katastri_nr = '50404:002:O''NEIL' AND eraldise_nr = 2"
    )


def test_exact_notice_references_are_used_instead_of_broad_bbox():
    notices = gpd.GeoDataFrame(
        {"katastri_nr": ["50404:002:0131"], "eraldise_nr": [2]},
        geometry=[polygon(24.47)],
        crs="EPSG:4326",
    )
    exact_result = gpd.GeoDataFrame({"id": [12148722]}, geometry=[polygon(24.47)], crs="EPSG:4326")
    exact_loader = Mock(return_value=exact_result)
    bbox_loader = Mock()

    stands = load_stands_for_notices(
        notices,
        exact_loader=exact_loader,
        bbox_loader=bbox_loader,
    )

    assert stands["id"].tolist() == [12148722]
    exact_loader.assert_called_once_with("katastri_nr = '50404:002:0131' AND eraldise_nr = 2")
    bbox_loader.assert_not_called()


def test_notice_without_exact_match_uses_its_own_bbox():
    notices = gpd.GeoDataFrame(
        {"katastri_nr": ["50404:002:0131"], "eraldise_nr": [2]},
        geometry=[polygon(24.47)],
        crs="EPSG:4326",
    )
    fallback_result = gpd.GeoDataFrame(
        {"id": [12148722]}, geometry=[polygon(24.47)], crs="EPSG:4326"
    )
    exact_loader = Mock(return_value=gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))
    bbox_loader = Mock(return_value=fallback_result)

    stands = load_stands_for_notices(
        notices,
        exact_loader=exact_loader,
        bbox_loader=bbox_loader,
    )

    assert stands["id"].tolist() == [12148722]
    bbox = bbox_loader.call_args.args[0]
    assert bbox[0] < 24.47
    assert bbox[2] > 24.471


def test_unique_stand_references_are_loaded_in_batches():
    count = 1_095
    notices = gpd.GeoDataFrame(
        {
            "katastri_nr": [f"50404:002:{number:04d}" for number in range(count)],
            "eraldise_nr": [number + 1 for number in range(count)],
        },
        geometry=[polygon(24.0 + number / 1000) for number in range(count)],
        crs="EPSG:4326",
    )
    all_stands = gpd.GeoDataFrame(
        {
            "id": list(range(count)),
            "katastri_nr": notices["katastri_nr"],
            "eraldise_nr": notices["eraldise_nr"],
        },
        geometry=notices.geometry.copy(),
        crs="EPSG:4326",
    )
    exact_loader = Mock(return_value=all_stands)
    bbox_loader = Mock()
    progress = Mock()

    stands = load_stands_for_notices(
        notices,
        exact_loader=exact_loader,
        bbox_loader=bbox_loader,
        batch_size=50,
        progress_callback=progress,
    )

    assert len(stands) == count
    assert exact_loader.call_count == 22
    assert all(call.args[0].count(" OR ") <= 49 for call in exact_loader.call_args_list)
    progress.assert_any_call(22, 22, "exact")
    bbox_loader.assert_not_called()

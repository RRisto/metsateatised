from unittest.mock import Mock

import requests

from wfs import fetch_wfs_features


def response_with_features(*feature_ids):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"features": [{"id": feature_id} for feature_id in feature_ids]}
    return response


def test_fetch_wfs_features_paginates_large_requests():
    request_get = Mock(
        side_effect=[
            response_with_features(1, 2),
            response_with_features(3, 4),
            response_with_features(5),
        ]
    )

    features = fetch_wfs_features(
        "metsaregister:eraldis",
        max_features=5,
        page_size=2,
        request_get=request_get,
    )

    assert [feature["id"] for feature in features] == [1, 2, 3, 4, 5]
    assert [call.kwargs["params"]["count"] for call in request_get.call_args_list] == [2, 2, 1]
    assert [call.kwargs["params"]["startIndex"] for call in request_get.call_args_list] == [
        0,
        2,
        4,
    ]


def test_fetch_wfs_features_retries_timed_out_page():
    request_get = Mock(
        side_effect=[
            requests.ReadTimeout("server was slow"),
            response_with_features(1),
        ]
    )
    sleep = Mock()

    features = fetch_wfs_features(
        "metsaregister:eraldis",
        max_features=1,
        page_size=1,
        retries=1,
        request_get=request_get,
        sleep=sleep,
    )

    assert features == [{"id": 1}]
    assert request_get.call_count == 2
    sleep.assert_called_once_with(1)


def test_fetch_wfs_features_reuses_disk_cache(tmp_path):
    request_get = Mock(return_value=response_with_features(1))

    first = fetch_wfs_features(
        "metsaregister:eraldis",
        max_features=1,
        request_get=request_get,
        cache_root=tmp_path,
    )
    second = fetch_wfs_features(
        "metsaregister:eraldis",
        max_features=1,
        request_get=request_get,
        cache_root=tmp_path,
    )

    assert first == second == [{"id": 1}]
    assert request_get.call_count == 1

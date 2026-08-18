from unittest.mock import Mock, call

import pytest
import requests

from wfs import fetch_wfs_features


def response_with_features(*feature_ids, number_matched=None):
    response = Mock()
    response.raise_for_status.return_value = None
    document = {
        "type": "FeatureCollection",
        "features": [{"id": feature_id} for feature_id in feature_ids],
        "numberReturned": len(feature_ids),
    }
    if number_matched is not None:
        document["numberMatched"] = number_matched
    response.json.return_value = document
    return response


def response_with_document(document):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = document
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


def test_fetch_wfs_features_paginates_until_short_page_when_unbounded():
    request_get = Mock(
        side_effect=[
            response_with_features(1, 2, number_matched=5),
            response_with_features(3, 4, number_matched=5),
            response_with_features(5, number_matched=5),
        ]
    )

    features = fetch_wfs_features(
        "metsaregister:teatis_arhiiv",
        max_features=None,
        page_size=2,
        request_get=request_get,
    )

    assert [feature["id"] for feature in features] == [1, 2, 3, 4, 5]
    assert [call.kwargs["params"]["startIndex"] for call in request_get.call_args_list] == [
        0,
        2,
        4,
    ]


def test_fetch_wfs_features_reports_completed_pages():
    progress = Mock()
    request_get = Mock(
        side_effect=[
            response_with_features(1, 2, number_matched=3),
            response_with_features(3, number_matched=3),
        ]
    )

    fetch_wfs_features(
        "metsaregister:teatis_arhiiv",
        max_features=None,
        page_size=2,
        request_get=request_get,
        page_progress=progress,
    )

    assert progress.call_args_list == [call(1, 2, 2), call(2, 1, 3)]


def test_fetch_wfs_features_uses_number_matched_when_server_caps_pages():
    """A short server-capped page must not truncate a response with known remaining rows."""
    request_get = Mock(
        side_effect=[
            response_with_features(1, 2, number_matched=5),
            response_with_features(3, 4, number_matched=5),
            response_with_features(5, number_matched=5),
        ]
    )

    features = fetch_wfs_features(
        "metsaregister:teatis_arhiiv",
        max_features=None,
        page_size=10,
        request_get=request_get,
    )

    assert [feature["id"] for feature in features] == [1, 2, 3, 4, 5]
    assert [call.kwargs["params"]["startIndex"] for call in request_get.call_args_list] == [
        0,
        2,
        4,
    ]


def test_fetch_wfs_features_without_total_continues_until_empty_page():
    """A short page without a total is not proof that the server has no next page."""
    request_get = Mock(
        side_effect=[
            response_with_features(1, 2),
            response_with_features(3),
            response_with_features(),
        ]
    )

    features = fetch_wfs_features(
        "metsaregister:teatis_arhiiv",
        max_features=None,
        page_size=10,
        request_get=request_get,
    )

    assert [feature["id"] for feature in features] == [1, 2, 3]
    assert request_get.call_count == 3


@pytest.mark.parametrize(
    "document",
    [
        {"type": "ExceptionReport", "message": "invalid CQL"},
        {"type": "FeatureCollection", "features": {"id": 1}},
    ],
)
def test_fetch_wfs_features_rejects_malformed_documents_before_caching(tmp_path, document):
    """A JSON error or non-list features value must never become a cached empty month."""
    request_get = Mock(return_value=response_with_document(document))

    with pytest.raises(ValueError, match="GeoJSON FeatureCollection"):
        fetch_wfs_features(
            "metsaregister:teatis_arhiiv",
            max_features=None,
            request_get=request_get,
            cache_root=tmp_path,
        )

    assert not list(tmp_path.rglob("*.json"))


def test_fetch_wfs_features_never_exceeds_bounded_maximum_when_server_overreturns():
    """A server ignoring count must not make the bounded public contract return extra rows."""
    request_get = Mock(return_value=response_with_features(1, 2, 3))

    features = fetch_wfs_features(
        "metsaregister:eraldis",
        max_features=2,
        page_size=10,
        request_get=request_get,
    )

    assert [feature["id"] for feature in features] == [1, 2]


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

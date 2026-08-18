from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import requests

from data_cache import read_json_cache, write_json_cache

WFS_URL = "https://gsavalik.envir.ee/geoserver/metsaregister/ows"


def _optional_response_count(document: dict, field: str) -> int | None:
    value = document.get(field)
    if value is None or value == "unknown":
        return None
    if isinstance(value, bool):
        raise ValueError(f"WFS response has invalid {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"WFS response has invalid {field}") from error
    if parsed < 0:
        raise ValueError(f"WFS response has invalid {field}")
    return parsed


def _validated_feature_page(document: object) -> tuple[list[dict], int | None]:
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ValueError("WFS response is not a GeoJSON FeatureCollection")
    page = document.get("features")
    if not isinstance(page, list):
        raise ValueError("WFS response is not a GeoJSON FeatureCollection with a features list")

    number_returned = _optional_response_count(document, "numberReturned")
    if number_returned is not None and number_returned != len(page):
        raise ValueError("WFS response numberReturned does not match its features list")
    return page, _optional_response_count(document, "numberMatched")


def fetch_wfs_features(
    type_name: str,
    *,
    max_features: int | None = None,
    cql_filter: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    page_size: int = 2_000,
    retries: int = 2,
    request_get: Callable[..., Any] = requests.get,
    sleep: Callable[[float], None] = time.sleep,
    cache_root: Path | None = None,
    cache_max_age: timedelta = timedelta(hours=24),
    force_refresh: bool = False,
    page_progress: Callable[[int, int, int], None] | None = None,
) -> list[dict]:
    """Fetch WFS features in bounded pages, retrying transient read timeouts."""
    features: list[dict] = []
    start_index = 0
    page_number = 0

    while max_features is None or len(features) < max_features:
        params: dict[str, str | int] = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": type_name,
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
        }
        remaining = None if max_features is None else max_features - len(features)
        requested_count = page_size if remaining is None else min(page_size, remaining)
        params["count"] = requested_count
        params["startIndex"] = start_index
        if cql_filter:
            params["CQL_FILTER"] = cql_filter
        if bbox:
            params["bbox"] = ",".join(map(str, bbox)) + ",EPSG:4326"

        cache_key = sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        cached = None
        if cache_root is not None and not force_refresh:
            cached = read_json_cache(
                "wfs",
                cache_key,
                max_age=cache_max_age,
                cache_root=cache_root,
            )

        if cached is None:
            for attempt in range(retries + 1):
                try:
                    response = request_get(WFS_URL, params=params, timeout=(10, 60))
                    response.raise_for_status()
                    break
                except requests.ReadTimeout:
                    if attempt == retries:
                        raise
                    sleep(2**attempt)
            document = response.json()
            page, number_matched = _validated_feature_page(document)
            if cache_root is not None:
                write_json_cache("wfs", cache_key, document, cache_root=cache_root)
        else:
            document = cached
            page, number_matched = _validated_feature_page(document)

        included_page = page[:requested_count]
        features.extend(included_page)
        page_number += 1

        if page_progress:
            page_progress(page_number, len(included_page), len(features))

        bounded_complete = max_features is not None and len(features) >= max_features
        matched_complete = number_matched is not None and len(features) >= min(
            number_matched,
            max_features if max_features is not None else number_matched,
        )
        if bounded_complete or matched_complete:
            break
        if not page:
            if number_matched is not None and len(features) < number_matched:
                raise RuntimeError(
                    "WFS pagination ended before numberMatched features were returned"
                )
            break
        start_index += len(page)

    return features

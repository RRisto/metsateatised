import os

import pytest
import requests

from carbon import estimate_intersection_from_notice_volume, parse_detail
from wfs import fetch_wfs_features

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="set RUN_LIVE_TESTS=1 to query the public Forest Register",
    ),
]


def test_notice_3973677_has_every_input_required_for_fallback_estimate():
    notices = fetch_wfs_features(
        "metsaregister:teatis_arhiiv",
        max_features=1,
        cql_filter="teatis_id = 3973677",
    )
    assert len(notices) == 1
    notice = notices[0]["properties"]
    assert notice["raiutav_maht"] == 18

    stands = fetch_wfs_features(
        "metsaregister:eraldis",
        max_features=1,
        cql_filter="katastri_nr = '50404:002:0131' AND eraldise_nr = 2",
    )
    assert len(stands) == 1
    stand_id = stands[0]["properties"]["id"]
    assert stand_id == 12148722

    response = requests.get(
        f"https://register.metsad.ee/portaal/api/rest/eraldis/detail/{stand_id}",
        timeout=(10, 30),
    )
    response.raise_for_status()
    detail = response.json()
    detail["_stand_id"] = stand_id
    parsed = parse_detail(detail)
    assert [(row["species_code"], row["share"]) for row in parsed["species_rows"]] == [
        ("KS", 80.0),
        ("KU", 10.0),
        ("HB", 5.0),
        ("LM", 5.0),
    ]

    allocated = estimate_intersection_from_notice_volume(
        notice_volume_m3=notice["raiutav_maht"],
        overlap_ha=notice["pindala"],
        total_overlap_ha=notice["pindala"],
        species_rows=parsed["species_rows"],
    )
    assert sum(row["volume_m3"] for row in allocated) == pytest.approx(18.0)
    assert sum(row["carbon_co2e_t"] for row in allocated) == pytest.approx(20.9352)

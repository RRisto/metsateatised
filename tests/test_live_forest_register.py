import os
from datetime import date

import pytest
import requests

from carbon import calculate_notice_carbon, estimate_planned_harvest_volume
from stand_model import build_stand_record
from wfs import fetch_wfs_features

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="set RUN_LIVE_TESTS=1 to query the public Forest Register",
    ),
]


def test_notice_3973677_has_stand_and_planned_harvest_inputs():
    notices = fetch_wfs_features(
        "metsaregister:teatis_arhiiv",
        max_features=1,
        cql_filter="teatis_id = 3973677",
    )
    assert len(notices) == 1
    notice = notices[0]["properties"]

    stands = fetch_wfs_features(
        "metsaregister:eraldis",
        max_features=1,
        cql_filter="katastri_nr = '50404:002:0131' AND eraldise_nr = 2",
    )
    assert len(stands) == 1
    stand_properties = stands[0]["properties"]
    stand_id = stand_properties["id"]

    response = requests.get(
        f"https://register.metsad.ee/portaal/api/rest/eraldis/detail/{stand_id}",
        timeout=(10, 30),
    )
    response.raise_for_status()
    detail = response.json()
    detail["_stand_id"] = stand_id
    stand = build_stand_record(stand_properties, detail, as_of_date=date.today())
    planned = estimate_planned_harvest_volume(
        notice_volume_m3=notice["raiutav_maht"], species=stand.species
    )
    carbon = calculate_notice_carbon(
        standing_species_volumes=(),
        planned_harvest_species_volumes=planned.species_volumes,
    )

    assert stand.stand_id == 12148722
    assert stand.species
    assert planned.total_volume_m3 == pytest.approx(notice["raiutav_maht"])
    assert carbon.planned_harvest_biomass_tco2 > 0

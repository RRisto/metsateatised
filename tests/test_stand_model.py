import subprocess
import sys
from datetime import date

import pytest

from stand_model import (
    build_stand_record,
    classify_inventory_recency,
    normalize_species_records,
)

WFS_STAND = {
    "id": "12148722",
    "katastri_nr": "50404:002:0131",
    "eraldise_nr": "2",
    "invent_kp": "2021-07-29Z",
    "pindala": "1.45",
    "korgus": "18.5",
    "tagavara_1_ha": "228",
    "tagavara_2_ha": "14",
    "tagavara_y_ha": "7",
    "juurdekasv": "3.8",
    "rpindala_1": "23.4",
    "taius_1": "78",
    "kasvukoht_kood": "KM",
    "boniteedi_kood": "II",
    "kuivendatud": "true",
    "arengukl_kood": "NOOR",
}

DETAIL = {
    "elemendid": [
        {
            "puuliigiKood": "KS",
            "osakaal": "80",
            "tagavara": "180",
            "vanus": "45",
            "jooksevVanus": "50",
        },
        {
            "puuliigiKood": "KU",
            "osakaal": "20",
            "tagavara": "69",
            "vanus": "40",
            "jooksevVanus": "45",
        },
    ]
}


def test_stand_model_import_does_not_load_carbon_calculation_layer():
    """Importing the domain model must not depend on the carbon calculation module."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import stand_model; raise SystemExit('carbon' in sys.modules)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_build_stand_record_merges_wfs_and_detail():
    """Changing WFS/detail field mapping breaks the unified stand contract."""
    stand = build_stand_record(WFS_STAND, DETAIL, as_of_date=date(2026, 8, 9))

    assert stand.stand_id == 12148722
    assert stand.cadastral_reference == "50404:002:0131"
    assert stand.stand_number == 2
    assert stand.inventory_date == date(2021, 7, 29)
    assert stand.inventory_age_years == pytest.approx(5.03, rel=0.01)
    assert stand.current_increment_m3_ha_y == 3.8
    assert stand.species[0].inventory_age == 45
    assert stand.species[0].current_age == 50


def test_total_wfs_stock_sums_available_living_strata():
    """Dropping a living stratum would understate the stand's WFS stock."""
    row = {**WFS_STAND, "tagavara_1_ha": 228, "tagavara_2_ha": 14, "tagavara_y_ha": 7}

    stand = build_stand_record(row, DETAIL, as_of_date=date(2026, 8, 9))

    assert stand.stock_m3_ha == 249
    assert stand.raw_stock_components_m3_ha == {
        "layer_1": 228.0,
        "layer_2": 14.0,
        "young": 7.0,
    }


def test_normalize_species_records_keeps_missing_stock_and_ignores_negative_stock():
    """Treating missing volume as negative would discard useful species composition."""
    records = normalize_species_records(
        {
            "elemendid": [
                {"puuliigiKood": "MA", "osakaal": "50", "tagavara": "nan"},
                {"puuliigiKood": "KU", "osakaal": "30", "tagavara": "-2"},
                {
                    "puuliigiKood": "KS",
                    "osakaal": "20",
                    "tagavara": "10",
                    "vanus": "30",
                    "jooksevVanus": "35",
                },
            ]
        }
    )

    assert [record.code for record in records] == ["MA", "KS"]
    assert records[0].stock_m3_ha is None
    assert records[1].stock_m3_ha == 10.0
    assert records[1].inventory_age == 30.0
    assert records[1].current_age == 35.0


def test_build_stand_record_excludes_invalid_stock_components():
    """Rejected inputs remain auditable while invalid values stay out of the total."""
    row = {**WFS_STAND, "tagavara_1_ha": "nan", "tagavara_2_ha": -2, "tagavara_y_ha": "7"}

    stand = build_stand_record(row, None, as_of_date=date(2026, 8, 9))

    assert stand.stock_m3_ha == 7.0
    assert stand.raw_stock_components_m3_ha == {"young": 7.0}
    assert stand.raw_stock_component_inputs == {
        "tagavara_1_ha": "nan",
        "tagavara_2_ha": -2,
        "tagavara_y_ha": "7",
    }


def test_normalization_preserves_real_stratum_identity_and_raw_fields_immutably():
    """Dropping or mutating source metadata would make multi-stratum allocation unauditable."""
    raw_element = {
        "eraldisId": 6522173,
        "rindeKood": "2",
        "puuliigiKood": "KU",
        "paritoluKood": "S",
        "osakaal": 100,
        "vanus": 81,
        "aasta": 1932,
        "korgus": 15.0,
        "diameeter": 14,
        "gSumma": 0.0,
        "tagavara": 16,
        "arv": 130,
        "enamus": False,
        "mahtTm": 19.2,
        "jooksevVanus": 94,
        "id": 20048897,
    }

    record = normalize_species_records({"elemendid": [raw_element]})[0]

    assert record.record_id == 20048897
    assert record.stratum_code == "2"
    assert record.raw_fields == raw_element
    with pytest.raises(TypeError):
        record.raw_fields["tagavara"] = 999


def test_stand_record_stock_audit_mappings_are_immutable():
    """A frozen stand must not expose mutable dictionaries that alter its audit trail."""
    stand = build_stand_record(WFS_STAND, DETAIL, as_of_date=date(2026, 8, 9))

    with pytest.raises(TypeError):
        stand.raw_stock_components_m3_ha["layer_1"] = 999
    with pytest.raises(TypeError):
        stand.raw_stock_component_inputs["tagavara_1_ha"] = 999


@pytest.mark.parametrize(
    ("inventory_date", "as_of_date", "expected_years", "expected_recency"),
    [
        (date(2020, 8, 9), date(2023, 8, 9), 3.0, "hea"),
        (date(2020, 8, 9), date(2026, 8, 9), 6.0, "vananev"),
        (date(2017, 8, 9), date(2026, 8, 9), 9.0, "nõrk"),
        (date(2020, 2, 29), date(2023, 2, 28), 3.0, "hea"),
        (date(2020, 2, 29), date(2026, 2, 28), 6.0, "vananev"),
        (date(2020, 2, 29), date(2029, 2, 28), 9.0, "nõrk"),
    ],
)
def test_build_stand_record_uses_calendar_anniversaries_for_recency(
    inventory_date, as_of_date, expected_years, expected_recency
):
    """Day-count division must not delay recency bands past calendar anniversaries."""
    stand = build_stand_record(
        {**WFS_STAND, "invent_kp": inventory_date.isoformat()},
        DETAIL,
        as_of_date=as_of_date,
    )

    assert stand.inventory_age_years == expected_years
    assert classify_inventory_recency(stand.inventory_age_years) == expected_recency


@pytest.mark.parametrize(
    ("years", "expected"),
    [
        (0.0, "väga hea"),
        (2.0, "väga hea"),
        (3.0, "hea"),
        (5.0, "hea"),
        (6.0, "vananev"),
        (8.0, "vananev"),
        (9.0, "nõrk"),
    ],
)
def test_inventory_recency_bands(years, expected):
    """Changing a recency threshold would misrepresent inventory freshness."""
    assert classify_inventory_recency(years) == expected


@pytest.mark.parametrize("years", [None, float("nan"), -0.1])
def test_missing_or_invalid_inventory_age_is_not_classified_as_fresh(years):
    """Unknown inventory timing must never be promoted to the strongest recency band."""
    assert classify_inventory_recency(years) == "teadmata"


@pytest.mark.parametrize(
    ("years", "expected"),
    [
        (2.999, "väga hea"),
        (3.0, "hea"),
        (5.999, "hea"),
        (6.0, "vananev"),
        (8.999, "vananev"),
        (9.0, "nõrk"),
    ],
)
def test_inventory_recency_uses_completed_year_bands(years, expected):
    """Promoting an inventory before its third, sixth, or ninth anniversary is incorrect."""
    assert classify_inventory_recency(years) == expected

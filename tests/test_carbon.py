import pytest

from carbon import (
    DEFAULT_WOOD_DENSITY,
    allocate_volume_by_species,
    carbon_from_species_volume,
    density_for_species,
    estimate_intersection_from_notice_volume,
    parse_detail,
    species_name_for_code,
)


def test_density_normalizes_species_code():
    assert density_for_species(" ma ") == 0.42


def test_unknown_species_uses_default_density():
    assert density_for_species("XX") == DEFAULT_WOOD_DENSITY


def test_carbon_formula_for_spruce():
    expected = 10 * 0.40 * 1.30 * 0.50 * (44 / 12)

    assert carbon_from_species_volume(10, "KU") == pytest.approx(expected)


def test_parse_detail_skips_negative_and_invalid_volume():
    detail = {
        "_stand_id": 7,
        "elemendid": [
            {"puuliigiKood": "MA", "tagavara": "100", "vanus": "40"},
            {"puuliigiKood": "KU", "tagavara": "invalid"},
            {"puuliigiKood": "KS", "tagavara": -1},
        ],
    }

    parsed = parse_detail(detail)

    assert parsed["stand_id"] == 7
    assert parsed["volume_m3_ha"] == 100.0
    assert parsed["main_species"] == "Mänd"
    assert parsed["weighted_age"] == 40.0


def test_parse_detail_keeps_species_shares_when_inventory_volume_is_missing():
    detail = {
        "_stand_id": 12148722,
        "elemendid": [
            {"puuliigiKood": "KS", "osakaal": 80, "vanus": 45},
            {"puuliigiKood": "KU", "osakaal": 20, "vanus": 50},
        ],
    }

    parsed = parse_detail(detail)

    assert parsed["volume_m3_ha"] == 0
    assert parsed["main_species"] == "Kask"
    assert [row["share"] for row in parsed["species_rows"]] == [80.0, 20.0]
    assert all(row["volume_m3_ha"] is None for row in parsed["species_rows"])


def test_allocate_volume_by_species_uses_normalized_shares():
    species_rows = [
        {"species_code": "KS", "species_name": "Kask", "share": 80.0, "age": 45.0},
        {"species_code": "KU", "species_name": "Kuusk", "share": 10.0, "age": 50.0},
        {"species_code": "HB", "species_name": "Haab", "share": 5.0, "age": 45.0},
        {"species_code": "LM", "species_name": "Sanglepp", "share": 5.0, "age": 45.0},
    ]

    allocated = allocate_volume_by_species(18.0, species_rows)

    assert [row["volume_m3"] for row in allocated] == pytest.approx([14.4, 1.8, 0.9, 0.9])
    assert sum(row["carbon_co2e_t"] for row in allocated) == pytest.approx(20.9352)


def test_notice_volume_is_prorated_across_intersections():
    species_rows = [{"species_code": "KS", "species_name": "Kask", "share": 100.0, "age": 45.0}]

    allocated = estimate_intersection_from_notice_volume(
        notice_volume_m3=18.0,
        overlap_ha=0.25,
        total_overlap_ha=0.5,
        species_rows=species_rows,
    )

    assert allocated[0]["volume_m3"] == 9.0


@pytest.mark.parametrize(
    ("code", "name"),
    [
        ("JA", "Jalakas"),
        ("KD", "Kadakas"),
        ("LH", "Lehis"),
        ("PA", "Paju"),
        ("PI", "Pihlakas"),
        ("PK", "Paakspuu"),
        ("PN", "Pärn"),
        ("PP", "Pappel"),
        ("RE", "Remmelgas"),
        ("SP", "Sarapuu"),
        ("TM", "Toomingas"),
        ("TO", "Teised okaspuud"),
        ("TP", "Teised põõsaliigid"),
        ("VA", "Vaher"),
        ("0", "Määramata"),
    ],
)
def test_species_codes_have_canonical_names(code, name):
    assert species_name_for_code(code) == name


def test_unknown_species_code_has_readable_fallback():
    assert species_name_for_code("XY") == "Muu (XY)"

from datetime import date

import pytest

from carbon import (
    SpeciesVolume,
    VolumeBasis,
    calculate_notice_carbon,
    carbon_from_species_volume,
    estimate_planned_harvest_volume,
    estimate_standing_volume,
)
from stand_model import SpeciesRecord, StandRecord, build_stand_record


def _stand(*, stock_m3_ha: float | None, species: tuple[SpeciesRecord, ...]) -> StandRecord:
    return StandRecord(
        stand_id=1,
        cadastral_reference=None,
        stand_number=None,
        inventory_date=None,
        inventory_age_years=None,
        area_ha=1.0,
        height_m=None,
        stock_m3_ha=stock_m3_ha,
        raw_stock_components_m3_ha={},
        raw_stock_component_inputs={},
        current_increment_m3_ha_y=None,
        basal_area_m2_ha=None,
        stocking_pct=None,
        site_type=None,
        site_class=None,
        drained=None,
        development_class=None,
        species=species,
    )


SPECIES_WITH_SHARES = (
    SpeciesRecord("KS", "Kask", 80.0, None, None, None),
    SpeciesRecord("KU", "Kuusk", 10.0, None, None, None),
    SpeciesRecord("HB", "Haab", 5.0, None, None, None),
    SpeciesRecord("LM", "Sanglepp", 5.0, None, None, None),
)

STAND_WITH_SPECIES_STOCK = _stand(
    stock_m3_ha=109.0,
    species=(SpeciesRecord("KS", "Kask", 100.0, 100.0, None, None),),
)
STAND_WITH_WFS_STOCK_ONLY = _stand(stock_m3_ha=109.0, species=SPECIES_WITH_SHARES)
STAND_WITHOUT_STOCK = _stand(stock_m3_ha=None, species=SPECIES_WITH_SHARES)


def test_detail_species_stock_takes_priority_over_wfs_stock():
    """Using WFS stock when detail stock exists would discard the better inventory source."""
    estimate = estimate_standing_volume(STAND_WITH_SPECIES_STOCK, overlap_ha=0.4)

    assert estimate.basis is VolumeBasis.DETAIL_SPECIES_STOCK
    assert estimate.total_volume_m3 == pytest.approx(40.0)


def test_wfs_stock_is_allocated_by_species_share_when_detail_stock_is_missing():
    """Allocating a known stand total by raw shares would misstate its species volumes."""
    estimate = estimate_standing_volume(STAND_WITH_WFS_STOCK_ONLY, overlap_ha=0.4)

    assert estimate.basis is VolumeBasis.WFS_STAND_STOCK_ALLOCATED
    assert estimate.total_volume_m3 == pytest.approx(43.6)
    assert [row.volume_m3 for row in estimate.species_volumes] == pytest.approx(
        [34.88, 4.36, 2.18, 2.18]
    )


def test_missing_inventory_never_becomes_standing_harvest_volume():
    """A notice harvest quantity must not be silently represented as standing stock."""
    estimate = estimate_standing_volume(STAND_WITHOUT_STOCK, overlap_ha=0.4)

    assert estimate.basis is VolumeBasis.UNKNOWN
    assert estimate.total_volume_m3 is None


def test_planned_harvest_volume_is_separate_and_carries_its_own_basis():
    """A planned-harvest estimate must retain notice-volume provenance."""
    estimate = estimate_planned_harvest_volume(18.0, SPECIES_WITH_SHARES)

    assert estimate.basis is VolumeBasis.NOTICE_HARVEST_VOLUME
    assert estimate.total_volume_m3 == pytest.approx(18.0)
    assert [row.volume_m3 for row in estimate.species_volumes] == pytest.approx(
        [14.4, 1.8, 0.9, 0.9]
    )


def test_partial_harvest_keeps_standing_and_planned_harvest_separate():
    """Combining the estimates would make planned harvesting look like standing biomass."""
    result = calculate_notice_carbon(
        standing_species_volumes=[SpeciesVolume("KU", 100.0)],
        planned_harvest_species_volumes=[SpeciesVolume("KU", 40.0)],
    )

    assert result.standing_live_biomass_tco2 == pytest.approx(carbon_from_species_volume(100, "KU"))
    assert result.planned_harvest_biomass_tco2 == pytest.approx(
        carbon_from_species_volume(40, "KU")
    )
    assert result.planned_harvest_biomass_tco2 < result.standing_live_biomass_tco2


REAL_MULTI_STRATUM_WFS = {
    "id": 6522173,
    "tagavara_1_ha": 228,
    "tagavara_2_ha": 16,
    "tagavara_y_ha": 0,
}
REAL_MULTI_STRATUM_DETAIL = {
    "elemendid": [
        {
            "eraldisId": 6522173,
            "rindeKood": "1",
            "puuliigiKood": "KU",
            "paritoluKood": "S",
            "osakaal": 54,
            "vanus": 178,
            "aasta": 1835,
            "korgus": 28.0,
            "diameeter": 36,
            "gSumma": 0.0,
            "tagavara": 123,
            "arv": 92,
            "enamus": True,
            "mahtTm": 147.6,
            "jooksevVanus": 191,
            "id": 20048903,
        },
        {
            "eraldisId": 6522173,
            "rindeKood": "1",
            "puuliigiKood": "MA",
            "paritoluKood": "S",
            "osakaal": 31,
            "vanus": 178,
            "aasta": 1835,
            "korgus": 28.0,
            "diameeter": 32,
            "gSumma": 0.0,
            "tagavara": 71,
            "arv": 70,
            "enamus": False,
            "mahtTm": 85.2,
            "jooksevVanus": 191,
            "id": 20048901,
        },
        {
            "eraldisId": 6522173,
            "rindeKood": "1",
            "puuliigiKood": "KS",
            "paritoluKood": "S",
            "osakaal": 11,
            "vanus": 148,
            "aasta": 1865,
            "korgus": 27.0,
            "diameeter": 28,
            "gSumma": 0.0,
            "tagavara": 25,
            "arv": 32,
            "enamus": False,
            "mahtTm": 30.0,
            "jooksevVanus": 161,
            "id": 20048905,
        },
        {
            "eraldisId": 6522173,
            "rindeKood": "1",
            "puuliigiKood": "HB",
            "paritoluKood": "S",
            "osakaal": 4,
            "vanus": 148,
            "aasta": 1865,
            "korgus": 29.0,
            "diameeter": 38,
            "gSumma": 0.0,
            "tagavara": 9,
            "arv": 6,
            "enamus": False,
            "mahtTm": 10.8,
            "jooksevVanus": 161,
            "id": 20048899,
        },
        {
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
        },
    ]
}


def test_real_schema_multistratum_planned_volume_uses_detail_stock_not_pooled_shares():
    """Pooling two independently normalized strata would overallocate the duplicate KU rows."""
    stand = build_stand_record(
        REAL_MULTI_STRATUM_WFS,
        REAL_MULTI_STRATUM_DETAIL,
        as_of_date=date(2026, 8, 9),
    )

    estimate = estimate_planned_harvest_volume(100.0, stand=stand)

    assert estimate.is_complete is True
    assert [row.source_record_id for row in estimate.species_volumes] == [
        20048903,
        20048901,
        20048905,
        20048899,
        20048897,
    ]
    assert [row.volume_m3 for row in estimate.species_volumes] == pytest.approx(
        [100 * 123 / 244, 100 * 71 / 244, 100 * 25 / 244, 100 * 9 / 244, 100 * 16 / 244]
    )


def test_incomplete_detail_stock_falls_back_to_matching_wfs_strata():
    """One detail stock value must not suppress complete layer-aware WFS allocation."""
    stand = build_stand_record(
        {"id": 1, "tagavara_1_ha": 100, "tagavara_2_ha": 50},
        {
            "elemendid": [
                {
                    "id": 11,
                    "rindeKood": "1",
                    "puuliigiKood": "MA",
                    "osakaal": 60,
                    "tagavara": 60,
                    "vanus": 80,
                },
                {
                    "id": 12,
                    "rindeKood": "1",
                    "puuliigiKood": "KS",
                    "osakaal": 40,
                    "tagavara": None,
                    "vanus": 40,
                },
                {
                    "id": 13,
                    "rindeKood": "2",
                    "puuliigiKood": "KU",
                    "osakaal": 100,
                    "tagavara": None,
                    "vanus": 20,
                },
            ]
        },
        as_of_date=date(2026, 8, 9),
    )

    estimate = estimate_standing_volume(stand, overlap_ha=1.0)

    assert estimate.basis is VolumeBasis.WFS_STAND_STOCK_ALLOCATED
    assert estimate.is_complete is True
    assert estimate.total_volume_m3 == 150.0
    assert [row.volume_m3 for row in estimate.species_volumes] == pytest.approx([60, 40, 50])
    assert [row.inventory_age for row in estimate.species_volumes] == [80, 40, 20]


def test_non_living_shrub_stratum_is_not_allocated_planned_volume():
    """A stockless A stratum must not receive a share of living-stock biomass."""
    stand = build_stand_record(
        {"id": 11871445, "tagavara_1_ha": 176, "tagavara_2_ha": 26},
        {
            "elemendid": [
                {"id": 1, "rindeKood": "1", "puuliigiKood": "MA", "osakaal": 65, "tagavara": 114},
                {"id": 2, "rindeKood": "1", "puuliigiKood": "KU", "osakaal": 22, "tagavara": 39},
                {"id": 3, "rindeKood": "1", "puuliigiKood": "HB", "osakaal": 13, "tagavara": 23},
                {"id": 4, "rindeKood": "2", "puuliigiKood": "KU", "osakaal": 100, "tagavara": 26},
                {"id": 5, "rindeKood": "A", "puuliigiKood": "SP", "osakaal": 100},
            ]
        },
        as_of_date=date(2026, 8, 9),
    )

    estimate = estimate_planned_harvest_volume(100.0, stand=stand)

    assert estimate.is_complete is True
    assert sum(row.volume_m3 for row in estimate.species_volumes) == pytest.approx(100.0)
    assert all(row.species_code != "SP" for row in estimate.species_volumes)


def test_single_stratum_live_schema_without_stock_allocates_planned_volume_by_shares():
    """A stockless single living stratum still has unambiguous notice-volume composition."""
    stand = build_stand_record(
        {
            "id": 12148722,
            "tagavara_1_ha": None,
            "tagavara_2_ha": None,
            "tagavara_y_ha": None,
        },
        {
            "elemendid": [
                {
                    "rindeKood": "1",
                    "puuliigiKood": "KS",
                    "paritoluKood": "S",
                    "osakaal": 80,
                    "vanus": 45,
                    "aasta": 1981,
                    "korgus": 18.0,
                    "jooksevVanus": 45,
                    "id": 40766420,
                },
                {
                    "rindeKood": "1",
                    "puuliigiKood": "KU",
                    "paritoluKood": "S",
                    "osakaal": 10,
                    "vanus": 50,
                    "aasta": 1976,
                    "korgus": 18.0,
                    "jooksevVanus": 50,
                    "id": 40766421,
                },
                {
                    "rindeKood": "1",
                    "puuliigiKood": "HB",
                    "paritoluKood": "V",
                    "osakaal": 5,
                    "vanus": 45,
                    "aasta": 1981,
                    "korgus": 22.0,
                    "jooksevVanus": 45,
                    "id": 40766422,
                },
                {
                    "rindeKood": "1",
                    "puuliigiKood": "LM",
                    "paritoluKood": "V",
                    "osakaal": 5,
                    "vanus": 45,
                    "aasta": 1981,
                    "korgus": 16.0,
                    "jooksevVanus": 45,
                    "id": 40766423,
                },
            ]
        },
        as_of_date=date(2026, 8, 9),
    )

    estimate = estimate_planned_harvest_volume(100.0, stand=stand)

    assert estimate.is_complete is True
    assert [row.volume_m3 for row in estimate.species_volumes] == pytest.approx([80, 10, 5, 5])


def test_positive_planned_volume_without_allocation_is_explicitly_incomplete():
    """Known notice provenance must not make uncalculable positive biomass look complete."""
    estimate = estimate_planned_harvest_volume(10.0, ())
    carbon = calculate_notice_carbon(
        standing_species_volumes=(),
        planned_harvest_species_volumes=estimate.species_volumes,
    )

    assert estimate.basis is VolumeBasis.NOTICE_HARVEST_VOLUME
    assert estimate.total_volume_m3 == 10.0
    assert estimate.is_complete is False
    assert carbon.planned_harvest_biomass_tco2 is None


def test_zero_planned_volume_is_complete_zero_without_species_shares():
    """A finite known zero must remain zero rather than become unknown biomass."""
    estimate = estimate_planned_harvest_volume(0.0, ())
    carbon = calculate_notice_carbon(
        standing_species_volumes=(),
        planned_harvest_species_volumes=estimate.species_volumes,
    )

    assert estimate.is_complete is True
    assert estimate.total_volume_m3 == 0.0
    assert carbon.planned_harvest_biomass_tco2 == 0.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_public_volume_inputs_are_unknown(value):
    """Non-finite inputs must not leak NaN or infinity into public biomass estimates."""
    planned = estimate_planned_harvest_volume(value, SPECIES_WITH_SHARES)
    standing = estimate_standing_volume(STAND_WITH_WFS_STOCK_ONLY, overlap_ha=value)

    assert planned.basis is VolumeBasis.UNKNOWN
    assert planned.total_volume_m3 is None
    assert planned.is_complete is False
    assert standing.basis is VolumeBasis.UNKNOWN
    assert standing.total_volume_m3 is None
    assert standing.is_complete is False

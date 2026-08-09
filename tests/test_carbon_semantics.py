import pytest

from carbon import (
    VolumeBasis,
    SpeciesVolume,
    calculate_notice_carbon,
    carbon_from_species_volume,
    estimate_planned_harvest_volume,
    estimate_standing_volume,
)
from stand_model import SpeciesRecord, StandRecord


def _stand(
    *, stock_m3_ha: float | None, species: tuple[SpeciesRecord, ...]
) -> StandRecord:
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

    assert result.standing_live_biomass_tco2 == pytest.approx(
        carbon_from_species_volume(100, "KU")
    )
    assert result.planned_harvest_biomass_tco2 == pytest.approx(
        carbon_from_species_volume(40, "KU")
    )
    assert result.planned_harvest_biomass_tco2 < result.standing_live_biomass_tco2

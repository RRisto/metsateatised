from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isclose, isfinite

import numpy as np
import pandas as pd

from stand_model import SpeciesRecord, StandRecord
from stand_model import species_name_for_code as _species_name_for_code

WOOD_DENSITY = {
    "MA": 0.42,
    "KU": 0.40,
    "KS": 0.51,
    "HB": 0.35,
    "LM": 0.45,
    "LV": 0.45,
    "SA": 0.57,
    "TA": 0.58,
}
DEFAULT_WOOD_DENSITY = 0.42
CARBON_FRACTION = 0.50
CO2_PER_C = 44.0 / 12.0
BEF = 1.30
STRATUM_STOCK_COMPONENT = {"1": "layer_1", "2": "layer_2", "Y": "young"}
NON_LIVING_STOCK_STRATA = {"A"}


class VolumeBasis(StrEnum):
    """Provenance for an estimated stem-volume total."""

    DETAIL_SPECIES_STOCK = "detail-liigiline tagavara"
    WFS_STAND_STOCK_ALLOCATED = "eraldise tagavara + liigiosakaal"
    NOTICE_HARVEST_VOLUME = "raiemahu põhine hinnang"
    UNKNOWN = "andmed puuduvad"


@dataclass(frozen=True)
class SpeciesVolume:
    """A stem-volume allocation for one tree species."""

    species_code: str | None
    volume_m3: float
    source_record_id: int | str | None = None
    stratum_code: str | None = None
    inventory_age: float | None = None
    current_age: float | None = None


@dataclass(frozen=True)
class StandingVolumeEstimate:
    """A volume estimate with its explicit source and species allocation."""

    basis: VolumeBasis
    total_volume_m3: float | None
    species_volumes: tuple[SpeciesVolume, ...]
    is_complete: bool


@dataclass(frozen=True)
class NoticeCarbonEstimate:
    """Independent biomass estimates for inventory state and a planned harvest."""

    standing_live_biomass_tco2: float | None
    planned_harvest_biomass_tco2: float | None


def aggregate_intersections(intersection_rows: list[dict]) -> pd.DataFrame:
    """Aggregate stand intersections while preserving unknown estimates."""
    intersections = pd.DataFrame(intersection_rows)
    if intersections.empty:
        return intersections

    completeness_columns = {
        "standing_biomass_is_complete": "standing_live_biomass_tco2",
        "planned_harvest_biomass_is_complete": "planned_harvest_biomass_tco2",
    }
    for completeness_column, value_column in completeness_columns.items():
        if completeness_column not in intersections:
            intersections[completeness_column] = intersections[value_column].notna()

    def combined_basis(values: pd.Series) -> str:
        return " + ".join(sorted({str(value) for value in values if pd.notna(value)}))

    aggregated = intersections.groupby("notice_ix", as_index=False).agg(
        standing_live_biomass_tco2=(
            "standing_live_biomass_tco2",
            lambda values: values.sum(min_count=1),
        ),
        planned_harvest_biomass_tco2=(
            "planned_harvest_biomass_tco2",
            lambda values: values.sum(min_count=1),
        ),
        standing_volume_basis=("standing_volume_basis", combined_basis),
        planned_harvest_volume_basis=(
            "planned_harvest_volume_basis",
            combined_basis,
        ),
        standing_biomass_is_complete=("standing_biomass_is_complete", "all"),
        planned_harvest_biomass_is_complete=(
            "planned_harvest_biomass_is_complete",
            "all",
        ),
        covered_by_inventory_ha=("overlap_ha", "sum"),
        weighted_age_num=("weighted_age_num", "sum"),
        weighted_age_den=("weighted_age_den", "sum"),
    )
    aggregated["mean_age"] = aggregated["weighted_age_num"] / aggregated[
        "weighted_age_den"
    ].replace(0, np.nan)
    for completeness_column, value_column in completeness_columns.items():
        incomplete = ~aggregated[completeness_column].astype(bool)
        aggregated.loc[incomplete, value_column] = np.nan
        basis_column = (
            "standing_volume_basis"
            if completeness_column == "standing_biomass_is_complete"
            else "planned_harvest_volume_basis"
        )
        aggregated.loc[incomplete, basis_column] = aggregated.loc[incomplete, basis_column].apply(
            _mark_basis_incomplete
        )
    return aggregated


def density_for_species(code: str | None) -> float:
    if code is None:
        return DEFAULT_WOOD_DENSITY
    return WOOD_DENSITY.get(str(code).strip().upper(), DEFAULT_WOOD_DENSITY)


def species_name_for_code(code: str | None) -> str:
    """Compatibility wrapper for the canonical stand-model species helper."""
    return _species_name_for_code(code)


def _mark_basis_incomplete(basis: object) -> str:
    text = str(basis) if pd.notna(basis) and str(basis) else VolumeBasis.UNKNOWN.value
    if VolumeBasis.UNKNOWN.value in text:
        return text
    return f"{VolumeBasis.UNKNOWN.value} + {text}"


def _is_living_stock_record(species: SpeciesRecord) -> bool:
    return species.stratum_code not in NON_LIVING_STOCK_STRATA


def _species_volume(species: SpeciesRecord, volume_m3: float) -> SpeciesVolume:
    return SpeciesVolume(
        species_code=species.code,
        volume_m3=volume_m3,
        source_record_id=species.record_id,
        stratum_code=species.stratum_code,
        inventory_age=species.inventory_age,
        current_age=species.current_age,
    )


def _allocate_by_weights(
    total_volume_m3: float,
    weighted_species: tuple[tuple[SpeciesRecord, float], ...],
) -> tuple[SpeciesVolume, ...]:
    if total_volume_m3 == 0:
        return (SpeciesVolume(None, 0.0),)
    usable = tuple(
        (species, float(weight))
        for species, weight in weighted_species
        if isfinite(float(weight)) and float(weight) > 0
    )
    total_weight = sum(weight for _, weight in usable)
    if not usable or total_weight <= 0:
        return ()
    return tuple(
        _species_volume(species, total_volume_m3 * weight / total_weight)
        for species, weight in usable
    )


def _allocate_by_shares(
    total_volume_m3: float, species: tuple[SpeciesRecord, ...]
) -> tuple[SpeciesVolume, ...]:
    if not species or any(
        item.share_pct is None or not isfinite(item.share_pct) or item.share_pct < 0
        for item in species
    ):
        return ()
    return _allocate_by_weights(
        total_volume_m3,
        tuple(
            (item, item.share_pct)
            for item in species
            if _is_living_stock_record(item) and item.share_pct is not None
        ),
    )


def _stocks_reconcile(detail_total: float, inventory_total: float) -> bool:
    return isclose(detail_total, inventory_total, rel_tol=0.02, abs_tol=1.0)


def _complete_detail_species(stand: StandRecord) -> tuple[SpeciesRecord, ...] | None:
    living_species = tuple(item for item in stand.species if _is_living_stock_record(item))
    if not living_species or any(item.stock_m3_ha is None for item in living_species):
        return None

    if stand.raw_stock_components_m3_ha:
        has_strata = any(item.stratum_code is not None for item in living_species)
        if has_strata:
            for component, inventory_total in stand.raw_stock_components_m3_ha.items():
                matching = tuple(
                    item
                    for item in living_species
                    if STRATUM_STOCK_COMPONENT.get(item.stratum_code) == component
                )
                if inventory_total > 0 and not matching:
                    return None
                if matching and not _stocks_reconcile(
                    sum(item.stock_m3_ha for item in matching), inventory_total
                ):
                    return None
        elif stand.stock_m3_ha is not None and not _stocks_reconcile(
            sum(item.stock_m3_ha for item in living_species), stand.stock_m3_ha
        ):
            return None

    return living_species


def _allocate_wfs_species(
    stand: StandRecord, overlap_ha: float
) -> tuple[SpeciesVolume, ...] | None:
    if stand.stock_m3_ha is None:
        return None
    if stand.stock_m3_ha == 0:
        return (SpeciesVolume(None, 0.0),)

    living_species = tuple(item for item in stand.species if _is_living_stock_record(item))
    has_strata = any(item.stratum_code is not None for item in living_species)
    if has_strata and stand.raw_stock_components_m3_ha:
        allocations = []
        for component, stock_m3_ha in stand.raw_stock_components_m3_ha.items():
            if stock_m3_ha == 0:
                continue
            matching = tuple(
                item
                for item in living_species
                if STRATUM_STOCK_COMPONENT.get(item.stratum_code) == component
            )
            component_allocations = _allocate_by_shares(stock_m3_ha * overlap_ha, matching)
            if not component_allocations:
                return None
            allocations.extend(component_allocations)
        allocated_total = sum(item.volume_m3 for item in allocations)
        expected_total = stand.stock_m3_ha * overlap_ha
        if not _stocks_reconcile(allocated_total, expected_total):
            return None
        return tuple(allocations)

    allocations = _allocate_by_shares(stand.stock_m3_ha * overlap_ha, living_species)
    return allocations or None


def _allocate_detail_strata_by_shares(
    total_volume_m3: float, detail_species: tuple[SpeciesRecord, ...]
) -> tuple[SpeciesVolume, ...]:
    """Weight strata by stock, then species by local share; stock is the local fallback."""
    species_by_stratum: dict[str | None, list[SpeciesRecord]] = {}
    for item in detail_species:
        species_by_stratum.setdefault(item.stratum_code, []).append(item)

    stratum_weights = {
        stratum: sum(item.stock_m3_ha for item in items)
        for stratum, items in species_by_stratum.items()
    }
    total_weight = sum(stratum_weights.values())
    if total_weight <= 0:
        return ()

    allocations = []
    for stratum, items in species_by_stratum.items():
        stratum_weight = stratum_weights[stratum]
        if stratum_weight <= 0:
            continue
        stratum_volume = total_volume_m3 * stratum_weight / total_weight
        local_species = tuple(items)
        local_allocations = _allocate_by_shares(stratum_volume, local_species)
        if not local_allocations:
            local_allocations = _allocate_by_weights(
                stratum_volume,
                tuple((item, item.stock_m3_ha) for item in local_species),
            )
        if not local_allocations:
            return ()
        allocations.extend(local_allocations)
    return tuple(allocations)


def estimate_standing_volume(stand: StandRecord, overlap_ha: float) -> StandingVolumeEstimate:
    """Estimate standing stock from inventory sources, never a notice harvest quantity."""
    if not isfinite(overlap_ha) or overlap_ha <= 0:
        return StandingVolumeEstimate(VolumeBasis.UNKNOWN, None, (), False)

    detail_species = _complete_detail_species(stand)
    if detail_species is not None:
        species_volumes = tuple(
            _species_volume(item, item.stock_m3_ha * overlap_ha) for item in detail_species
        )
        return StandingVolumeEstimate(
            VolumeBasis.DETAIL_SPECIES_STOCK,
            sum(item.volume_m3 for item in species_volumes),
            species_volumes,
            True,
        )

    if stand.stock_m3_ha is not None:
        total_volume_m3 = stand.stock_m3_ha * overlap_ha
        species_volumes = _allocate_wfs_species(stand, overlap_ha)
        if species_volumes is not None:
            return StandingVolumeEstimate(
                VolumeBasis.WFS_STAND_STOCK_ALLOCATED,
                total_volume_m3,
                species_volumes,
                True,
            )

    return StandingVolumeEstimate(VolumeBasis.UNKNOWN, None, (), False)


def estimate_planned_harvest_volume(
    notice_volume_m3: float,
    species: tuple[SpeciesRecord, ...] | None = None,
    *,
    stand: StandRecord | None = None,
) -> StandingVolumeEstimate:
    """Allocate a notice's planned harvest volume independently from standing stock."""
    if not isfinite(notice_volume_m3) or notice_volume_m3 < 0:
        return StandingVolumeEstimate(VolumeBasis.UNKNOWN, None, (), False)
    if notice_volume_m3 == 0:
        return StandingVolumeEstimate(
            VolumeBasis.NOTICE_HARVEST_VOLUME,
            0.0,
            (SpeciesVolume(None, 0.0),),
            True,
        )

    allocation = ()
    if stand is not None:
        wfs_species = _allocate_wfs_species(stand, 1.0)
        if wfs_species is not None:
            allocation = _allocate_by_weights(
                notice_volume_m3,
                tuple(
                    (
                        SpeciesRecord(
                            item.species_code,
                            species_name_for_code(item.species_code),
                            None,
                            None,
                            item.inventory_age,
                            item.current_age,
                            item.source_record_id,
                            item.stratum_code,
                        ),
                        item.volume_m3,
                    )
                    for item in wfs_species
                ),
            )
        else:
            detail_species = _complete_detail_species(stand)
            if detail_species is not None:
                allocation = _allocate_detail_strata_by_shares(notice_volume_m3, detail_species)
        if not allocation and stand.stock_m3_ha is None:
            living_species = tuple(item for item in stand.species if _is_living_stock_record(item))
            living_strata = {
                item.stratum_code for item in living_species if item.stratum_code is not None
            }
            if len(living_strata) <= 1:
                allocation = _allocate_by_shares(notice_volume_m3, living_species)
    else:
        candidate_species = tuple(species or ())
        living_species = tuple(item for item in candidate_species if _is_living_stock_record(item))
        complete_stock = living_species and all(
            item.stock_m3_ha is not None for item in living_species
        )
        if len({item.stratum_code for item in living_species if item.stratum_code}) <= 1:
            allocation = _allocate_by_shares(notice_volume_m3, living_species)
        if not allocation and complete_stock:
            allocation = _allocate_detail_strata_by_shares(notice_volume_m3, living_species)

    return StandingVolumeEstimate(
        VolumeBasis.NOTICE_HARVEST_VOLUME,
        notice_volume_m3,
        allocation,
        bool(allocation),
    )


def carbon_from_species_volume(volume_m3: float, species_code: str | None) -> float:
    """Return tonnes of CO2e stored in live-tree biomass for a stem volume."""
    density = density_for_species(species_code)
    dry_stem_t = volume_m3 * density
    total_biomass_t = dry_stem_t * BEF
    carbon_t = total_biomass_t * CARBON_FRACTION
    return carbon_t * CO2_PER_C


def calculate_notice_carbon(
    *,
    standing_species_volumes: list[SpeciesVolume] | tuple[SpeciesVolume, ...],
    planned_harvest_species_volumes: list[SpeciesVolume] | tuple[SpeciesVolume, ...],
) -> NoticeCarbonEstimate:
    """Calculate standing and planned-harvest biomass without combining their meanings."""

    def carbon_total(species_volumes: list[SpeciesVolume] | tuple[SpeciesVolume, ...]):
        if not species_volumes:
            return None
        if any(not isfinite(item.volume_m3) or item.volume_m3 < 0 for item in species_volumes):
            return None
        return sum(
            carbon_from_species_volume(item.volume_m3, item.species_code)
            for item in species_volumes
        )

    return NoticeCarbonEstimate(
        standing_live_biomass_tco2=carbon_total(standing_species_volumes),
        planned_harvest_biomass_tco2=carbon_total(planned_harvest_species_volumes),
    )


def allocate_volume_by_species(total_volume_m3: float, species_rows: list[dict]) -> list[dict]:
    """Allocate a known total volume across species using their percentage shares."""
    rows_with_share = [
        row for row in species_rows if row.get("share") is not None and row["share"] > 0
    ]
    total_share = sum(row["share"] for row in rows_with_share)
    if total_volume_m3 <= 0 or total_share <= 0:
        return []

    allocated = []
    for row in rows_with_share:
        volume_m3 = total_volume_m3 * row["share"] / total_share
        allocated.append(
            {
                **row,
                "volume_m3": volume_m3,
                "carbon_co2e_t": carbon_from_species_volume(volume_m3, row.get("species_code")),
            }
        )
    return allocated


def estimate_intersection_from_notice_volume(
    notice_volume_m3: float,
    overlap_ha: float,
    total_overlap_ha: float,
    species_rows: list[dict],
) -> list[dict]:
    """Prorate a notice's harvest volume to one stand intersection and its species."""
    if notice_volume_m3 <= 0 or overlap_ha <= 0 or total_overlap_ha <= 0:
        return []
    intersection_volume_m3 = notice_volume_m3 * overlap_ha / total_overlap_ha
    return allocate_volume_by_species(intersection_volume_m3, species_rows)


def parse_detail(detail: dict) -> dict:
    """Extract carbon-relevant information from one Forest Register response."""
    elements = detail.get("elemendid") or []
    species_rows = []

    for element in elements:
        species = element.get("puuliigiKood")
        volume_ha = pd.to_numeric(element.get("tagavara"), errors="coerce")
        share = pd.to_numeric(element.get("osakaal"), errors="coerce")
        age = pd.to_numeric(element.get("vanus"), errors="coerce")

        if not pd.isna(volume_ha) and volume_ha < 0:
            continue
        if pd.isna(volume_ha) and (pd.isna(share) or share <= 0):
            continue

        species_rows.append(
            {
                "species_code": species,
                "species_name": species_name_for_code(species),
                "volume_m3_ha": None if pd.isna(volume_ha) else float(volume_ha),
                "share": None if pd.isna(share) else float(share),
                "age": None if pd.isna(age) else float(age),
                "wood_density": density_for_species(species),
            }
        )

    rows_with_volume = [row for row in species_rows if row["volume_m3_ha"] is not None]
    total_volume_ha = sum(row["volume_m3_ha"] for row in rows_with_volume)
    if total_volume_ha > 0:
        weighted_density = (
            sum(row["volume_m3_ha"] * row["wood_density"] for row in rows_with_volume)
            / total_volume_ha
        )
        weighted_age = (
            sum(row["volume_m3_ha"] * (row["age"] or 0) for row in rows_with_volume)
            / total_volume_ha
        )
    else:
        weighted_density = DEFAULT_WOOD_DENSITY
        weighted_age = np.nan

    main_species = None
    if rows_with_volume:
        main_species = max(rows_with_volume, key=lambda row: row["volume_m3_ha"])["species_name"]
    elif species_rows:
        main_species = max(species_rows, key=lambda row: row.get("share") or 0)["species_name"]

    return {
        "stand_id": detail.get("_stand_id"),
        "species_rows": species_rows,
        "volume_m3_ha": total_volume_ha,
        "weighted_density": weighted_density,
        "weighted_age": weighted_age,
        "main_species": main_species,
        "site_class": detail.get("boniteediKood"),
        "site_type": detail.get("kasvukohaKood"),
        "drained": detail.get("kuivendatud"),
    }

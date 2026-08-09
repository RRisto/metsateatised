from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

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


@dataclass(frozen=True)
class StandingVolumeEstimate:
    """A volume estimate with its explicit source and species allocation."""

    basis: VolumeBasis
    total_volume_m3: float | None
    species_volumes: tuple[SpeciesVolume, ...]


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
        covered_by_inventory_ha=("overlap_ha", "sum"),
        weighted_age_num=("weighted_age_num", "sum"),
        weighted_age_den=("weighted_age_den", "sum"),
    )
    aggregated["mean_age"] = aggregated["weighted_age_num"] / aggregated[
        "weighted_age_den"
    ].replace(0, np.nan)
    return aggregated


def density_for_species(code: str | None) -> float:
    if code is None:
        return DEFAULT_WOOD_DENSITY
    return WOOD_DENSITY.get(str(code).strip().upper(), DEFAULT_WOOD_DENSITY)


def species_name_for_code(code: str | None) -> str:
    """Compatibility wrapper for the canonical stand-model species helper."""
    return _species_name_for_code(code)


def _allocate_species_volumes(
    total_volume_m3: float, species: tuple[SpeciesRecord, ...]
) -> tuple[SpeciesVolume, ...]:
    species_with_share = tuple(
        item for item in species if item.share_pct is not None and item.share_pct > 0
    )
    total_share = sum(item.share_pct for item in species_with_share)
    if total_volume_m3 <= 0 or total_share <= 0:
        return ()

    return tuple(
        SpeciesVolume(
            species_code=item.code,
            volume_m3=total_volume_m3 * item.share_pct / total_share,
        )
        for item in species_with_share
    )


def estimate_standing_volume(stand: StandRecord, overlap_ha: float) -> StandingVolumeEstimate:
    """Estimate standing stock from inventory sources, never a notice harvest quantity."""
    if overlap_ha <= 0:
        return StandingVolumeEstimate(VolumeBasis.UNKNOWN, None, ())

    detail_species = tuple(item for item in stand.species if item.stock_m3_ha is not None)
    if detail_species:
        species_volumes = tuple(
            SpeciesVolume(
                species_code=item.code,
                volume_m3=item.stock_m3_ha * overlap_ha,
            )
            for item in detail_species
        )
        return StandingVolumeEstimate(
            VolumeBasis.DETAIL_SPECIES_STOCK,
            sum(item.volume_m3 for item in species_volumes),
            species_volumes,
        )

    if stand.stock_m3_ha is not None:
        total_volume_m3 = stand.stock_m3_ha * overlap_ha
        species_volumes = _allocate_species_volumes(total_volume_m3, stand.species)
        if species_volumes:
            return StandingVolumeEstimate(
                VolumeBasis.WFS_STAND_STOCK_ALLOCATED,
                total_volume_m3,
                species_volumes,
            )

    return StandingVolumeEstimate(VolumeBasis.UNKNOWN, None, ())


def estimate_planned_harvest_volume(
    notice_volume_m3: float, species: tuple[SpeciesRecord, ...]
) -> StandingVolumeEstimate:
    """Allocate a notice's planned harvest volume independently from standing stock."""
    if notice_volume_m3 < 0:
        return StandingVolumeEstimate(VolumeBasis.UNKNOWN, None, ())

    return StandingVolumeEstimate(
        VolumeBasis.NOTICE_HARVEST_VOLUME,
        notice_volume_m3,
        _allocate_species_volumes(notice_volume_m3, species),
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

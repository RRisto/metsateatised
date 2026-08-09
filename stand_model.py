"""Normalized, immutable records for Forest Register stand data."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

SPECIES_NAMES = {
    "0": "Määramata",
    "HB": "Haab",
    "JA": "Jalakas",
    "KD": "Kadakas",
    "KP": "Künnapuu",
    "KS": "Kask",
    "KU": "Kuusk",
    "LH": "Lehis",
    "LM": "Sanglepp",
    "LV": "Hall lepp",
    "MA": "Mänd",
    "NU": "Nulg",
    "PA": "Paju",
    "PI": "Pihlakas",
    "PK": "Paakspuu",
    "PN": "Pärn",
    "PP": "Pappel",
    "RE": "Remmelgas",
    "SA": "Saar",
    "SD": "Seedermänd",
    "SP": "Sarapuu",
    "TA": "Tamm",
    "TL": "Teised lehtpuud",
    "TM": "Toomingas",
    "TO": "Teised okaspuud",
    "TP": "Teised põõsaliigid",
    "TS": "Ebatsuuga",
    "TY": "Türnpuu",
    "VA": "Vaher",
}


def species_name_for_code(code: str | None) -> str:
    """Return the display name used by Forest Register species codes."""
    if code is None or not str(code).strip():
        return "Muu"
    normalized_code = str(code).strip().upper()
    return SPECIES_NAMES.get(normalized_code, f"Muu ({normalized_code})")


@dataclass(frozen=True)
class SpeciesRecord:
    code: str | None
    name: str
    share_pct: float | None
    stock_m3_ha: float | None
    inventory_age: float | None
    current_age: float | None


@dataclass(frozen=True)
class StandRecord:
    stand_id: int | str
    cadastral_reference: str | None
    stand_number: int | None
    inventory_date: date | None
    inventory_age_years: float | None
    area_ha: float | None
    height_m: float | None
    stock_m3_ha: float | None
    raw_stock_components_m3_ha: dict[str, float]
    raw_stock_component_inputs: dict[str, object]
    current_increment_m3_ha_y: float | None
    basal_area_m2_ha: float | None
    stocking_pct: float | None
    site_type: str | None
    site_class: str | None
    drained: bool | None
    development_class: str | None
    species: tuple[SpeciesRecord, ...]


@dataclass(frozen=True)
class IncrementAggregate:
    """Present-state increment totals for the inventory-covered overlap only."""

    current_increment_on_overlap_m3_y: float | None
    current_increment_m3_ha_y: float | None
    current_increment_coverage_pct: float | None
    current_increment_is_complete: bool


def _finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def classify_inventory_recency(inventory_age_years: float | None) -> str:
    """Classify inventory age using the documented project recency bands."""
    age = _finite_float(inventory_age_years)
    if age is None or age < 0:
        return "teadmata"
    if age < 3:
        return "väga hea"
    if age < 6:
        return "hea"
    if age < 9:
        return "vananev"
    return "nõrk"


def classify_spatial_coverage(spatial_coverage_pct: float | None) -> str:
    """Classify inventory spatial coverage without combining it with other dimensions."""
    coverage = _finite_float(spatial_coverage_pct)
    if coverage is None:
        return "teadmata"
    if coverage >= 90:
        return "hea"
    if coverage >= 50:
        return "osaline"
    return "nõrk"


def aggregate_increment(rows: Iterable[Mapping[str, object]]) -> IncrementAggregate:
    """Aggregate reported current increment by overlap area, never into a trajectory."""
    total_increment_m3_y = 0.0
    increment_area_ha = 0.0
    total_overlap_ha = 0.0
    for row in rows:
        overlap_ha = _finite_float(row.get("overlap_ha"))
        increment_m3_ha_y = _finite_float(row.get("increment_m3_ha_y"))
        if overlap_ha is None or overlap_ha <= 0:
            continue
        total_overlap_ha += overlap_ha
        if increment_m3_ha_y is None:
            continue
        total_increment_m3_y += increment_m3_ha_y * overlap_ha
        increment_area_ha += overlap_ha

    if total_overlap_ha == 0:
        return IncrementAggregate(None, None, None, False)
    coverage_pct = 100 * increment_area_ha / total_overlap_ha
    if increment_area_ha != total_overlap_ha:
        return IncrementAggregate(None, None, coverage_pct, False)
    return IncrementAggregate(
        current_increment_on_overlap_m3_y=total_increment_m3_y,
        current_increment_m3_ha_y=total_increment_m3_y / increment_area_ha,
        current_increment_coverage_pct=coverage_pct,
        current_increment_is_complete=True,
    )


def _integer(value: object) -> int | None:
    number = _finite_float(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _stand_id(value: object) -> int | str:
    integer = _integer(value)
    if integer is not None:
        return integer
    if value is None:
        return ""
    return str(value)


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _text(value)
    if text is None:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        try:
            return date.fromisoformat(normalized)
        except ValueError:
            return None


def _bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = _text(value)
    if text is None:
        return None
    if text.lower() in {"true", "t", "yes", "y", "1"}:
        return True
    if text.lower() in {"false", "f", "no", "n", "0"}:
        return False
    return None


def _detail_elements(detail: Mapping[str, object] | None) -> Iterable[Mapping[str, object]]:
    if not detail:
        return ()
    elements = detail.get("elemendid")
    if not isinstance(elements, Iterable) or isinstance(elements, (str, bytes)):
        return ()
    return (element for element in elements if isinstance(element, Mapping))


def normalize_species_records(detail: Mapping[str, object] | None) -> tuple[SpeciesRecord, ...]:
    """Normalize the per-species elements supplied by a detail response."""
    records = []
    for element in _detail_elements(detail):
        stock = _finite_float(element.get("tagavara"))
        if stock is not None and stock < 0:
            continue
        code = _text(element.get("puuliigiKood"))
        normalized_code = code.upper() if code is not None else None
        records.append(
            SpeciesRecord(
                code=normalized_code,
                name=species_name_for_code(normalized_code),
                share_pct=_finite_float(element.get("osakaal")),
                stock_m3_ha=stock,
                inventory_age=_finite_float(element.get("vanus")),
                current_age=_finite_float(element.get("jooksevVanus")),
            )
        )
    return tuple(records)


def build_stand_record(
    wfs_row: Mapping[str, object],
    detail: Mapping[str, object] | None,
    as_of_date: date,
) -> StandRecord:
    """Merge a WFS stand row and its optional detail response into one typed record."""
    inventory_date = _date(wfs_row.get("invent_kp"))
    inventory_age_years = (
        (as_of_date - inventory_date).days / 365.25 if inventory_date is not None else None
    )

    stock_components = {}
    raw_stock_component_inputs = {}
    for output_name, field_name in (
        ("layer_1", "tagavara_1_ha"),
        ("layer_2", "tagavara_2_ha"),
        ("young", "tagavara_y_ha"),
    ):
        raw_value = wfs_row.get(field_name)
        raw_stock_component_inputs[field_name] = raw_value
        value = _finite_float(raw_value)
        if value is not None and value >= 0:
            stock_components[output_name] = value

    return StandRecord(
        stand_id=_stand_id(wfs_row.get("id")),
        cadastral_reference=_text(wfs_row.get("katastri_nr")),
        stand_number=_integer(wfs_row.get("eraldise_nr")),
        inventory_date=inventory_date,
        inventory_age_years=inventory_age_years,
        area_ha=_finite_float(wfs_row.get("pindala")),
        height_m=_finite_float(wfs_row.get("korgus")),
        stock_m3_ha=sum(stock_components.values()) if stock_components else None,
        raw_stock_components_m3_ha=stock_components,
        raw_stock_component_inputs=raw_stock_component_inputs,
        current_increment_m3_ha_y=_finite_float(wfs_row.get("juurdekasv")),
        basal_area_m2_ha=_finite_float(wfs_row.get("rpindala_1")),
        stocking_pct=_finite_float(wfs_row.get("taius_1")),
        site_type=_text(wfs_row.get("kasvukoht_kood")),
        site_class=_text(wfs_row.get("boniteedi_kood")),
        drained=_bool(wfs_row.get("kuivendatud")),
        development_class=_text(wfs_row.get("arengukl_kood")),
        species=normalize_species_records(detail),
    )

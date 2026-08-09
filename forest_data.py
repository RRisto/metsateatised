from __future__ import annotations

from collections.abc import Callable
from math import ceil

import geopandas as gpd
import pandas as pd


def stand_cql_filter(cadastral_reference: str, stand_number: object) -> str:
    escaped_reference = str(cadastral_reference).replace("'", "''")
    normalized_number = int(float(stand_number))
    return f"katastri_nr = '{escaped_reference}' AND eraldise_nr = {normalized_number}"


def _notice_bbox(notice: gpd.GeoDataFrame, pad_degrees: float = 0.002):
    min_x, min_y, max_x, max_y = notice.total_bounds
    return (
        min_x - pad_degrees,
        min_y - pad_degrees,
        max_x + pad_degrees,
        max_y + pad_degrees,
    )


def load_stands_for_notices(
    notices: gpd.GeoDataFrame,
    *,
    exact_loader: Callable[[str], gpd.GeoDataFrame],
    bbox_loader: Callable[[tuple[float, float, float, float]], gpd.GeoDataFrame],
    batch_size: int = 50,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> gpd.GeoDataFrame:
    """Load each notice's referenced stand, falling back to only that notice's bbox."""
    frames = []
    references = []
    for _, row in notices.iterrows():
        cadastral_reference = row.get("katastri_nr")
        stand_number = row.get("eraldise_nr")
        if pd.notna(cadastral_reference) and pd.notna(stand_number):
            reference = (str(cadastral_reference), int(float(stand_number)))
            if reference not in references:
                references.append(reference)

    matched_references = set()
    batch_count = ceil(len(references) / batch_size) if references else 0
    for batch_index, start in enumerate(range(0, len(references), batch_size), start=1):
        batch = references[start : start + batch_size]
        filters = [stand_cql_filter(reference, number) for reference, number in batch]
        cql_filter = (
            filters[0] if len(filters) == 1 else " OR ".join(f"({item})" for item in filters)
        )
        stands = exact_loader(cql_filter)
        if not stands.empty:
            frames.append(stands)
            if {"katastri_nr", "eraldise_nr"}.issubset(stands.columns):
                matched_references.update(
                    (str(row["katastri_nr"]), int(float(row["eraldise_nr"])))
                    for _, row in stands.iterrows()
                    if pd.notna(row["katastri_nr"]) and pd.notna(row["eraldise_nr"])
                )
            elif len(batch) == 1:
                matched_references.add(batch[0])
        if progress_callback:
            progress_callback(batch_index, batch_count, "exact")

    fallback_notices = []
    for index in notices.index:
        row = notices.loc[index]
        cadastral_reference = row.get("katastri_nr")
        stand_number = row.get("eraldise_nr")
        reference = None
        if pd.notna(cadastral_reference) and pd.notna(stand_number):
            reference = (str(cadastral_reference), int(float(stand_number)))
        if reference not in matched_references:
            fallback_notices.append(index)

    for fallback_index, index in enumerate(fallback_notices, start=1):
        stands = bbox_loader(_notice_bbox(notices.loc[[index]]))
        if not stands.empty:
            frames.append(stands)
        if progress_callback:
            progress_callback(fallback_index, len(fallback_notices), "fallback")

    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    combined = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs=frames[0].crs)
    if "id" in combined.columns:
        return combined.drop_duplicates(subset=["id"]).reset_index(drop=True)
    geometry_column = combined.geometry.name
    geometry_keys = combined[geometry_column].apply(
        lambda geometry: geometry.wkb_hex if geometry is not None else None
    )
    return combined.loc[~geometry_keys.duplicated()].reset_index(drop=True)

"""Reusable forest-notice stand enrichment and biomass/carbon analysis."""

import asyncio
import re
from collections.abc import Iterable
from datetime import date, timedelta

import aiohttp
import geopandas as gpd
import nest_asyncio
import numpy as np
import pandas as pd
from shapely.ops import unary_union

from carbon import (
    VolumeBasis,
    aggregate_intersections,
    calculate_notice_carbon,
    carbon_from_species_volume,
    estimate_planned_harvest_volume,
    estimate_standing_volume,
    species_name_for_code,
)
from data_cache import DEFAULT_CACHE_ROOT, read_json_cache, write_json_cache
from stand_model import (
    aggregate_increment,
    build_stand_record,
    classify_inventory_recency,
    classify_spatial_coverage,
)

DETAIL_URL = "https://register.metsad.ee/portaal/api/rest/eraldis/detail"
INTERSECTION_AREA_TOLERANCE_M2 = 1e-6
ANALYSIS_MODEL_VERSION = "estonia-bcef-v1"


def _clean_col(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def first_matching_column(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    norm = {_clean_col(c): c for c in columns}
    for cand in candidates:
        cc = _clean_col(cand)
        if cc in norm:
            return norm[cc]
    for cand in candidates:
        cc = _clean_col(cand)
        for nc, original in norm.items():
            if len(cc) >= 5 and cc in nc:
                return original
    return None


def choose_stand_id_column(df: pd.DataFrame) -> str | None:
    return first_matching_column(df.columns, ["eraldis_id", "eraldisid", "id", "stand_id"])


def normalize_stand_id(value):
    if pd.isna(value):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return str(value)


def _positive_area_polygonal_part(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if geometry.geom_type in {"Polygon", "MultiPolygon"}:
        polygonal = geometry
    elif geometry.geom_type == "GeometryCollection":
        parts = [
            part
            for item in geometry.geoms
            if (part := _positive_area_polygonal_part(item)) is not None
        ]
        polygonal = unary_union(parts) if parts else None
    else:
        polygonal = None
    if polygonal is None or polygonal.area <= INTERSECTION_AREA_TOLERANCE_M2:
        return None
    return polygonal


async def _fetch_detail_one(session: aiohttp.ClientSession, stand_id) -> dict | None:
    cached = read_json_cache(
        "details",
        str(stand_id),
        max_age=timedelta(days=30),
        cache_root=DEFAULT_CACHE_ROOT,
    )
    if isinstance(cached, dict):
        cached["_stand_id"] = stand_id
        return cached

    try:
        async with session.get(
            f"{DETAIL_URL}/{stand_id}", timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            data["_stand_id"] = stand_id
            write_json_cache(
                "details",
                str(stand_id),
                data,
                cache_root=DEFAULT_CACHE_ROOT,
            )
            return data
    except (TimeoutError, aiohttp.ClientError):
        return None


async def _fetch_detail_batch(
    stand_ids: list,
    n_workers: int = 10,
    progress_callback=None,
) -> list[dict]:
    sem = asyncio.Semaphore(n_workers)
    connector = aiohttp.TCPConnector(limit=n_workers)
    async with aiohttp.ClientSession(connector=connector) as session:

        async def worker(sid):
            async with sem:
                return await _fetch_detail_one(session, sid)

        tasks = [asyncio.create_task(worker(sid)) for sid in stand_ids]
        rows = []
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            rows.append(await task)
            if progress_callback:
                progress_callback(completed, len(tasks))
    return [x for x in rows if x is not None]


def fetch_stand_details(
    stand_ids_tuple: tuple,
    n_workers: int = 10,
    progress_callback=None,
) -> list[dict]:
    if not stand_ids_tuple:
        return []
    nest_asyncio.apply()
    return asyncio.run(
        _fetch_detail_batch(
            list(stand_ids_tuple),
            n_workers=n_workers,
            progress_callback=progress_callback,
        )
    )


def analyze_notices(
    notices: gpd.GeoDataFrame,
    stands: gpd.GeoDataFrame,
    detail_progress_callback=None,
    detail_loader=fetch_stand_details,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    if notices.empty:
        return notices, pd.DataFrame()
    if stands.empty:
        raise RuntimeError("Raiealade ümbrusest ei leitud eraldisi.")

    n = notices.to_crs(3301).copy()
    s = stands.to_crs(3301).copy()
    n["notice_ix"] = np.arange(len(n))
    s["stand_ix"] = np.arange(len(s))

    stand_id_col = choose_stand_id_column(s)
    if not stand_id_col:
        raise RuntimeError("Eraldiste kihist ei õnnestunud eraldise ID välja tuvastada.")

    intersections = gpd.overlay(
        n[["notice_ix", "geometry"]],
        s[["stand_ix", stand_id_col, "geometry"]],
        how="intersection",
        keep_geom_type=False,
    )
    intersections["geometry"] = intersections.geometry.apply(_positive_area_polygonal_part)
    intersections = intersections.loc[intersections.geometry.notna()].copy()

    if intersections.empty:
        out = n.copy()
        out["area_ha"] = out.geometry.area / 10000
        out["standing_live_biomass_tco2"] = np.nan
        out["standing_live_biomass_tco2_ha"] = np.nan
        out["planned_harvest_biomass_tco2"] = np.nan
        out["mean_age"] = np.nan
        out["mean_current_age_years"] = np.nan
        out["standing_volume_basis"] = VolumeBasis.UNKNOWN.value
        out["planned_harvest_volume_basis"] = VolumeBasis.UNKNOWN.value
        out["standing_biomass_is_complete"] = False
        out["planned_harvest_biomass_is_complete"] = False
        out["spatial_coverage_pct"] = 0.0
        out["spatial_coverage_quality"] = "nõrk"
        out["inventory_date"] = None
        out["inventory_age_years"] = np.nan
        out["inventory_recency"] = "teadmata"
        out["volume_source_quality"] = VolumeBasis.UNKNOWN.value
        out["current_increment_m3_ha_y"] = np.nan
        out["current_increment_on_overlap_m3_y"] = np.nan
        out["current_increment_covered_area_ha"] = 0.0
        out["current_increment_coverage_pct"] = np.nan
        out["current_increment_is_complete"] = False
        return out.to_crs(4326), pd.DataFrame()

    intersections["overlap_ha"] = intersections.geometry.area / 10000
    intersections["_join_stand_id"] = intersections[stand_id_col].apply(normalize_stand_id)
    stand_ids = tuple(sorted({x for x in intersections["_join_stand_id"].dropna()}))

    details_raw = detail_loader(
        stand_ids,
        progress_callback=detail_progress_callback,
    )
    detail_by_id = {normalize_stand_id(d["_stand_id"]): d for d in details_raw}
    stand_rows_by_ix = {
        int(stand_ix): stand_row.to_dict()
        for stand_ix, stand_row in s.set_index("stand_ix").iterrows()
    }

    notice_volume_col = first_matching_column(
        n.columns, ["raiutav_maht", "raie_maht", "harvest_volume"]
    )
    notice_volume_by_ix = (
        dict(zip(n["notice_ix"], pd.to_numeric(n[notice_volume_col], errors="coerce"), strict=True))
        if notice_volume_col
        else {}
    )
    total_overlap_by_notice = intersections.groupby("notice_ix")["overlap_ha"].sum().to_dict()

    species_breakdown_rows = []
    intersection_rows = []
    inventory_metric_rows = []
    for _, row in intersections.iterrows():
        sid = row["_join_stand_id"]
        stand_row = stand_rows_by_ix[int(row["stand_ix"])]
        if "id" not in stand_row:
            stand_row["id"] = stand_row[stand_id_col]
        stand = build_stand_record(stand_row, detail_by_id.get(sid), as_of_date=date.today())
        standing_volume = estimate_standing_volume(stand, float(row["overlap_ha"]))

        notice_volume = notice_volume_by_ix.get(row["notice_ix"])
        if pd.notna(notice_volume):
            planned_harvest_volume = estimate_planned_harvest_volume(
                float(notice_volume)
                * float(row["overlap_ha"])
                / total_overlap_by_notice[row["notice_ix"]],
                stand=stand,
            )
        else:
            planned_harvest_volume = estimate_planned_harvest_volume(float("nan"), stand=stand)

        carbon = calculate_notice_carbon(
            standing_species_volumes=standing_volume.species_volumes,
            planned_harvest_species_volumes=planned_harvest_volume.species_volumes,
        )
        age_num = 0.0
        age_den = 0.0
        current_age_num = 0.0
        current_age_den = 0.0
        for estimate_scope, species_volumes in (
            ("standing", standing_volume.species_volumes),
            ("planned_harvest", planned_harvest_volume.species_volumes),
        ):
            for species_volume in species_volumes:
                species_carbon = carbon_from_species_volume(
                    species_volume.volume_m3,
                    species_volume.species_code,
                )
                if estimate_scope == "standing" and species_volume.inventory_age is not None:
                    age_num += species_volume.inventory_age * species_volume.volume_m3
                    age_den += species_volume.volume_m3
                if estimate_scope == "standing" and species_volume.current_age is not None:
                    current_age_num += species_volume.current_age * species_volume.volume_m3
                    current_age_den += species_volume.volume_m3

                species_breakdown_rows.append(
                    {
                        "notice_ix": row["notice_ix"],
                        "stand_id": sid,
                        "source_record_id": species_volume.source_record_id,
                        "stratum_code": species_volume.stratum_code,
                        "estimate_scope": estimate_scope,
                        "species_code": species_volume.species_code,
                        "species": species_name_for_code(species_volume.species_code),
                        "overlap_ha": row["overlap_ha"],
                        "volume_m3": species_volume.volume_m3,
                        "biomass_tco2": species_carbon,
                        "age": species_volume.inventory_age,
                        "current_age": species_volume.current_age,
                        "site_class": stand.site_class,
                        "site_type": stand.site_type,
                        "drained": stand.drained,
                    }
                )

        intersection_rows.append(
            {
                "notice_ix": row["notice_ix"],
                "overlap_ha": row["overlap_ha"],
                "standing_live_biomass_tco2": carbon.standing_live_biomass_tco2,
                "planned_harvest_biomass_tco2": carbon.planned_harvest_biomass_tco2,
                "standing_volume_basis": standing_volume.basis.value,
                "planned_harvest_volume_basis": planned_harvest_volume.basis.value,
                "standing_biomass_is_complete": standing_volume.is_complete,
                "planned_harvest_biomass_is_complete": planned_harvest_volume.is_complete,
                "weighted_age_num": age_num,
                "weighted_age_den": age_den,
                "weighted_current_age_num": current_age_num,
                "weighted_current_age_den": current_age_den,
            }
        )
        inventory_metric_rows.append(
            {
                "notice_ix": row["notice_ix"],
                "overlap_ha": float(row["overlap_ha"]),
                "inventory_date": stand.inventory_date,
                "inventory_age_years": stand.inventory_age_years,
                "increment_m3_ha_y": stand.current_increment_m3_ha_y,
            }
        )

    agg = aggregate_intersections(intersection_rows)
    if agg.empty:
        raise RuntimeError("Eraldiste detailandmeid ei õnnestunud laadida.")
    current_age_rows = pd.DataFrame(intersection_rows)
    current_age = current_age_rows.groupby("notice_ix", as_index=False).agg(
        weighted_current_age_num=("weighted_current_age_num", "sum"),
        weighted_current_age_den=("weighted_current_age_den", "sum"),
    )
    current_age["mean_current_age_years"] = current_age["weighted_current_age_num"] / current_age[
        "weighted_current_age_den"
    ].replace(0, np.nan)
    agg = agg.merge(
        current_age[["notice_ix", "mean_current_age_years"]],
        on="notice_ix",
        how="left",
    )

    species_df = pd.DataFrame(species_breakdown_rows)
    if not species_df.empty:
        species_df["scope_rank"] = species_df["estimate_scope"].eq("planned_harvest")
        dom = (
            species_df.groupby(["notice_ix", "estimate_scope", "species"], as_index=False)[
                "volume_m3"
            ]
            .sum()
            .merge(
                species_df[["notice_ix", "estimate_scope", "scope_rank"]].drop_duplicates(),
                on=["notice_ix", "estimate_scope"],
                how="left",
            )
            .sort_values(
                ["notice_ix", "scope_rank", "volume_m3"],
                ascending=[True, True, False],
            )
            .drop_duplicates("notice_ix")
            .rename(columns={"species": "dominant_species"})[["notice_ix", "dominant_species"]]
        )
        species_df = species_df.drop(columns="scope_rank")
        agg = agg.merge(dom, on="notice_ix", how="left")

    out = n.merge(agg, on="notice_ix", how="left")
    out["area_ha"] = out.geometry.area / 10000
    out["spatial_coverage_pct"] = (
        100 * out["covered_by_inventory_ha"] / out["area_ha"].replace(0, np.nan)
    )
    out["standing_live_biomass_tco2_ha"] = out["standing_live_biomass_tco2"] / out[
        "area_ha"
    ].replace(0, np.nan)
    out["standing_volume_basis"] = out["standing_volume_basis"].fillna(VolumeBasis.UNKNOWN.value)
    out["planned_harvest_volume_basis"] = out["planned_harvest_volume_basis"].fillna(
        VolumeBasis.UNKNOWN.value
    )
    out["spatial_coverage_quality"] = out["spatial_coverage_pct"].apply(classify_spatial_coverage)
    out["volume_source_quality"] = out["standing_volume_basis"]

    inventory_metrics = pd.DataFrame(inventory_metric_rows)
    notice_inventory_metrics = []
    for notice_ix, rows in inventory_metrics.groupby("notice_ix"):
        known_ages = rows.dropna(subset=["inventory_age_years", "inventory_date"])
        if len(known_ages) != len(rows):
            inventory_age_years = np.nan
            inventory_date = None
        else:
            oldest_inventory = known_ages.loc[known_ages["inventory_date"].idxmin()]
            inventory_date = oldest_inventory["inventory_date"]
            inventory_age_years = oldest_inventory["inventory_age_years"]

        recency = (
            classify_inventory_recency(inventory_age_years)
            if inventory_date is not None
            else "teadmata"
        )
        increment = aggregate_increment(rows.to_dict("records"))
        notice_inventory_metrics.append(
            {
                "notice_ix": notice_ix,
                "inventory_date": inventory_date,
                "inventory_age_years": inventory_age_years,
                "inventory_recency": recency,
                "current_increment_m3_ha_y": increment.current_increment_m3_ha_y,
                "current_increment_on_overlap_m3_y": increment.current_increment_on_overlap_m3_y,
                "current_increment_covered_area_ha": (increment.current_increment_covered_area_ha),
                "current_increment_coverage_pct": increment.current_increment_coverage_pct,
                "current_increment_is_complete": increment.current_increment_is_complete,
            }
        )
    out = out.merge(pd.DataFrame(notice_inventory_metrics), on="notice_ix", how="left")
    return out.to_crs(4326), species_df

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Iterable
from datetime import date, timedelta

import aiohttp
import folium
import geopandas as gpd
import nest_asyncio
import numpy as np
import pandas as pd
import requests
import streamlit as st
from shapely.ops import unary_union
from streamlit_folium import st_folium

from analysis_cache import read_analysis_cache, write_analysis_cache
from carbon import (
    VolumeBasis,
    aggregate_intersections,
    calculate_notice_carbon,
    carbon_from_species_volume,
    estimate_planned_harvest_volume,
    estimate_standing_volume,
    species_name_for_code,
)
from data_cache import DEFAULT_CACHE_ROOT, clear_data_cache, read_json_cache, write_json_cache
from forest_data import load_stands_for_notices as resolve_stands_for_notices
from stand_model import (
    aggregate_increment,
    build_stand_record,
    classify_inventory_recency,
    classify_spatial_coverage,
)
from wfs import fetch_wfs_features

# -----------------------------------------------------------------------------
# Public Estonian Forest Register endpoints
# -----------------------------------------------------------------------------
WFS_URL = "https://gsavalik.envir.ee/geoserver/metsaregister/ows"
DETAIL_URL = "https://register.metsad.ee/portaal/api/rest/eraldis/detail"
LAYERS = {
    "current_notices": "metsaregister:teatis",
    "archive_notices": "metsaregister:teatis_arhiiv",
    "stands": "metsaregister:eraldis",
}
INTERSECTION_AREA_TOLERANCE_M2 = 1e-6
ANALYSIS_MODEL_VERSION = "estonia-bcef-v1"

st.set_page_config(page_title="Metsateatiste biomassi süsinik MVP", layout="wide")


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


def wfs_get(
    type_name: str,
    count: int | None = None,
    cql_filter: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> gpd.GeoDataFrame:
    feats = fetch_wfs_features(
        type_name,
        max_features=count,
        cql_filter=cql_filter,
        bbox=bbox,
        cache_root=DEFAULT_CACHE_ROOT,
    )
    if not feats:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")


@st.cache_data(ttl=3600, show_spinner=False)
def sample_layer(type_name: str, n: int = 5) -> gpd.GeoDataFrame:
    return wfs_get(type_name, count=n)


def detect_date_column(gdf: gpd.GeoDataFrame) -> str | None:
    candidates = [
        "kuupaev",
        "kuupäev",
        "esitamise_kuupaev",
        "esitatud",
        "esitamise_aeg",
        "registreerimise_kuupaev",
        "registreeritud",
        "otsuse_kuupaev",
        "kehtiv_alates",
        "alguskuupaev",
        "created",
        "date",
    ]
    col = first_matching_column(gdf.columns, candidates)
    if col:
        return col

    best, best_rate = None, 0.0
    for c in gdf.columns:
        if c == gdf.geometry.name:
            continue
        vals = gdf[c].dropna().astype(str).head(20)
        if vals.empty:
            continue
        parsed = pd.to_datetime(vals, errors="coerce", utc=True)
        rate = parsed.notna().mean()
        if rate > 0.8 and rate > best_rate:
            best, best_rate = c, rate
    return best


@st.cache_data(ttl=1800, show_spinner=False)
def load_notices(start: date, end: date, max_features: int = 10000) -> gpd.GeoDataFrame:
    frames, errors = [], []
    for key in ["archive_notices", "current_notices"]:
        layer = LAYERS[key]
        try:
            sample = sample_layer(layer)
            date_col = detect_date_column(sample)
            if not date_col:
                raise RuntimeError(f"Ei suutnud kihis {layer} kuupäevavälja tuvastada")

            cql = (
                f"{date_col} >= '{start.isoformat()}' AND "
                f"{date_col} < '{(end + timedelta(days=1)).isoformat()}'"
            )
            try:
                gdf = wfs_get(layer, count=max_features, cql_filter=cql)
            except requests.RequestException:
                gdf = wfs_get(layer, count=max_features)
                if date_col in gdf:
                    dt = pd.to_datetime(gdf[date_col], errors="coerce").dt.date
                    gdf = gdf[(dt >= start) & (dt <= end)].copy()

            gdf["_source_layer"] = layer
            gdf["_date_col"] = date_col
            frames.append(gdf)
        except Exception as e:
            errors.append(f"{layer}: {e}")

    if not frames:
        raise RuntimeError("Metsateatisi ei õnnestunud laadida. " + " | ".join(errors))

    combined = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(combined, geometry="geometry", crs="EPSG:4326")

    id_col = first_matching_column(
        gdf.columns, ["teatis_id", "teatise_id", "id", "dokumendi_id", "number", "teatise_nr"]
    )
    if id_col:
        gdf = gdf.drop_duplicates(subset=[id_col], keep="last")
    else:
        gdf["_geom_wkb"] = gdf.geometry.apply(lambda x: x.wkb_hex if x is not None else None)
        gdf = gdf.drop_duplicates(subset=["_geom_wkb"]).drop(columns=["_geom_wkb"])
    return gdf


def bbox_of(gdf: gpd.GeoDataFrame, pad_deg: float = 0.002):
    minx, miny, maxx, maxy = gdf.total_bounds
    return minx - pad_deg, miny - pad_deg, maxx + pad_deg, maxy + pad_deg


@st.cache_data(ttl=3600, show_spinner=False)
def load_stands_for_bbox(
    bbox_tuple: tuple[float, float, float, float], max_features: int = 5_000
) -> gpd.GeoDataFrame:
    return wfs_get(LAYERS["stands"], count=max_features, bbox=bbox_tuple)


@st.cache_data(ttl=3600, show_spinner=False)
def load_stands_for_filter(cql_filter: str) -> gpd.GeoDataFrame:
    return wfs_get(LAYERS["stands"], count=500, cql_filter=cql_filter)


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


def analyze(
    notices: gpd.GeoDataFrame,
    stands: gpd.GeoDataFrame,
    detail_progress_callback=None,
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

    details_raw = fetch_stand_details(
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


def likely_harvest_type_column(gdf):
    return first_matching_column(
        gdf.columns, ["raie_liik", "raieliik", "raie_kood", "tegevus", "harvest_type"]
    )


def likely_notice_id_column(gdf):
    return first_matching_column(
        gdf.columns, ["teatis_id", "teatise_id", "id", "teatise_nr", "number"]
    )


def build_export_table(results: pd.DataFrame) -> pd.DataFrame:
    """Select the stable, user-facing result schema in display order."""
    id_col = likely_notice_id_column(results)
    harvest_type_col = likely_harvest_type_column(results)
    requested_columns = [
        id_col,
        "raiutav_maht",
        harvest_type_col,
        "area_ha",
        "dominant_species",
        "mean_age",
        "mean_current_age_years",
        "standing_live_biomass_tco2",
        "standing_live_biomass_tco2_ha",
        "planned_harvest_biomass_tco2",
        "standing_volume_basis",
        "planned_harvest_volume_basis",
        "standing_biomass_is_complete",
        "planned_harvest_biomass_is_complete",
        "inventory_date",
        "inventory_age_years",
        "inventory_recency",
        "spatial_coverage_pct",
        "spatial_coverage_quality",
        "volume_source_quality",
        "current_increment_m3_ha_y",
        "current_increment_on_overlap_m3_y",
        "current_increment_covered_area_ha",
        "current_increment_coverage_pct",
        "current_increment_is_complete",
    ]
    columns = []
    for column in requested_columns:
        if column and column in results and column not in columns:
            columns.append(column)
    return pd.DataFrame(results[columns]).rename(columns={"mean_age": "mean_inventory_age_years"})


def fmt_num(x, digits=0):
    if pd.isna(x):
        return "–"
    return f"{x:,.{digits}f}".replace(",", " ")


MAP_COLOR_MODES = ("Süsinikuvaru", "Raieliik")
CUTTING_TYPE_COLORS = (
    "#1f78b4",
    "#33a02c",
    "#e31a1c",
    "#ff7f00",
    "#6a3d9a",
    "#b15928",
    "#a6cee3",
    "#b2df8a",
)
MISSING_MAP_COLOR = "#777777"


def _add_map_legend(m: folium.Map, title: str, entries: list[tuple[str, str]]) -> None:
    items = "".join(
        '<div style="display:flex;align-items:center;gap:6px;margin-top:4px">'
        f'<span style="background:{color};width:14px;height:14px;display:inline-block;'
        'border:1px solid #555"></span>'
        f"<span>{html.escape(label)}</span></div>"
        for label, color in entries
    )
    legend = (
        '<div style="position:fixed;bottom:28px;left:28px;z-index:9999;'
        'background:white;padding:10px 12px;border:1px solid #aaa;border-radius:4px;'
        'font-size:13px;box-shadow:0 1px 4px rgba(0,0,0,.25)">'
        f"<strong>{html.escape(title)}</strong>{items}</div>"
    )
    m.get_root().html.add_child(folium.Element(legend))


def make_map(results: gpd.GeoDataFrame, color_mode: str = "Süsinikuvaru"):
    if results.empty:
        return folium.Map(location=[58.6, 25.0], zoom_start=7)

    cent = results.to_crs(3301).geometry.union_all().centroid
    center = gpd.GeoSeries([cent], crs=3301).to_crs(4326).iloc[0]
    m = folium.Map(location=[center.y, center.x], zoom_start=8, tiles="CartoDB positron")

    vals = results["standing_live_biomass_tco2"].replace([np.inf, -np.inf], np.nan).dropna()
    q1 = vals.quantile(0.33) if len(vals) else 0
    q2 = vals.quantile(0.66) if len(vals) else 0

    id_col = likely_notice_id_column(results)
    harvest_col = likely_harvest_type_column(results)
    date_col = None
    if "_date_col" in results and len(results):
        date_col = (
            results["_date_col"].dropna().iloc[0] if results["_date_col"].notna().any() else None
        )

    def carbon_color(v):
        if pd.isna(v):
            return MISSING_MAP_COLOR
        if v <= q1:
            return "#2ca25f"
        if v <= q2:
            return "#fec44f"
        return "#de2d26"

    cutting_types = []
    if harvest_col:
        cutting_types = sorted(
            {str(value).strip() for value in results[harvest_col].dropna() if str(value).strip()}
        )
    cutting_type_colors = {
        value: CUTTING_TYPE_COLORS[index % len(CUTTING_TYPE_COLORS)]
        for index, value in enumerate(cutting_types)
    }

    if color_mode == "Raieliik":
        legend_entries = [(value, cutting_type_colors[value]) for value in cutting_types]
        if not harvest_col or results[harvest_col].isna().any():
            legend_entries.append(("Puudub / teadmata", MISSING_MAP_COLOR))
        _add_map_legend(m, "Raieliik", legend_entries)
    else:
        _add_map_legend(
            m,
            "Elusbiomassi süsinikuvaru",
            [
                (f"Madal (≤ {fmt_num(q1, 0)} t CO₂e)", "#2ca25f"),
                (f"Keskmine (≤ {fmt_num(q2, 0)} t CO₂e)", "#fec44f"),
                (f"Kõrge (> {fmt_num(q2, 0)} t CO₂e)", "#de2d26"),
                ("Puudub / teadmata", MISSING_MAP_COLOR),
            ],
        )

    features = []
    for _, row in results.iterrows():
        popup = [
            f"<b>Metsateatis</b>: {row.get(id_col, '–') if id_col else '–'}",
            f"Pindala: {fmt_num(row.get('area_ha'), 2)} ha",
            f"Valdav puuliik: {row.get('dominant_species', '–')}",
            f"Keskmine inventuurivanus: {fmt_num(row.get('mean_age'), 0)} a",
            "Elusbiomassi süsinikuvaru: "
            f"{fmt_num(row.get('standing_live_biomass_tco2'), 0)} t CO₂e",
            "Kavandatava raiemahu biomass: "
            f"{fmt_num(row.get('planned_harvest_biomass_tco2'), 0)} t CO₂e",
            f"Elusbiomassi mahu alus: {row.get('standing_volume_basis', 'andmed puuduvad')}",
            "Kavandatava raiemahu alus: "
            f"{row.get('planned_harvest_volume_basis', 'andmed puuduvad')}",
            f"Inventuuri kuupäev: {row.get('inventory_date', '–')}",
            "Inventuuri vanus ja värskus: "
            f"{fmt_num(row.get('inventory_age_years'), 2)} a · "
            f"{row.get('inventory_recency', 'teadmata')}",
            "Ruumiline andmekate: "
            f"{fmt_num(row.get('spatial_coverage_pct'), 0)}% · "
            f"{row.get('spatial_coverage_quality', 'teadmata')}",
            "Jooksev juurdekasv: "
            f"{fmt_num(row.get('current_increment_m3_ha_y'), 1)} m³/ha/a · "
            f"{fmt_num(row.get('current_increment_on_overlap_m3_y'), 1)} m³/a",
        ]
        if harvest_col:
            popup.insert(1, f"Raieliik: {row.get(harvest_col, '–')}")
        if date_col and date_col in row:
            popup.insert(1, f"Kuupäev: {row.get(date_col, '–')}")

        if color_mode == "Raieliik" and harvest_col:
            harvest_type = row.get(harvest_col)
            feature_color = (
                cutting_type_colors.get(str(harvest_type).strip(), MISSING_MAP_COLOR)
                if pd.notna(harvest_type) and str(harvest_type).strip()
                else MISSING_MAP_COLOR
            )
        elif color_mode == "Raieliik":
            feature_color = MISSING_MAP_COLOR
        else:
            feature_color = carbon_color(row.get("standing_live_biomass_tco2"))
        tooltip = (
            "Elusbiomassi süsinikuvaru "
            f"{fmt_num(row.get('standing_live_biomass_tco2'), 0)} t CO₂e · "
            f"{row.get('dominant_species', '–')}"
        )
        features.append(
            {
                "type": "Feature",
                "geometry": row.geometry.__geo_interface__,
                "properties": {
                    "_color": feature_color,
                    "_tooltip": tooltip,
                    "_popup": "<br>".join(popup),
                },
            }
        )

    folium.GeoJson(
        {"type": "FeatureCollection", "features": features},
        style_function=lambda feature: {
            "color": feature["properties"]["_color"],
            "weight": 2,
            "fillColor": feature["properties"]["_color"],
            "fillOpacity": 0.45,
        },
        tooltip=folium.GeoJsonTooltip(fields=["_tooltip"], aliases=[""], labels=False),
        popup=folium.GeoJsonPopup(
            fields=["_popup"],
            aliases=[""],
            labels=False,
            max_width=380,
            style=(
                "background-color: white; color: #333; font-family: arial; font-size: 12px;"
            ),
        ),
    ).add_to(m)
    return m


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------
st.title("🌲 Metsateatiste biomassi süsinik — MVP")
st.caption(
    "Metsaregistri WFS → ainult kattuvate eraldiste detail-API → puuliigipõhine biomassi süsinik"
)

with st.sidebar:
    st.header("Periood")
    default_end = date.today()
    default_start = default_end - timedelta(days=7)
    start = st.date_input("Algus", value=default_start)
    end = st.date_input("Lõpp", value=default_end)
    max_notices = st.number_input(
        "Maksimaalne kirjete arv kihist",
        min_value=100,
        max_value=100000,
        value=5000,
        step=100,
    )
    run = st.button("Laadi ja arvuta", type="primary", use_container_width=True)
    force_recalculation = st.checkbox("Arvuta uuesti (eirab salvestatud tulemust)")
    refresh_data = st.button("Värskenda lähteandmeid", use_container_width=True)
    st.divider()
    st.markdown("**Süsiniku MVP**")
    st.caption(
        "Puuliigiti: tüvemaht × puidutihedus × BEF 1.30 × C 0.50 × 44/12. "
        "Elusbiomassi süsinikuvaru ja kavandatava raiemahu biomass "
        "ei ole heite ega kliimamõju hinnangud."
    )

if refresh_data:
    clear_data_cache(cache_root=DEFAULT_CACHE_ROOT)
    st.cache_data.clear()
    st.session_state.pop("results", None)
    st.session_state.pop("species_df", None)
    st.rerun()

if start > end:
    st.error("Alguskuupäev peab olema lõppkuupäevast varasem.")
    st.stop()

if run:
    try:
        cached_analysis = read_analysis_cache(
            start,
            end,
            int(max_notices),
            ANALYSIS_MODEL_VERSION,
            bypass=force_recalculation,
            cache_root=DEFAULT_CACHE_ROOT,
        )
        if cached_analysis is not None:
            results, species_df = cached_analysis
            st.success("Laaditud salvestatud arvutustulemus.")
        else:
            with st.status("Laen Metsaregistri andmeid…", expanded=True) as status:
                st.write("1/4 Metsateatised…")
                notices = load_notices(start, end, int(max_notices))
                if notices.empty:
                    status.update(label="Valmis", state="complete")
                    st.warning("Valitud perioodil metsateatisi ei leitud.")
                    st.stop()

                st.write(f"2/4 Leitud {len(notices)} teatist. Laen eraldised pakkidena…")
                stand_progress = st.progress(0.0, text="Valmistan eraldiste päringuid ette…")

                def update_stand_progress(completed, total, stage):
                    stage_name = "täpsed päringud" if stage == "exact" else "bbox-varupäringud"
                    ratio = completed / total if total else 1.0
                    stand_progress.progress(
                        ratio,
                        text=f"Eraldised: {stage_name} {completed}/{total}",
                    )

                stands = resolve_stands_for_notices(
                    notices,
                    exact_loader=load_stands_for_filter,
                    bbox_loader=load_stands_for_bbox,
                    batch_size=50,
                    progress_callback=update_stand_progress,
                )
                stand_progress.empty()
                st.write(
                    f"3/4 Leitud {len(stands)} eraldist. "
                    "Küsin ainult kattuvate eraldiste detailid…"
                )
                detail_progress = st.progress(0.0, text="Valmistan detailpäringuid ette…")

                def update_detail_progress(completed, total):
                    ratio = completed / total if total else 1.0
                    detail_progress.progress(
                        ratio,
                        text=f"Eraldiste detailid: {completed}/{total}",
                    )

                results, species_df = analyze(
                    notices,
                    stands,
                    detail_progress_callback=update_detail_progress,
                )
                detail_progress.empty()
                st.write("4/4 Koondan puuliigipõhise süsiniku ja kaardi…")
                status.update(label="Arvutus valmis", state="complete")

            try:
                write_analysis_cache(
                    start,
                    end,
                    int(max_notices),
                    ANALYSIS_MODEL_VERSION,
                    results,
                    species_df,
                    cache_root=DEFAULT_CACHE_ROOT,
                )
            except Exception as cache_error:
                st.warning(f"Arvutus õnnestus, kuid tulemust ei saanud salvestada: {cache_error}")
        st.session_state["results"] = results
        st.session_state["species_df"] = species_df
    except Exception as e:
        st.exception(e)
        st.info(
            "Kui Metsaregistri skeem on muutunud, on kõige tõenäolisemad kohad eraldise ID "
            "või detail-API `elemendid` väljad."
        )

if "results" in st.session_state:
    results = st.session_state["results"]
    species_df = st.session_state.get("species_df", pd.DataFrame())
    if "species_code" in species_df.columns:
        species_df = species_df.copy()
        species_df["species"] = species_df["species_code"].apply(species_name_for_code)
    valid_standing = results[results["standing_live_biomass_tco2"].notna()].copy()
    valid_planned = results[results["planned_harvest_biomass_tco2"].notna()].copy()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Metsateatisi", f"{len(results):,}".replace(",", " "))
    c2.metric("Raiealade pindala", f"{results['area_ha'].sum():,.1f} ha".replace(",", " "))
    c3.metric(
        "Elusbiomassi süsinikuvaru",
        f"{valid_standing['standing_live_biomass_tco2'].sum():,.0f} t CO₂e".replace(",", " ")
        if len(valid_standing)
        else "–",
    )
    c4.metric(
        "Kavandatava raiemahu biomass",
        f"{valid_planned['planned_harvest_biomass_tco2'].sum():,.0f} t CO₂e".replace(",", " ")
        if len(valid_planned)
        else "–",
    )

    standing_sources = " + ".join(
        sorted({str(value) for value in results["standing_volume_basis"].dropna()})
    )
    planned_sources = " + ".join(
        sorted({str(value) for value in results["planned_harvest_volume_basis"].dropna()})
    )
    st.caption(
        f"Elusbiomassi mahu alus: {standing_sources or 'andmed puuduvad'} · "
        f"Kavandatava raiemahu alus: {planned_sources or 'andmed puuduvad'}"
    )

    inventory_dates = sorted({str(value) for value in results["inventory_date"].dropna()})
    inventory_recency = ", ".join(
        sorted({str(value) for value in results["inventory_recency"].dropna()})
    )
    inventory_ages = results["inventory_age_years"].dropna()
    st.caption(
        f"Inventuuri kuupäev: {', '.join(inventory_dates) or '–'} · "
        f"vanus: {fmt_num(inventory_ages.max(), 2) if len(inventory_ages) else '–'} a · "
        f"värskus: {inventory_recency or 'teadmata'}"
    )

    increment_complete = results.get(
        "current_increment_is_complete",
        results["current_increment_on_overlap_m3_y"].notna(),
    ).astype(bool)
    increment_totals = pd.to_numeric(results["current_increment_on_overlap_m3_y"], errors="coerce")
    increment_areas = pd.to_numeric(
        results.get("current_increment_covered_area_ha"), errors="coerce"
    )
    compatible_increment = (
        increment_complete
        & np.isfinite(increment_totals)
        & np.isfinite(increment_areas)
        & (increment_areas > 0)
    )
    dashboard_increment_total = increment_totals.loc[compatible_increment].sum(min_count=1)
    dashboard_increment_area = increment_areas.loc[compatible_increment].sum(min_count=1)
    dashboard_increment_rate = (
        dashboard_increment_total / dashboard_increment_area
        if pd.notna(dashboard_increment_total)
        and pd.notna(dashboard_increment_area)
        and dashboard_increment_area > 0
        else np.nan
    )
    st.caption(
        "Jooksev juurdekasv: "
        f"{fmt_num(dashboard_increment_rate, 1)} m³/ha/a · "
        f"{fmt_num(dashboard_increment_total, 1)} m³/a"
    )

    harvest_estimates = (
        results["planned_harvest_volume_basis"]
        .astype(str)
        .str.contains(VolumeBasis.NOTICE_HARVEST_VOLUME.value, regex=False)
        .sum()
    )
    if harvest_estimates:
        st.info(
            f"{harvest_estimates} teatise kavandatava raiemahu biomass põhineb teatises "
            "esitatud raiemahul ja puuliikide osakaaludel. See ei ole heite hinnang."
        )

    tab1, tab2, tab3, tab4 = st.tabs(["Kaart", "Koond", "Puuliigid", "Andmed"])

    with tab1:
        map_color_mode = st.selectbox("Kaardi värv", MAP_COLOR_MODES)
        st_folium(
            make_map(results, color_mode=map_color_mode),
            use_container_width=True,
            height=650,
            returned_objects=[],
        )

    with tab2:
        left, right = st.columns(2)
        with left:
            st.subheader("Elusbiomassi süsinikuvaru ja kavandatava raiemahu biomass teatise kaupa")
            chart_df = results[
                ["standing_live_biomass_tco2", "planned_harvest_biomass_tco2"]
            ].dropna(how="all")
            chart_df = chart_df.rename(
                columns={
                    "standing_live_biomass_tco2": "Elusbiomassi süsinikuvaru (t CO₂e)",
                    "planned_harvest_biomass_tco2": ("Kavandatava raiemahu biomass (t CO₂e)"),
                }
            )
            chart_df["teatis"] = np.arange(1, len(chart_df) + 1)
            st.bar_chart(chart_df.set_index("teatis"), height=350)
        with right:
            st.subheader("Ruumilise andmekatte kvaliteet")
            spatial_quality = (
                results["spatial_coverage_quality"]
                .value_counts()
                .rename_axis("kvaliteet")
                .reset_index(name="teatisi")
            )
            st.bar_chart(spatial_quality.set_index("kvaliteet"), y="teatisi", height=160)
            st.subheader("Inventuuri värskus")
            recency = (
                results["inventory_recency"]
                .value_counts()
                .rename_axis("värskus")
                .reset_index(name="teatisi")
            )
            st.bar_chart(recency.set_index("värskus"), y="teatisi", height=160)

        st.subheader("Suurima elusbiomassi süsinikuvaruga teatised")
        summary = build_export_table(results)
        st.dataframe(
            summary.sort_values("standing_live_biomass_tco2", ascending=False).head(20),
            use_container_width=True,
            hide_index=True,
        )

    with tab3:
        st.subheader("Puuliikide biomass hinnangu liigi kaupa")
        if species_df.empty:
            st.info("Puuliigipõhist detailinfot ei saadud.")
        else:
            by_species = (
                species_df.groupby(["species", "estimate_scope"], as_index=False)
                .agg(
                    volume_m3=("volume_m3", "sum"),
                    biomass_tco2=("biomass_tco2", "sum"),
                )
                .sort_values("biomass_tco2", ascending=False)
            )
            species_chart = by_species.pivot(
                index="species", columns="estimate_scope", values="biomass_tco2"
            ).rename(
                columns={
                    "standing": "Elusbiomassi süsinikuvaru (t CO₂e)",
                    "planned_harvest": "Kavandatava raiemahu biomass (t CO₂e)",
                }
            )
            st.bar_chart(species_chart, height=380)
            st.dataframe(by_species, use_container_width=True, hide_index=True)

    with tab4:
        non_geom = build_export_table(results)
        st.dataframe(non_geom, use_container_width=True, hide_index=True)
        csv = non_geom.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "Laadi tulemused CSV-na",
            csv,
            "metsateatised_biomassi_susinik.csv",
            "text/csv",
        )

    with st.expander("Metoodika ja piirangud"):
        st.markdown(
            """
**Mis muutus võrreldes esimese versiooniga?**

- Rakendus ei lae enam kogu `eraldis_element` kihti.
- Kõigepealt leitakse ruumiliselt ainult metsateatisega kattuvad eraldised.
- Seejärel küsitakse Metsaregistri avalikust eraldise detail-API-st ainult nende
  eraldiste detailid.
- Süsinik arvutatakse **iga puuliigi kohta eraldi** tema tagavara ja
  puidutihedusega ning summeeritakse teatise tasemele.

Valem on:

`tüvemaht liigiti × puidutihedus liigiti × BEF × C-fraktsioon × 44/12`.

**Elusbiomassi süsinikuvaru** kirjeldab inventuuriandmetest hinnatud praegust
eluspuude biomassi. **Kavandatava raiemahu biomass** arvutatakse teatises esitatud
raiemahust eraldi. See ei ole heite ega kliimamõju hinnang.

Jooksev juurdekasv on inventuuri hetkeseisu aastane mahunäitaja. Seda ei kasutata
tulevase tagavara ega süsinikuvaru prognoosimiseks. Kliimamõju hindamiseks tuleb
eraldi mudelis võrrelda ajas kulgevaid stsenaariume — vähemalt raie puudumist ning
raiet koos metsauuendusega — ja lisada raiutud puittooted, surnud orgaaniline aine
ning mullasüsinik.
            """
        )

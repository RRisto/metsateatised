from __future__ import annotations

import asyncio
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
from streamlit_folium import st_folium

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

st.set_page_config(page_title="Metsateatiste süsinikumõju MVP", layout="wide")


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

    if intersections.empty:
        out = n.copy()
        out["area_ha"] = out.geometry.area / 10000
        out["standing_live_biomass_tco2"] = np.nan
        out["standing_live_biomass_tco2_ha"] = np.nan
        out["planned_harvest_biomass_tco2"] = np.nan
        out["standing_volume_basis"] = VolumeBasis.UNKNOWN.value
        out["planned_harvest_volume_basis"] = VolumeBasis.UNKNOWN.value
        out["spatial_coverage_pct"] = 0.0
        out["spatial_coverage_quality"] = "nõrk"
        out["inventory_date"] = None
        out["inventory_age_years"] = np.nan
        out["inventory_recency"] = "teadmata"
        out["volume_source_quality"] = VolumeBasis.UNKNOWN.value
        out["current_increment_m3_ha_y"] = np.nan
        out["current_increment_on_overlap_m3_y"] = np.nan
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
                stand.species,
            )
        else:
            planned_harvest_volume = estimate_planned_harvest_volume(-1.0, stand.species)

        carbon = calculate_notice_carbon(
            standing_species_volumes=standing_volume.species_volumes,
            planned_harvest_species_volumes=planned_harvest_volume.species_volumes,
        )
        age_num = 0.0
        age_den = 0.0
        species_by_code = {species.code: species for species in stand.species}
        for estimate_scope, species_volumes in (
            ("standing", standing_volume.species_volumes),
            ("planned_harvest", planned_harvest_volume.species_volumes),
        ):
            for species_volume in species_volumes:
                species = species_by_code.get(species_volume.species_code)
                species_carbon = carbon_from_species_volume(
                    species_volume.volume_m3,
                    species_volume.species_code,
                )
                if estimate_scope == "standing" and species and species.inventory_age is not None:
                    age_num += species.inventory_age * species_volume.volume_m3
                    age_den += species_volume.volume_m3

                species_breakdown_rows.append(
                    {
                        "notice_ix": row["notice_ix"],
                        "stand_id": sid,
                        "estimate_scope": estimate_scope,
                        "species_code": species_volume.species_code,
                        "species": species_name_for_code(species_volume.species_code),
                        "overlap_ha": row["overlap_ha"],
                        "volume_m3": species_volume.volume_m3,
                        "biomass_tco2": species_carbon,
                        "age": species.inventory_age if species else None,
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
                "weighted_age_num": age_num,
                "weighted_age_den": age_den,
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
    out["standing_volume_basis"] = out["standing_volume_basis"].fillna(
        VolumeBasis.UNKNOWN.value
    )
    out["planned_harvest_volume_basis"] = out["planned_harvest_volume_basis"].fillna(
        VolumeBasis.UNKNOWN.value
    )
    out["spatial_coverage_quality"] = out["spatial_coverage_pct"].apply(
        classify_spatial_coverage
    )
    out["volume_source_quality"] = out["standing_volume_basis"]

    inventory_metrics = pd.DataFrame(inventory_metric_rows)
    notice_inventory_metrics = []
    for notice_ix, rows in inventory_metrics.groupby("notice_ix"):
        known_ages = rows.dropna(subset=["inventory_age_years"])
        if known_ages.empty:
            inventory_age_years = np.nan
            inventory_date = None
        else:
            inventory_age_years = np.average(
                known_ages["inventory_age_years"], weights=known_ages["overlap_ha"]
            )
            inventory_date = known_ages["inventory_date"].min()

        recency = (
            classify_inventory_recency(inventory_age_years)
            if len(known_ages) == len(rows)
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


def fmt_num(x, digits=0):
    if pd.isna(x):
        return "–"
    return f"{x:,.{digits}f}".replace(",", " ")


def make_map(results: gpd.GeoDataFrame):
    if results.empty:
        return folium.Map(location=[58.6, 25.0], zoom_start=7)

    cent = results.to_crs(3301).geometry.union_all().centroid
    center = gpd.GeoSeries([cent], crs=3301).to_crs(4326).iloc[0]
    m = folium.Map(location=[center.y, center.x], zoom_start=8, tiles="CartoDB positron")

    vals = results["carbon_co2e_t"].replace([np.inf, -np.inf], np.nan).dropna()
    q1 = vals.quantile(0.33) if len(vals) else 0
    q2 = vals.quantile(0.66) if len(vals) else 0

    id_col = likely_notice_id_column(results)
    harvest_col = likely_harvest_type_column(results)
    date_col = None
    if "_date_col" in results and len(results):
        date_col = (
            results["_date_col"].dropna().iloc[0] if results["_date_col"].notna().any() else None
        )

    def color(v):
        if pd.isna(v):
            return "#777777"
        if v <= q1:
            return "#2ca25f"
        if v <= q2:
            return "#fec44f"
        return "#de2d26"

    for _, row in results.iterrows():
        popup = [
            f"<b>Metsateatis</b>: {row.get(id_col, '–') if id_col else '–'}",
            f"Pindala: {fmt_num(row.get('area_ha'), 2)} ha",
            f"Valdav puuliik: {row.get('dominant_species', '–')}",
            f"Keskmine vanus: {fmt_num(row.get('mean_age'), 0)} a",
            f"Biomassi süsinik: {fmt_num(row.get('carbon_co2e_t'), 0)} t CO₂e",
            f"Tüvemaht: {fmt_num(row.get('estimated_stem_volume_m3'), 0)} m³",
            f"Arvutuse alus: {row.get('calculation_basis', 'andmed puuduvad')}",
            f"Andmekate: {fmt_num(row.get('inventory_coverage_pct'), 0)}%",
        ]
        if harvest_col:
            popup.insert(1, f"Raieliik: {row.get(harvest_col, '–')}")
        if date_col and date_col in row:
            popup.insert(1, f"Kuupäev: {row.get(date_col, '–')}")

        feature_color = color(row.get("carbon_co2e_t"))
        tooltip = (
            f"{fmt_num(row.get('carbon_co2e_t'), 0)} t CO₂e · {row.get('dominant_species', '–')}"
        )
        folium.GeoJson(
            row.geometry.__geo_interface__,
            style_function=lambda _, c=feature_color: {
                "color": c,
                "weight": 2,
                "fillColor": c,
                "fillOpacity": 0.45,
            },
            tooltip=tooltip,
            popup=folium.Popup("<br>".join(popup), max_width=380),
        ).add_to(m)
    return m


# -----------------------------------------------------------------------------
# UI
# -----------------------------------------------------------------------------
st.title("🌲 Metsateatiste süsinikumõju — MVP")
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
        max_value=50000,
        value=5000,
        step=100,
    )
    run = st.button("Laadi ja arvuta", type="primary", use_container_width=True)
    refresh_data = st.button("Värskenda lähteandmeid", use_container_width=True)
    st.divider()
    st.markdown("**Süsiniku MVP**")
    st.caption(
        "Puuliigiti: tüvemaht × puidutihedus × BEF 1.30 × C 0.50 × 44/12. "
        "Tulemus on eluspuude biomassis oleva CO₂e hinnang, mitte veel täielik lageraie kliimamõju."
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
                f"3/4 Leitud {len(stands)} eraldist. Küsin ainult kattuvate eraldiste detailid…"
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
    valid = results[results["carbon_co2e_t"].notna()].copy()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Metsateatisi", f"{len(results):,}".replace(",", " "))
    c2.metric("Raiealade pindala", f"{results['area_ha'].sum():,.1f} ha".replace(",", " "))
    c3.metric(
        "Biomassi süsinik",
        f"{valid['carbon_co2e_t'].sum():,.0f} t CO₂e".replace(",", " ") if len(valid) else "–",
    )
    c4.metric(
        "Keskmine",
        f"{valid['carbon_t_per_ha'].mean():,.0f} t CO₂e/ha".replace(",", " ")
        if len(valid)
        else "–",
    )

    harvest_estimates = results["calculation_basis"].eq("raiemahu põhine hinnang").sum()
    if harvest_estimates:
        st.info(
            f"{harvest_estimates} teatise süsinik on hinnatud raiutava mahu ja "
            "puuliikide osakaalude põhjal, sest inventuuri tagavara puudus."
        )

    tab1, tab2, tab3, tab4 = st.tabs(["Kaart", "Koond", "Puuliigid", "Andmed"])

    with tab1:
        st_folium(make_map(results), use_container_width=True, height=650, returned_objects=[])

    with tab2:
        left, right = st.columns(2)
        with left:
            st.subheader("Süsinik teatise kaupa")
            chart_df = valid[["carbon_co2e_t"]].copy()
            chart_df["teatis"] = np.arange(1, len(chart_df) + 1)
            st.bar_chart(chart_df.set_index("teatis"), y="carbon_co2e_t", height=350)
        with right:
            st.subheader("Andmekatte kvaliteet")
            quality = (
                results["data_quality"]
                .value_counts()
                .rename_axis("kvaliteet")
                .reset_index(name="teatisi")
            )
            st.bar_chart(quality.set_index("kvaliteet"), y="teatisi", height=350)

        st.subheader("Suurima biomassi süsinikuga teatised")
        id_col = likely_notice_id_column(results)
        harvest_col = likely_harvest_type_column(results)
        cols = [
            c
            for c in [
                id_col,
                harvest_col,
                "dominant_species",
                "mean_age",
                "area_ha",
                "estimated_stem_volume_m3",
                "carbon_co2e_t",
                "carbon_t_per_ha",
                "inventory_coverage_pct",
                "calculation_basis",
                "data_quality",
            ]
            if c and c in results
        ]
        st.dataframe(
            results.sort_values("carbon_co2e_t", ascending=False)[cols].head(20),
            use_container_width=True,
            hide_index=True,
        )

    with tab3:
        st.subheader("Puuliikide panus biomassi süsinikku")
        if species_df.empty:
            st.info("Puuliigipõhist detailinfot ei saadud.")
        else:
            by_species = (
                species_df.groupby("species", as_index=False)
                .agg(
                    volume_m3=("volume_m3", "sum"),
                    carbon_co2e_t=("carbon_co2e_t", "sum"),
                )
                .sort_values("carbon_co2e_t", ascending=False)
            )
            st.bar_chart(by_species.set_index("species"), y="carbon_co2e_t", height=380)
            st.dataframe(by_species, use_container_width=True, hide_index=True)

    with tab4:
        non_geom = pd.DataFrame(results.drop(columns=[results.geometry.name], errors="ignore"))
        st.dataframe(non_geom, use_container_width=True, hide_index=True)
        csv = non_geom.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "Laadi tulemused CSV-na",
            csv,
            "metsateatised_susinikumõju.csv",
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

See tulemus kirjeldab hinnanguliselt **praegu eluspuude biomassis olevat CO₂e
kogust raiutaval alal**. See ei ole veel lageraie netokliimamõju. Selleks tuleb
järgmises etapis võrrelda vähemalt kahte ajas kulgevat stsenaariumi: **raieta** vs
**lageraie + metsauuendus**, ning lisada raiutud puittooted, surnud orgaaniline
aine ja mullasüsinik.
            """
        )

"""Resumable monthly preprocessing for the durable raw notice archive."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

import geopandas as gpd
import pandas as pd

import wfs
from data_cache import DEFAULT_CACHE_ROOT
from forest_data import load_stands_for_notices
from notice_analysis import analyze_notices, fetch_stand_details
from processed_notice_store import (
    MonthKey,
    RawMonth,
    discover_raw_months,
    is_month_current,
    publish_processed_month,
    read_raw_month,
)

STANDS_LAYER = "metsaregister:eraldis"


@dataclass(frozen=True)
class PreprocessProgress:
    month: MonthKey
    stage: Literal["reading", "stands", "details", "publishing"]
    completed: int | None = None
    total: int | None = None


@dataclass(frozen=True)
class MonthFailure:
    month: MonthKey
    message: str


@dataclass(frozen=True)
class PreprocessResult:
    completed: tuple[MonthKey, ...]
    skipped: tuple[MonthKey, ...]
    failed: tuple[MonthFailure, ...]


def _wfs_frame(*, cql_filter: str | None = None, bbox=None, maximum: int) -> gpd.GeoDataFrame:
    features = wfs.fetch_wfs_features(
        STANDS_LAYER,
        max_features=maximum,
        cql_filter=cql_filter,
        bbox=bbox,
        cache_root=DEFAULT_CACHE_ROOT,
    )
    if not features:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")


def _resolve_stands(
    notices: gpd.GeoDataFrame,
    *,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> gpd.GeoDataFrame:
    return load_stands_for_notices(
        notices,
        exact_loader=lambda cql: _wfs_frame(cql_filter=cql, maximum=500),
        bbox_loader=lambda bbox: _wfs_frame(bbox=bbox, maximum=5_000),
        batch_size=50,
        progress_callback=progress_callback,
    )


def _calculate(
    notices: gpd.GeoDataFrame,
    stands: gpd.GeoDataFrame,
    *,
    detail_workers: int,
    detail_progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    detail_loader = partial(fetch_stand_details, n_workers=detail_workers)
    return analyze_notices(
        notices,
        stands,
        detail_loader=detail_loader,
        detail_progress_callback=detail_progress_callback,
    )


def process_month(
    raw_root: Path,
    processed_root: Path,
    raw_month: RawMonth,
    *,
    model_version: str,
    resolve_stands: Callable = _resolve_stands,
    calculate: Callable = _calculate,
    detail_workers: int = 12,
    progress: Callable[[PreprocessProgress], None] | None = None,
) -> None:
    def report(stage, completed=None, total=None):
        if progress is not None:
            progress(PreprocessProgress(raw_month.key, stage, completed, total))

    report("reading")
    notices = read_raw_month(raw_root, raw_month)
    report("stands")
    stands = resolve_stands(
        notices,
        progress_callback=lambda completed, total, _stage: report(
            "stands", completed, total
        ),
    )
    results, species = calculate(
        notices,
        stands,
        detail_workers=detail_workers,
        detail_progress_callback=lambda completed, total: report(
            "details", completed, total
        ),
    )
    report("publishing")
    publish_processed_month(
        processed_root,
        raw_month.key,
        results,
        species,
        input_fingerprint=raw_month.input_fingerprint,
        model_version=model_version,
    )


def preprocess_notices(
    raw_root: Path,
    processed_root: Path,
    *,
    model_version: str,
    start: MonthKey | None = None,
    end: MonthKey | None = None,
    force: bool = False,
    resolve_stands: Callable = _resolve_stands,
    calculate: Callable = _calculate,
    detail_workers: int = 12,
    progress: Callable[[PreprocessProgress], None] | None = None,
) -> PreprocessResult:
    completed = []
    skipped = []
    failed = []
    for raw_month in discover_raw_months(raw_root):
        if start is not None and raw_month.key < start:
            continue
        if end is not None and raw_month.key > end:
            continue
        if not force and is_month_current(processed_root, raw_month, model_version):
            skipped.append(raw_month.key)
            continue
        try:
            process_month(
                raw_root,
                processed_root,
                raw_month,
                model_version=model_version,
                resolve_stands=resolve_stands,
                calculate=calculate,
                detail_workers=detail_workers,
                progress=progress,
            )
            completed.append(raw_month.key)
        except Exception as error:
            failed.append(MonthFailure(raw_month.key, str(error)))
    return PreprocessResult(tuple(completed), tuple(skipped), tuple(failed))

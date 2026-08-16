from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import pandas as pd


def _cache_directory(
    start: date,
    end: date,
    max_features: int,
    model_version: str,
    cache_root: Path,
) -> Path:
    parameters = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "max_features": int(max_features),
        "model_version": model_version,
    }
    serialized = json.dumps(parameters, sort_keys=True, separators=(",", ":"))
    key = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return cache_root / "analysis" / key


def write_analysis_cache(
    start: date,
    end: date,
    max_features: int,
    model_version: str,
    results: gpd.GeoDataFrame,
    species: pd.DataFrame,
    *,
    cache_root: Path,
) -> None:
    directory = _cache_directory(start, end, max_features, model_version, cache_root)
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    temporary_results = directory / f"results-{token}.parquet"
    temporary_species = directory / f"species-{token}.parquet"
    temporary_manifest = directory / f"manifest-{token}.json"

    results.to_parquet(temporary_results, index=False)
    species.to_parquet(temporary_species, index=False)
    temporary_manifest.write_text(
        json.dumps({"model_version": model_version}, ensure_ascii=False),
        encoding="utf-8",
    )

    temporary_results.replace(directory / "results.parquet")
    temporary_species.replace(directory / "species.parquet")
    temporary_manifest.replace(directory / "manifest.json")


def read_analysis_cache(
    start: date,
    end: date,
    max_features: int,
    model_version: str,
    *,
    bypass: bool = False,
    cache_root: Path,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame] | None:
    if bypass:
        return None
    directory = _cache_directory(start, end, max_features, model_version, cache_root)
    manifest_path = directory / "manifest.json"
    results_path = directory / "results.parquet"
    species_path = directory / "species.parquet"
    if not (manifest_path.exists() and results_path.exists() and species_path.exists()):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("model_version") != model_version:
            return None
        return gpd.read_parquet(results_path), pd.read_parquet(species_path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None

from datetime import date

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point

from analysis_cache import read_analysis_cache, write_analysis_cache


def _cached_frames():
    results = gpd.GeoDataFrame(
        {"teatis_id": [3973677], "standing_live_biomass_tco2": [300.0]},
        geometry=[Point(24.47, 58.75)],
        crs="EPSG:4326",
    )
    species = pd.DataFrame({"species": ["Kask"], "biomass_tco2": [300.0]})
    return results, species


def test_analysis_cache_round_trip_preserves_results_and_geometry(tmp_path):
    """Losing geometry or calculated columns would make a persisted result unusable."""
    results, species = _cached_frames()
    write_analysis_cache(
        date(2016, 1, 1),
        date(2026, 1, 1),
        50_000,
        "model-v1",
        results,
        species,
        cache_root=tmp_path,
    )

    cached = read_analysis_cache(
        date(2016, 1, 1),
        date(2026, 1, 1),
        50_000,
        "model-v1",
        cache_root=tmp_path,
    )

    assert cached is not None
    cached_results, cached_species = cached
    assert isinstance(cached_results, gpd.GeoDataFrame)
    assert cached_results.crs == results.crs
    assert cached_results.geometry.iloc[0].equals(results.geometry.iloc[0])
    pd.testing.assert_frame_equal(
        cached_results.drop(columns="geometry"), results.drop(columns="geometry")
    )
    pd.testing.assert_frame_equal(cached_species, species)


def test_analysis_cache_does_not_reuse_another_model_version(tmp_path):
    """Reusing results after calculation semantics change would show stale carbon values."""
    results, species = _cached_frames()
    write_analysis_cache(
        date(2016, 1, 1),
        date(2026, 1, 1),
        50_000,
        "model-v1",
        results,
        species,
        cache_root=tmp_path,
    )

    current = read_analysis_cache(
        date(2016, 1, 1),
        date(2026, 1, 1),
        50_000,
        "model-v1",
        cache_root=tmp_path,
    )
    stale = read_analysis_cache(
        date(2016, 1, 1),
        date(2026, 1, 1),
        50_000,
        "model-v2",
        cache_root=tmp_path,
    )

    assert current is not None
    assert stale is None


def test_analysis_cache_can_be_bypassed_for_forced_recalculation(tmp_path):
    """A forced recalculation must not silently return the persisted result."""
    results, species = _cached_frames()
    write_analysis_cache(
        date(2016, 1, 1),
        date(2026, 1, 1),
        50_000,
        "model-v1",
        results,
        species,
        cache_root=tmp_path,
    )

    ordinary = read_analysis_cache(
        date(2016, 1, 1),
        date(2026, 1, 1),
        50_000,
        "model-v1",
        cache_root=tmp_path,
    )
    bypassed = read_analysis_cache(
        date(2016, 1, 1),
        date(2026, 1, 1),
        50_000,
        "model-v1",
        bypass=True,
        cache_root=tmp_path,
    )

    assert ordinary is not None
    assert bypassed is None

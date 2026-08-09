import pandas as pd

from carbon import aggregate_intersections


def test_aggregate_intersections_keeps_all_missing_estimates_unknown():
    rows = [
        {
            "notice_ix": 0,
            "overlap_ha": 1.5,
            "stem_volume_m3": float("nan"),
            "carbon_co2e_t": float("nan"),
            "weighted_age_num": 0.0,
            "weighted_age_den": 0.0,
        }
    ]

    result = aggregate_intersections(rows)

    assert pd.isna(result.loc[0, "carbon_co2e_t"])
    assert pd.isna(result.loc[0, "estimated_stem_volume_m3"])
    assert result.loc[0, "covered_by_inventory_ha"] == 1.5

import pandas as pd

from carbon import aggregate_intersections


def test_aggregate_intersections_keeps_all_missing_estimates_unknown():
    rows = [
        {
            "notice_ix": 0,
            "overlap_ha": 1.5,
            "standing_live_biomass_tco2": float("nan"),
            "planned_harvest_biomass_tco2": float("nan"),
            "standing_volume_basis": "andmed puuduvad",
            "planned_harvest_volume_basis": "andmed puuduvad",
            "weighted_age_num": 0.0,
            "weighted_age_den": 0.0,
        }
    ]

    result = aggregate_intersections(rows)

    assert pd.isna(result.loc[0, "standing_live_biomass_tco2"])
    assert pd.isna(result.loc[0, "planned_harvest_biomass_tco2"])
    assert result.loc[0, "covered_by_inventory_ha"] == 1.5


def test_aggregate_intersections_keeps_planned_harvest_out_of_standing_biomass():
    """Copying a planned estimate into standing biomass would misstate inventory state."""
    rows = [
        {
            "notice_ix": 0,
            "overlap_ha": 1.0,
            "standing_live_biomass_tco2": float("nan"),
            "planned_harvest_biomass_tco2": 12.5,
            "standing_volume_basis": "andmed puuduvad",
            "planned_harvest_volume_basis": "raiemahu pÃµhine hinnang",
            "weighted_age_num": 0.0,
            "weighted_age_den": 0.0,
        }
    ]

    result = aggregate_intersections(rows)

    assert pd.isna(result.loc[0, "standing_live_biomass_tco2"])
    assert result.loc[0, "planned_harvest_biomass_tco2"] == 12.5

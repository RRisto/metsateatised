from stand_model import aggregate_increment, classify_spatial_coverage


def test_increment_is_scaled_to_overlap_and_area_weighted():
    """Ignoring overlap area would misstate the notice's current annual increment."""
    rows = [
        {"overlap_ha": 1.0, "increment_m3_ha_y": 4.0},
        {"overlap_ha": 3.0, "increment_m3_ha_y": 8.0},
    ]

    result = aggregate_increment(rows)

    assert result.current_increment_on_overlap_m3_y == 28.0
    assert result.current_increment_m3_ha_y == 7.0


def test_increment_aggregation_does_not_treat_missing_rates_as_zero():
    """Missing inventory increment must remain unknown rather than become a zero-growth claim."""
    result = aggregate_increment([{"overlap_ha": 1.0, "increment_m3_ha_y": None}])

    assert result.current_increment_on_overlap_m3_y is None
    assert result.current_increment_m3_ha_y is None


def test_spatial_coverage_uses_existing_thresholds_without_overall_score():
    """Changing the 90/50 percent bands would alter the coverage dimension's meaning."""
    assert classify_spatial_coverage(90.0) == "hea"
    assert classify_spatial_coverage(50.0) == "osaline"
    assert classify_spatial_coverage(49.9) == "nõrk"

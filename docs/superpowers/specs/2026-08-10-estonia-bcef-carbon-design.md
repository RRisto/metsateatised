# Estonia BCEF Above-Ground Biomass Carbon Design

## Objective

Replace the current wood-density × BEF 1.30 conversion with the supplied Estonia-specific above-ground biomass conversion and expansion factors (BCEF). The calculation answers only how much carbon is stored in above-ground living biomass represented by standing or planned-harvest stem volume.

The result remains a stock expressed as tonnes CO₂ equivalent. It is not an estimate of immediate harvesting emissions, climate impact, roots, soil carbon, dead wood, harvested wood products, future regrowth, avoided sequestration, or carbon payback time.

## Public Schema Rename

Rename the public result fields everywhere, without legacy aliases in dashboard or CSV output:

- `standing_live_biomass_tco2` → `standing_aboveground_biomass_tco2`
- `standing_live_biomass_tco2_ha` → `standing_aboveground_biomass_tco2_ha`
- `planned_harvest_biomass_tco2` → `planned_harvest_aboveground_biomass_tco2`

The renamed fields flow through result dataclasses, dataframes, aggregation, maps, tables, charts, CSV exports, tests, and documentation. Old public names are removed so consumers cannot confuse above-ground biomass with whole-tree biomass.

## Calculation Model

For every species-volume allocation `i`:

```text
aboveground_co2e_i =
    volume_m3_i
    × BCEF[calculation_group_i][stock_class(total_stand_stock_m3_ha)]
    × 0.50
    × 44/12
```

The BCEF directly converts stem growing stock volume to tonnes of dry above-ground biomass. Wood density and BEF 1.30 must not participate in the primary calculation.

The stock class is selected from the total pre-harvest living stand growing stock per hectare, not from species stock or planned-harvest volume:

- `0–20` → `le_20`
- `>20–50` → `20_50`
- `>50–100` → `50_100`
- `>100` → `gt_100`

All species allocated from the same stand use that stand's stock class.

## BCEF Groups and Coefficients

Species calculation groups are internal and never replace Forest Register identity:

- `MA` → `pine`
- `KU` → `spruce`
- `KS` → `birch`
- `HB` → `aspen`
- `LV` → `grey_alder`
- `LM` → `black_alder`
- every other or missing code → `other`

Codes are normalized by trimming whitespace and uppercasing for lookup only. Original species codes and canonical display names remain unchanged in source records, normalized records, UI, maps, exports, and debug data.

The coefficient table is exactly the one supplied in the source specification:

| Group | ≤20 | >20–50 | >50–100 | >100 |
| --- | ---: | ---: | ---: | ---: |
| pine | 0.6057 | 0.5587 | 0.5429 | 0.5056 |
| spruce | 0.5714 | 0.5455 | 0.5321 | 0.5055 |
| birch | 0.7085 | 0.6321 | 0.6148 | 0.5992 |
| aspen | 0.4246 | 0.4383 | 0.4179 | 0.4365 |
| grey_alder | 0.4045 | 0.4181 | 0.4313 | 0.4395 |
| black_alder | 0.4281 | 0.4669 | 0.4768 | 0.4844 |
| other | 0.4914 | 0.4889 | 0.4852 | 0.4899 |

## Data Model and Provenance

Extend immutable `SpeciesVolume` with:

```python
stand_stock_m3_ha: float | None
```

It records the total pre-harvest living stand stock associated with the allocation. It is not species stock. Existing source-record identity, stratum, inventory age, and current age fields remain.

Standing and planned-harvest allocators populate `stand_stock_m3_ha` from the normalized stand. If the normalized total is absent, it may be reconstructed only from species-level stock proven complete for the living stand. An incomplete subset must never be summed and presented as the total.

Per-species calculation rows expose auditable provenance:

- original species code and name;
- internal BCEF group;
- total stand stock and stock class;
- selected BCEF coefficient;
- allocated stem volume;
- dry above-ground biomass;
- above-ground carbon;
- above-ground CO₂e.

## Completeness and Validation

Public numeric inputs must be finite and non-negative. Invalid volume or stock supplied to low-level coefficient/calculation helpers raises `ValueError`.

At estimate level:

- missing or unreliable total stand stock yields unknown biomass and `is_complete=False`;
- a known zero allocated volume yields zero biomass when the required stand stock context is known;
- unknown species uses the `other` group and does not make an otherwise valid estimate incomplete;
- mixed known and unknown allocations must not be summed and displayed as complete;
- volume-source provenance remains independent from biomass-calculation completeness.

No undocumented default stock class is allowed.

## Standing and Planned-Harvest Flows

The existing standing-volume hierarchy remains:

1. complete detailed species stock;
2. complete WFS stand stock allocated using valid stratum-local species shares;
3. unknown/incomplete.

Only the volume-to-biomass conversion changes.

Planned harvest remains a separate estimate:

```text
notice volume → stand intersection allocation → species allocation → BCEF conversion
```

Its BCEF class comes from the stand's total pre-harvest stock, never the planned-harvest volume. Existing multi-stratum allocation and completeness safeguards remain intact.

## Application and Documentation

Dashboard metrics, map tooltips, charts, tables, and CSV exports use explicit Estonian wording for “maapealse elusa biomassi süsinikuvaru”. Methodology text states that BCEF converts stem volume directly to dry above-ground biomass, mixed stands are calculated species by species using the total stand stock class, and uncommon species retain their display identity while using the internal `other` coefficient.

README documents the formula, units, renamed schema, provenance fields, missing-data behavior, and exclusions. `streamlit run app.py` remains the entrypoint.

## Testing Strategy

Implementation follows red-green-refactor. Tests cover:

- exact stock-class boundaries, including fractional values immediately above boundaries;
- normalized species-group lookup and unknown codes;
- every supplied coefficient family and representative exact coefficients;
- invalid and missing numeric inputs;
- replacement of the old density × BEF test;
- mixed-stand calculation where all species use the same total-stock class;
- preservation of original species identity;
- propagation of total stand stock into standing and planned allocations;
- planned harvest using pre-harvest stock rather than harvest volume;
- missing, partial, zero, and mixed-completeness cases;
- multi-stratum allocations and provenance;
- renamed result, aggregation, dashboard, map, and CSV contracts;
- methodology text and removal of old public labels;
- existing offline suite and opt-in live Forest Register contract.

## Acceptance Criteria

The change is complete when:

1. The primary calculation uses the supplied BCEF table and no wood-density or BEF 1.30 multiplier.
2. Total pre-harvest stand stock selects the BCEF class for every species in that stand.
3. Forest Register species codes and names remain unchanged; grouping affects calculation only.
4. Missing total stand stock never silently selects a coefficient class.
5. Standing and planned-harvest estimates remain separate and preserve completeness semantics.
6. Public result fields, UI, maps, charts, CSV, tests, and documentation use the new above-ground names.
7. The dashboard explicitly describes stored above-ground living biomass carbon and not harvesting emissions or broader climate impact.
8. Roots, soil, dead wood, future trajectories, and other excluded carbon pools are not implemented.
9. Unit, integration, dashboard/export, lint, formatting, compilation, and live-contract verification pass.

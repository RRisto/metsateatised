# Estonia BCEF Above-Ground Biomass Carbon Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the wood-density × BEF calculation with Estonia-specific above-ground BCEFs, propagate total pre-harvest stand stock through every species allocation, and rename all public outputs to explicit above-ground biomass fields.

**Architecture:** `carbon.py` owns BCEF coefficients, grouping, stock classes, conversion helpers, allocation context, and carbon result aggregation. `stand_model.py` continues to preserve immutable Forest Register identity and completeness metadata. `app.py` consumes the renamed result contract and exposes the methodology and calculation provenance consistently across analysis, maps, charts, tables, and CSV exports.

**Tech Stack:** Python 3.11+, dataclasses, pandas, GeoPandas, Streamlit, pytest, Ruff, uv.

## Global Constraints

- Use the supplied Estonia BCEF table exactly; BCEF units are tonnes dry above-ground biomass per m³ stem growing stock.
- Do not multiply BCEF by wood density or `BEF = 1.30`.
- Use `CARBON_FRACTION = 0.50` and `CO2_PER_C = 44.0 / 12.0`.
- Select the BCEF class from total pre-harvest living stand stock per hectare, never species stock or planned-harvest volume.
- Preserve original Forest Register species codes and names everywhere; BCEF grouping is calculation-only.
- Missing or unreliable total stand stock must produce unknown/incomplete biomass, never a default stock class.
- Keep standing and planned-harvest quantities semantically separate.
- Rename public outputs without legacy dashboard or CSV aliases.
- Do not add roots, soil, forest floor, dead wood, residue decomposition, harvested wood products, regrowth, counterfactual trajectories, payback time, or avoided sequestration.
- Preserve `streamlit run app.py` as the entrypoint.
- Every production behavior change follows red-green-refactor.

---

### Task 1: Add the BCEF coefficient model and conversion helpers

**Files:**
- Modify: `carbon.py:13-27,127-132,400-406`
- Modify: `tests/test_carbon.py:1-27`
- Modify: `tests/test_carbon_semantics.py:1-15`

**Interfaces:**
- Produces: `BCEF_ABOVEGROUND: dict[str, dict[str, float]]`
- Produces: `BCEF_SPECIES_GROUP: dict[str, str]`
- Produces: `bcef_group_for_species(code: str | None) -> str`
- Produces: `bcef_stock_class(stock_m3_ha: float) -> str`
- Produces: `bcef_for_species(species_code: str | None, stand_stock_m3_ha: float) -> float`
- Produces: `aboveground_biomass_from_species_volume(volume_m3: float, species_code: str | None, stand_stock_m3_ha: float) -> float`
- Defers: replacement of the public `carbon_from_species_volume` signature until Task 3, when every consuming result path can migrate atomically.

- [ ] **Step 1: Write failing stock-class and species-group tests**

Add to `tests/test_carbon.py`:

```python
@pytest.mark.parametrize(
    ("stock", "expected"),
    [
        (0, "le_20"),
        (20, "le_20"),
        (20.0001, "20_50"),
        (50, "20_50"),
        (50.0001, "50_100"),
        (100, "50_100"),
        (100.0001, "gt_100"),
        (300, "gt_100"),
    ],
)
def test_bcef_stock_class(stock, expected):
    assert bcef_stock_class(stock) == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("MA", "pine"),
        ("KU", "spruce"),
        ("KS", "birch"),
        ("HB", "aspen"),
        ("LV", "grey_alder"),
        ("LM", "black_alder"),
        ("TA", "other"),
        ("SA", "other"),
        ("LH", "other"),
        ("VA", "other"),
        ("XX", "other"),
        (None, "other"),
        (" ma ", "pine"),
        ("ku", "spruce"),
    ],
)
def test_bcef_species_group(code, expected):
    assert bcef_group_for_species(code) == expected
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
uv run pytest tests/test_carbon.py -q
```

Expected: collection fails because `bcef_stock_class` and `bcef_group_for_species` do not exist.

- [ ] **Step 3: Add exact coefficient, validation, and mixed-stand tests**

Add tests asserting:

```python
def test_exact_high_stock_coefficients():
    assert bcef_for_species("MA", 200) == pytest.approx(0.5056)
    assert bcef_for_species("KU", 200) == pytest.approx(0.5055)
    assert bcef_for_species("KS", 200) == pytest.approx(0.5992)
    assert bcef_for_species("TA", 200) == pytest.approx(0.4899)


@pytest.mark.parametrize("stock", [-1, float("nan"), float("inf")])
def test_bcef_rejects_invalid_stand_stock(stock):
    with pytest.raises(ValueError, match="stand_stock_m3_ha must be non-negative"):
        bcef_for_species("KU", stock)


@pytest.mark.parametrize("volume", [-1, float("nan"), float("inf")])
def test_aboveground_biomass_rejects_invalid_volume(volume):
    with pytest.raises(ValueError, match="volume_m3 must be non-negative"):
        aboveground_biomass_from_species_volume(volume, "KU", 150)


def test_aboveground_biomass_formula_for_spruce_uses_bcef_without_density_or_bef():
    assert aboveground_biomass_from_species_volume(10, "KU", 150) == pytest.approx(
        10 * 0.5055
    )


def test_mixed_stand_uses_one_total_stock_class_for_all_species():
    actual = sum(
        carbon_from_species_volume(volume, code, 200)
        for code, volume in [("MA", 60), ("KS", 30), ("KU", 10)]
    )
    expected = (60 * 0.5056 + 30 * 0.5992 + 10 * 0.5055) * 0.50 * (44 / 12)
    assert actual == pytest.approx(expected)
```

- [ ] **Step 4: Implement the BCEF model minimally**

In `carbon.py`, add the exact coefficient table:

```python
BCEF_ABOVEGROUND = {
    "pine": {"le_20": 0.6057, "20_50": 0.5587, "50_100": 0.5429, "gt_100": 0.5056},
    "spruce": {"le_20": 0.5714, "20_50": 0.5455, "50_100": 0.5321, "gt_100": 0.5055},
    "birch": {"le_20": 0.7085, "20_50": 0.6321, "50_100": 0.6148, "gt_100": 0.5992},
    "aspen": {"le_20": 0.4246, "20_50": 0.4383, "50_100": 0.4179, "gt_100": 0.4365},
    "grey_alder": {"le_20": 0.4045, "20_50": 0.4181, "50_100": 0.4313, "gt_100": 0.4395},
    "black_alder": {"le_20": 0.4281, "20_50": 0.4669, "50_100": 0.4768, "gt_100": 0.4844},
    "other": {"le_20": 0.4914, "20_50": 0.4889, "50_100": 0.4852, "gt_100": 0.4899},
}
BCEF_SPECIES_GROUP = {
    "MA": "pine", "KU": "spruce", "KS": "birch", "HB": "aspen",
    "LV": "grey_alder", "LM": "black_alder",
}
```

Then implement:

```python
def bcef_group_for_species(code: str | None) -> str:
    normalized = str(code).strip().upper() if code is not None else ""
    return BCEF_SPECIES_GROUP.get(normalized, "other")


def bcef_stock_class(stock_m3_ha: float) -> str:
    if stock_m3_ha <= 20:
        return "le_20"
    if stock_m3_ha <= 50:
        return "20_50"
    if stock_m3_ha <= 100:
        return "50_100"
    return "gt_100"


def bcef_for_species(species_code: str | None, stand_stock_m3_ha: float) -> float:
    if not isfinite(stand_stock_m3_ha) or stand_stock_m3_ha < 0:
        raise ValueError("stand_stock_m3_ha must be non-negative")
    return BCEF_ABOVEGROUND[bcef_group_for_species(species_code)][
        bcef_stock_class(stand_stock_m3_ha)
    ]


def aboveground_biomass_from_species_volume(
    volume_m3: float,
    species_code: str | None,
    stand_stock_m3_ha: float,
) -> float:
    if not isfinite(volume_m3) or volume_m3 < 0:
        raise ValueError("volume_m3 must be non-negative")
    return volume_m3 * bcef_for_species(species_code, stand_stock_m3_ha)
```

Do not change the existing public `carbon_from_species_volume` signature in this task. Task 3 migrates that function and every result consumer together, keeping the full suite green at each review boundary. Keep old density symbols isolated to the legacy helper until Task 3 removes that calculation path.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run:

```powershell
uv run pytest tests/test_carbon.py -q
uv run ruff check carbon.py tests/test_carbon.py
```

Expected: all focused tests pass and Ruff reports no errors.

- [ ] **Step 6: Commit Task 1**

```powershell
git add carbon.py tests/test_carbon.py tests/test_carbon_semantics.py
git commit -m "Add Estonia BCEF carbon model"
```

---

### Task 2: Propagate total pre-harvest stand stock through allocations

**Files:**
- Modify: `carbon.py:40-50,145-397`
- Modify: `tests/test_carbon_semantics.py:16-550`
- Modify: `tests/test_live_forest_register.py:20-56`

**Interfaces:**
- Consumes: Task 1 BCEF helpers.
- Produces: `SpeciesVolume.stand_stock_m3_ha: float | None`
- Preserves: `source_record_id`, `stratum_code`, `inventory_age`, `current_age`, species code, and species name.
- Guarantees: all allocations derived from one `StandRecord` carry its reliable total pre-harvest stock.

- [ ] **Step 1: Write failing propagation and identity tests**

Add focused tests to `tests/test_carbon_semantics.py`:

```python
def test_standing_allocations_carry_total_stand_stock():
    stand = _stand(
        stock_m3_ha=200,
        species=(SpeciesRecord("KU", "Kuusk", 100, 200, None, None),),
    )
    estimate = estimate_standing_volume(stand, overlap_ha=0.5)
    assert {item.stand_stock_m3_ha for item in estimate.species_volumes} == {200}


def test_planned_allocations_use_preharvest_stock_not_harvest_volume():
    stand = _stand(stock_m3_ha=250, species=SPECIES_WITH_SHARES)
    estimate = estimate_planned_harvest_volume(180, stand=stand)
    assert {item.stand_stock_m3_ha for item in estimate.species_volumes} == {250}
    assert all(bcef_stock_class(item.stand_stock_m3_ha) == "gt_100" for item in estimate.species_volumes)


def test_oak_identity_is_preserved_while_calculation_group_is_other():
    oak = SpeciesRecord(code="TA", name=species_name_for_code("TA"), share_pct=100)
    assert oak.name == "Tamm"
    assert bcef_group_for_species(oak.code) == "other"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
uv run pytest tests/test_carbon_semantics.py -q
```

Expected: failures because `SpeciesVolume` has no `stand_stock_m3_ha` and constructors do not populate it.

- [ ] **Step 3: Extend `SpeciesVolume` and all allocation constructors**

Add the field without changing Forest Register identity:

```python
@dataclass(frozen=True)
class SpeciesVolume:
    species_code: str | None
    volume_m3: float
    stand_stock_m3_ha: float | None = None
    source_record_id: int | str | None = None
    stratum_code: str | None = None
    inventory_age: float | None = None
    current_age: float | None = None
```

Change `_species_volume`, `_allocate_by_weights`, `_allocate_by_shares`, `_allocate_wfs_species`, and `_allocate_detail_strata_by_shares` so their signatures receive the reliable total stand stock and pass it to every returned row. Known-zero sentinel allocations must also carry the stock context when available.

- [ ] **Step 4: Add failing missing/partial stock tests**

Add tests showing:

```python
def test_missing_total_stock_keeps_allocated_biomass_incomplete():
    stand = _stand(stock_m3_ha=None, species=SPECIES_WITH_SHARES)
    planned = estimate_planned_harvest_volume(10, stand=stand)
    assert planned.is_complete is False
    assert all(item.stand_stock_m3_ha is None for item in planned.species_volumes)


def test_incomplete_species_stock_is_not_reconstructed_as_total_stock():
    stand = _stand(
        stock_m3_ha=None,
        species=(
            SpeciesRecord("KU", "Kuusk", 70, 140, None, None),
            SpeciesRecord("KS", "Kask", 30, None, None, None),
        ),
    )
    planned = estimate_planned_harvest_volume(10, stand=stand)
    assert all(item.stand_stock_m3_ha is None for item in planned.species_volumes)
    assert planned.is_complete is False
```

Also extend real-schema multi-stratum tests to assert every allocation carries the whole stand's `stock_m3_ha`, not stratum/species stock.

- [ ] **Step 5: Implement completeness-aware stock propagation**

Use `StandRecord.stock_m3_ha` when finite and non-negative. Reconstruct only through the existing `_complete_detail_species` path when all living-stock records are proven complete; otherwise retain `None` and mark the volume/biomass estimate incomplete. Do not use notice volume as stock context.

- [ ] **Step 6: Run allocation and live-contract tests**

Run:

```powershell
uv run pytest tests/test_carbon_semantics.py tests/test_live_forest_register.py -q
uv run ruff check carbon.py tests/test_carbon_semantics.py tests/test_live_forest_register.py
```

Expected: all offline tests pass; the opt-in live test remains skipped unless enabled.

- [ ] **Step 7: Commit Task 2**

```powershell
git add carbon.py tests/test_carbon_semantics.py tests/test_live_forest_register.py
git commit -m "Propagate stand stock into biomass allocations"
```

---

### Task 3: Convert notice results and aggregation to explicit above-ground fields

**Files:**
- Modify: `carbon.py:52-124,409-451`
- Modify: `tests/test_carbon_semantics.py:92-110,520-550`
- Modify: `tests/test_aggregation.py:1-90`

**Interfaces:**
- Consumes: `SpeciesVolume.stand_stock_m3_ha` and Task 1 conversion helpers.
- Produces: `NoticeCarbonEstimate.standing_aboveground_biomass_tco2: float | None`
- Produces: `NoticeCarbonEstimate.planned_harvest_aboveground_biomass_tco2: float | None`
- Produces dataframe columns with the same names and no old aliases.
- Replaces: `carbon_from_species_volume(volume_m3: float, species_code: str | None)` with `carbon_from_species_volume(volume_m3: float, species_code: str | None, stand_stock_m3_ha: float) -> float` atomically with every production caller.

- [ ] **Step 1: Rename tests first and verify RED**

Replace old result assertions with:

```python
assert result.standing_aboveground_biomass_tco2 == pytest.approx(
    carbon_from_species_volume(100, "KU", 150)
)
assert result.planned_harvest_aboveground_biomass_tco2 == pytest.approx(
    carbon_from_species_volume(40, "KU", 150)
)
assert not hasattr(result, "standing_live_biomass_tco2")
assert not hasattr(result, "planned_harvest_biomass_tco2")
```

Update aggregation fixtures to use only:

```text
standing_aboveground_biomass_tco2
planned_harvest_aboveground_biomass_tco2
```

Run:

```powershell
uv run pytest tests/test_carbon_semantics.py tests/test_aggregation.py -q
```

Expected: failures because production dataclasses and aggregators still use the old names.

- [ ] **Step 2: Add failing completeness tests for BCEF conversion**

Cover these exact cases:

```python
def test_known_zero_with_stock_context_is_complete_zero(): ...
def test_positive_volume_without_stock_context_is_unknown(): ...
def test_mixed_known_and_missing_stock_context_is_not_partially_summed(): ...
def test_unknown_species_with_known_stock_uses_other_and_remains_complete(): ...
```

The mixed case must return `None`, not a partial sum.

- [ ] **Step 3: Rename the result contract and update `calculate_notice_carbon`**

Rename the dataclass fields and aggregator mappings. Replace `carbon_from_species_volume` with the three-argument BCEF implementation, then migrate all production callers in `carbon.py` and `app.py` in the same step. Implement carbon aggregation as:

```python
def carbon_total(items: Iterable[SpeciesVolume]) -> float | None:
    rows = tuple(items)
    if not rows:
        return None
    if any(item.stand_stock_m3_ha is None for item in rows):
        return None
    return sum(
        carbon_from_species_volume(
            item.volume_m3,
            item.species_code,
            item.stand_stock_m3_ha,
        )
        for item in rows
    )
```

Retain existing known-zero behavior and volume-basis completeness; do not let a non-empty partial allocation appear complete.

- [ ] **Step 4: Add calculation provenance to species rows**

Update the species-row output generated by `allocate_volume_by_species` or its replacement to include:

```python
{
    "species_code": item.species_code,
    "species_name": species_name_for_code(item.species_code),
    "bcef_group": bcef_group_for_species(item.species_code),
    "stand_stock_m3_ha": item.stand_stock_m3_ha,
    "bcef_stock_class": bcef_stock_class(item.stand_stock_m3_ha),
    "bcef": bcef_for_species(item.species_code, item.stand_stock_m3_ha),
    "volume_m3": item.volume_m3,
    "aboveground_biomass_t": biomass_t,
    "aboveground_carbon_t": biomass_t * CARBON_FRACTION,
    "aboveground_co2e_t": biomass_t * CARBON_FRACTION * CO2_PER_C,
}
```

Only emit coefficient-derived values when stand stock is present; otherwise emit `None` and preserve the original identity/provenance fields.

- [ ] **Step 5: Run Task 3 tests and lint**

Run:

```powershell
uv run pytest tests/test_carbon.py tests/test_carbon_semantics.py tests/test_aggregation.py -q
uv run ruff check carbon.py tests/test_carbon.py tests/test_carbon_semantics.py tests/test_aggregation.py
```

- [ ] **Step 6: Commit Task 3**

```powershell
git add carbon.py tests/test_carbon.py tests/test_carbon_semantics.py tests/test_aggregation.py
git commit -m "Rename above-ground biomass result contract"
```

---

### Task 4: Migrate analysis, dashboard, map, charts, and CSV exports

**Files:**
- Modify: `app.py:18-29,291-680,686-975`
- Modify: `tests/test_dashboard.py:1-170`
- Modify: `tests/test_quality_metrics.py:56-330`

**Interfaces:**
- Consumes: renamed Task 3 result fields and per-species BCEF provenance.
- Produces: public dataframe/export columns `standing_aboveground_biomass_tco2`, `standing_aboveground_biomass_tco2_ha`, and `planned_harvest_aboveground_biomass_tco2`.
- Removes: all user-facing reads and exports of the three legacy biomass fields.

- [ ] **Step 1: Rename dashboard and export fixtures first**

Update fixtures and expected export allowlists in `tests/test_dashboard.py`. Add explicit exclusions:

```python
legacy = {
    "standing_live_biomass_tco2",
    "standing_live_biomass_tco2_ha",
    "planned_harvest_biomass_tco2",
}
assert legacy.isdisjoint(export.columns)
```

Require the new three columns, plus existing volume basis, completeness, inventory, age, and coverage columns.

- [ ] **Step 2: Add failing UI and provenance assertions**

Assert rendered Streamlit/map text contains:

```text
Maapealse elusa biomassi süsinikuvaru
Kavandatava raiemahu maapealne biomass
puistu kogu hektaritagavara
BCEF
mitte hinnang raiest koheselt atmosfääri eralduvale CO₂-le
```

Assert old methodology text `puidutihedus × BEF 1.30` is absent. Add species-table assertions for `bcef_group`, `stand_stock_m3_ha`, `bcef_stock_class`, and `bcef` while preserving display name `Tamm` for `TA`.

- [ ] **Step 3: Run dashboard tests and verify RED**

Run:

```powershell
uv run pytest tests/test_dashboard.py tests/test_quality_metrics.py -q
```

Expected: failures on old column names and old methodology copy.

- [ ] **Step 4: Migrate `analyze` and export schema**

Rename empty-result initialization, intersection rows, aggregate fields, per-hectare calculation, sorting, and export allowlist. Update every call to:

```python
carbon_from_species_volume(
    volume_m3=item.volume_m3,
    species_code=item.species_code,
    stand_stock_m3_ha=item.stand_stock_m3_ha,
)
```

Carry BCEF provenance into the species analysis dataframe without overwriting species code/name.

- [ ] **Step 5: Migrate all user-facing consumers and methodology copy**

Update map color values, popups, KPI totals, chart fields and labels, tables, and descriptions. Use the design's Estonian methodology statement and explicitly state the exclusions. Keep standing and planned harvest visibly separate.

- [ ] **Step 6: Prove no legacy public reads remain**

Run:

```powershell
rg -n "standing_live_biomass_tco2|standing_live_biomass_tco2_ha|planned_harvest_biomass_tco2|puidutihedus.*BEF" app.py tests/test_dashboard.py tests/test_quality_metrics.py
```

Expected: no matches. Mentions in migration documentation are allowed outside runtime/UI/test contracts.

- [ ] **Step 7: Run Task 4 tests and lint**

Run:

```powershell
uv run pytest tests/test_dashboard.py tests/test_quality_metrics.py tests/test_aggregation.py -q
uv run ruff check app.py tests/test_dashboard.py tests/test_quality_metrics.py
```

- [ ] **Step 8: Commit Task 4**

```powershell
git add app.py tests/test_dashboard.py tests/test_quality_metrics.py
git commit -m "Expose above-ground BCEF results in dashboard"
```

---

### Task 5: Update documentation, live contract, and complete verification

**Files:**
- Modify: `README.md:65-132`
- Modify: `tests/test_live_forest_register.py:1-60`
- Modify: any test fixture still using old public names, identified by the repository-wide search below.

**Interfaces:**
- Consumes: final renamed result and BCEF provenance contracts.
- Produces: documented public schema and a live assertion that both estimates have reliable pre-harvest stock context.

- [ ] **Step 1: Update the live contract test first**

Require:

```python
assert carbon.standing_aboveground_biomass_tco2 > 0
assert carbon.planned_harvest_aboveground_biomass_tco2 > 0
assert all(item.stand_stock_m3_ha is not None for item in standing.species_volumes)
assert all(item.stand_stock_m3_ha is not None for item in planned.species_volumes)
```

Also assert at least one live species row retains its Forest Register code/name while its BCEF group is calculation-only.

- [ ] **Step 2: Rewrite README methodology and field documentation**

Document the exact equation, coefficient dimensions, new field names, units, missing-data semantics, and explicit exclusions. Remove the old density × BEF formula. State that uncommon species retain their original identity and use `other` only for coefficient lookup.

- [ ] **Step 3: Search and remove stale runtime/test contract names**

Run:

```powershell
rg -n "standing_live_biomass_tco2|standing_live_biomass_tco2_ha|planned_harvest_biomass_tco2|BEF = 1\.30|puidutihedus.*BEF" --glob '!docs/superpowers/**'
```

Expected: no public runtime, test, or README matches. If legacy constants remain for a private compatibility helper, add a test proving the primary calculation does not reference them and document why they remain.

- [ ] **Step 4: Run the full offline quality gate**

Run:

```powershell
uv sync
uv run pytest -q
uv run ruff check app.py carbon.py stand_model.py forest_data.py wfs.py data_cache.py tests
uv run ruff format --check app.py carbon.py stand_model.py forest_data.py wfs.py data_cache.py tests
uv run python -m py_compile app.py carbon.py stand_model.py forest_data.py wfs.py data_cache.py
git diff --check
```

Expected: all offline tests pass with only the opt-in live test skipped; lint, format, compilation, and diff checks pass.

- [ ] **Step 5: Run the opt-in live Forest Register contract**

Run:

```powershell
$env:RUN_LIVE_TESTS='1'
uv run pytest -q -m live
```

Expected: the live contract passes. If the public service times out, record the network failure separately; do not weaken assertions or mark implementation complete without a later successful live run.

- [ ] **Step 6: Commit Task 5**

```powershell
git add README.md tests/test_live_forest_register.py tests
git commit -m "Document Estonia BCEF methodology"
```

- [ ] **Step 7: Final requirements audit**

Check each acceptance criterion in `docs/superpowers/specs/2026-08-10-estonia-bcef-carbon-design.md` against the final diff. Confirm that all public names are renamed, all species allocations carry total stand stock or explicit incompleteness, and no excluded carbon pool or trajectory logic was introduced.

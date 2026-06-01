# PLAN — Chart Model layer

## Problem

Every metric component (`web/components/cfd.py`, `aging.py`,
`cycle_time.py`, `forecast.py`, `throughput.py`, `data_source.py`)
does three jobs in two functions:

- `render(con, name, *, view)` — SQL data access **and** metric
  computation.
- `XData.vega_spec_json()` — chart-shaping **decisions** and
  Vega-Lite spec construction.

The *decisions* — percentile thresholds, cap/crop slider bounds,
window clamping, tick density, empty-state classification,
headline text — are spread across both functions and duplicated
across charts. The aging and cycle-time cap sliders are the same
control, but they drifted: one floored at P95-of-the-data, one at
the P95 reference line, until a hand-fix reconciled them. A change
to "how a range slider behaves" or "how percentiles are sourced"
ripples through every component, and the only way to test a
decision today is to stand up a DuckDB warehouse and parse the
emitted Vega JSON.

`windows.py` already proves the cure for **one** decision family:
`parse_windows()` is the single stateless resolver for date math,
and `WindowSelection` is the one object every view reads. This
plan extends that pattern to the rest of the chart logic.

## Target — three layers

```
LAYER 1  data access      flowmetrics/warehouse/queries.py
  Pure SQL. Returns raw typed rows. No windows, no decisions.
        completed_items(con, name, window) -> list[CompletedRow]
        stage_transitions(con, name)       -> list[TransitionRow]
        in_flight_snapshot(con, name, asof)-> list[InFlightRow]

LAYER 2  chart model      flowmetrics/charts/
  Stateless, pure Python. Raw rows + WindowSelection in; a fully
  resolved model out. Every assumption decided here. No DuckDB,
  no Vega, no colour tokens. THIS is the testable core.
        build_cfd_model(rows, window)        -> CfdModel
        build_aging_model(rows, window, asof)-> AgingModel
        build_cycle_time_model(rows, window) -> CycleTimeModel

LAYER 3  view             flowmetrics/web/components/*.py
  Model in, Vega-Lite spec dict out. Mechanical translation only.
  Owns colour tokens, fonts, axis layout, layer structure, the
  jitter encoding. Makes NO decisions — reads model.cap.floor,
  model.ticks.interval, model.empty_state, etc.
```

The win is Layer 2: it has no I/O, so its tests construct typed
inputs directly and assert the resolved model — no warehouse
fixtures, milliseconds per test, exhaustive edge coverage. Layer 3
becomes mechanical enough that one "the number reached the spec"
test per chart suffices.

## Shared model primitives — `flowmetrics/charts/primitives.py`

These are the decisions duplicated across charts today. One
definition, one test suite, every chart composes them.

- **`Percentiles`** — `p50 / p85 / p95`, plus `source_count`,
  `source_window`, and the `smell` flag. Bakes in: percentiles
  are empirical (not Monte Carlo), drawn from completed cycle
  times in the reference window. Used by aging + cycle-time.
- **`RangeControl`** — `floor / ceiling / default / visible /
  label`. Bakes in: the cap/crop slider *filters* (re-scales the
  axis), never pins the domain; default = show everything;
  hidden when `floor >= ceiling`. Used by the aging cap, the
  cycle-time cap, the CFD crop.
- **`TickPolicy`** — `interval / step`. Bakes in the span-adaptive
  day/week/month rule so no axis hatches the plot with one
  gridline per day. Used by cycle-time now; CFD next.
- **`EmptyState`** — `kind / message`. Bakes in the
  "no data vs. window-too-narrow vs. snapshot-never-captured"
  classification so the view just renders a string.

## Per-chart models — `flowmetrics/charts/<chart>.py`

Each `XModel` composes the primitives plus its own series and
headline. e.g. `CfdModel` = the clamped visible window, the daily
cumulative series, the stage order, a `RangeControl` for the crop
slider, a `TickPolicy`, the headline, and the
terminal-equals-completed invariant check.

## Phasing — vertical slices, tests green at every step

Strict TDD throughout (test first). **One chart at a time, end to
end through all three layers** — a vertical slice. This respects
the "don't abstract until the second use" rule (Non-goals):
primitives move into `primitives.py` the moment a second chart
needs them, never speculatively.

- **Pre-work — test hygiene.** Fix `test_zoom_browser.py`'s
  sample-regeneration side effect (see Testing) before touching
  components, so the refactor's many test runs don't dirty the
  tree.
- **Slice 1 — cycle-time, end to end.** `warehouse/queries.py`
  for its data access; `charts/cycle_time.py` for the model
  (percentiles, cap control, tick policy, empty-state, headline)
  — decision logic kept LOCAL to the module, not yet shared;
  `web/components/cycle_time.py` shrinks to `to_vega(model)`.
  Proves the three-layer pattern end to end.
- **Slice 2 — aging, end to end. The abstraction gate.** Aging's
  cap control + percentiles are the same as cycle-time's — the
  SECOND use. Extract `RangeControl`, `Percentiles` (and
  `EmptyState`) into `primitives.py` now, and retrofit
  cycle-time onto them. Reconcile the cap-floor drift for good.
- **Slice 3 — CFD.** Reuses `RangeControl` (crop slider); needs
  span-adaptive ticks — extract `TickPolicy` here.
- **Slice 4 — throughput, forecast, data-source.**
- **Slice 5 — collapse.** Confirm no `vega_spec_json` method or
  `XData` dataclass remains; delete dead code.

Behaviour is preserved at every slice — the model uses a Python
linear-interpolation percentile that matches today's DuckDB
`percentile_cont`, so no displayed number moves.

## Testing — strategy, pre-work, gaps

From a full audit of the chart/metric test suite.

### Principle

Tests concentrate in Layer 2: pure functions, typed inputs, no
DuckDB, no JSON — assert the resolved model. Layer 3 keeps **one**
thin "the model value reached the spec" check per chart. The many
current tests that `json.loads(vega_spec_json())` and assert param
names (`agecap`, `cyclecap`, `cfdfloor`), layer indices
(`spec["layer"][1]`), filter-transform strings and encoding
internals get **rewritten as Layer-2 model assertions** — they
test what is expected, not how the spec is shaped.

### Pre-work — before Phase 0

- **Fix `test_zoom_browser.py`.** Its `regenerate_samples`
  session fixture runs `scripts/generate_samples.py`, which
  rewrites the git-**tracked** `samples/` tree — every browser
  run dirties the working copy (the churn we just kept out of a
  commit by hand). Render samples to `tmp_path`, or gitignore
  `samples/`. `test_cfd_horizontal_guideline.py` also reads
  `samples/` — give it its own render.
- **Hoist the warehouse fixture.** The `materialize → CREATE
  VIEW` fixture is copy-pasted in six component test files.
  Move it to `conftest.py`, session-scoped. Most uses vanish
  once Layer 2 needs no warehouse; this de-risks the interim.

### Migration map

- **Keep as-is** — `test_windows.py` is already a Layer-2 model
  test (constructs `WindowSelection` directly, no I/O). The
  template.
- **Seed Layer 2** — `test_cfd.py`, `test_throughput.py`,
  `test_forecast.py` are clean pure-function tests of the
  `flowmetrics.{cfd,throughput,forecast}` compute modules;
  `test_chart_percentiles.py` + the percentile tests in
  `test_aging.py` seed the shared `Percentiles` suite. These are
  **not** duplicates of the `*_component.py` files — different
  modules — do not delete.
- **Split** — each `*_component.py`: decision assertions (cap
  bounds, tick density, empty-state, clamping, percentile
  values, headline) move to Layer-2 model tests and shed their
  fixtures; encoding-mechanics assertions (mark types,
  `scale.type`, colour fields, jitter/dash strings, layer
  indices) collapse to one thin Layer-3 spec check per chart.
- **Shrink** — e2e files (`test_cfd_window_e2e`,
  `test_filter_windows_e2e`, `test_forecast_fit_e2e`,
  `test_data_source_e2e`) stay as browser smoke tests, one or
  two cases each.
- **Out of scope** — `test_vega_specs.py` (~90 tests) targets
  the *separate, older* `renderers/vega_specs.py` CLI renderer,
  not `web/components/`. Earmark for a later reconciliation.

### Coverage gaps — write these with the phase that owns them

- `RangeControl` emits **no control** when `floor >= ceiling` —
  the "nothing to crop" path is silently untested today.
- Single-data-point warehouse — caps need `len >= 2`; the
  graceful no-cap path is untested.
- Empty-state matrix — cycle-time covers 2 of N branches; the
  CFD "window entirely outside the data" path has no test.
- Percentiles must shift when the reference window narrows —
  only the default reference is exercised today.
- Tick-density boundary values (exactly 30 / 210 / 1095 days);
  CFD's own label thinning has no density test.
- All-items-above-the-cap (range collapses); CFD interior
  no-transition days (NODATA vs. carried-forward).

## Decisions (resolved)

1. **`XData` merges into `XModel`.** The model *is* the template
   payload — no adapter. Jinja templates read the model directly.
2. **Vega spec is a free function.** `XData.vega_spec_json()`
   becomes `to_vega(model) -> dict` in the view layer; templates
   call a component function, not a dataclass method.
3. **New packages.** `flowmetrics/charts/` for Layer 2,
   `flowmetrics/warehouse/` for Layer 1. `windows.py` stays put —
   Layer 2 consumes `WindowSelection`, doesn't absorb it.

## Non-goals

- No generic charting framework. Six metrics, concrete models.
- Don't abstract a primitive until the second chart needs it
  (Slice 2 is the gate). A primitive used once stays inline.
- `samples/` regeneration is a separate hygiene task — out of
  scope here.

## Known duplication — deliberately deferred

`flowmetrics/{cfd,aging,throughput,forecast,compute,percentiles}.py`
plus `renderers/vega_specs.py` are the **CLI/report** chart path.
The web components reimplement that logic independently — and the
two have already *diverged*: the web computes percentiles with
linear interpolation (`percentile_cont`), the CLI with
`percentiles.chart_percentiles`' ceil-index rule; the web CFD reads
the `transitions` table, the CLI CFD walks `WorkItem.status_intervals`.

This refactor restructures only the **web** path. It does not add a
copy — the web copy already exists — but it does not unify web↔CLI
either. Unifying the two paths, and resolving the percentile-method
divergence, is a known follow-up, tracked alongside the
`test_vega_specs.py` reconciliation. `flowmetrics/charts/` (web
chart model) therefore coexists with `flowmetrics/cfd.py` et al.
until that follow-up.

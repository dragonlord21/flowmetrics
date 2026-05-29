# Spec: Wire step `matches` into materialisation

Status: **DRAFT — for review. Do not implement until approved.**

## Objective

Make the workflow **steps** a user defines in the contract builder
actually drive how source data is bucketed into stages — so the
dashboard's CFD, aging, cycle-time and throughput reflect the user's
workflow, not the source adapter's hard-coded vocabulary. Today the
steps/`matches` are **preview-only**; this spec closes that gap.

## Current state (verified)

- A step is `{name, wip, matches: [str]}` (`contract.py`). `matches`
  resolves to `effective_matches` = `matches` or `(name,)`.
- `matches` is consumed in exactly **one** place: the dry-run preview
  bucketer `source_probe.bucket_items_by_step` (called from the
  `_dry-run` endpoint, `app.py:891`). Matching there is:
  - **OR**, by plain **string equality** against an item's single
    `current_stage` (`source_probe.py:295` — `if stage in matches`);
  - **untyped** — a match string is compared to the stage text with no
    notion of "label" vs "status" vs "lifecycle event".
- The **real** materialise pipeline does **not** use `matches`. The
  source adapters emit their own `StageTransition(item_id, entered_at,
  stage, signal)` rows:
  - GitHub PR review lifecycle stages: `Draft / Awaiting Review /
    Changes Requested / Approved / Merged` (`sources/github.py:236`);
    label-mode uses the label name as the stage.
  - Jira: the raw status name (`sources/intervals.py:53`, `iv.status`).
  - `signal` is a named event constant (`signals.py`):
    `github-pr-created`, `github-pr-ready-for-review`,
    `github-pr-review-changes-requested`, `github-pr-review-approved`,
    `github-pr-merged`, `github-label-added/removed`,
    `jira-status-changed`, `jira-resolved`, …
- The only contract→dashboard influence is `Contract.states.wip` — the
  set of step **names** flagged WIP — used to filter aging/CFD WIP
  (`aging.py:60`, `app.py:1625/1894`). This silently works **only when
  step names equal the adapter's stage names**. The builder's chip
  vocabulary (`PR opened`, `Marked ready for review`, …) does **not**
  equal the adapter stages, so a UI-built workflow's WIP filter matches
  nothing.

### Answers to the two questions that prompted this

1. **AND across criteria?** No. `matches` is a flat OR list. There is no
   way to express "label A AND label B AND PR open".
2. **Per-platform typing?** No. `matches: - ready` means literally
   `current_stage == "ready"`; a label `ready`, a status `ready`, and a
   lifecycle event are indistinguishable.

## Key insight

Each transition already carries **both** axes the user is conflating:
- `signal` → the **named lifecycle event** ("PR merged", "status
  changed"), and
- `stage` → the **label / status text**.

So a *typed* matcher can target the right axis without new fetching:
`{signal: github-pr-merged}` vs `{label: "ready"}` vs `{status: "In
Progress"}`.

## Proposed design

### 1. Typed matchers (replaces flat string `matches`)

A step's matcher is one of:

| Kind     | Matches when…                                  | Source axis |
|----------|------------------------------------------------|-------------|
| `label`  | a `github-label-added` stage == value          | `stage`     |
| `status` | a Jira status (`stage`) == value               | `stage`     |
| `event`  | a transition `signal` == the named event       | `signal`    |
| `stage`  | raw adapter stage text == value (escape hatch) | `stage`     |

YAML shape (backward compatible — a bare string keeps today's meaning,
"match the stage text"):

```yaml
steps:
  - name: In Review
    wip: true
    matches:
      - event: PR marked ready for review   # signal match
      - event: Changes requested
      - label: needs-review                 # stage/label match
  - name: Done
    wip: false
    matches:
      - event: PR merged
```

- **Within a step: OR** — the item enters the step on *any* matching
  transition. (Matches today's mental model.)
- **No cross-criteria AND in v1.** "Label A AND label B AND open" needs
  point-in-time snapshot state, which only the label-mode snapshot has
  (not the transition stream). Defer; OR-of-typed-matchers covers the
  common cases. (Open question below.)

### 2. A remap layer at materialise time

Add a pure function that relabels adapter transitions to the user's
step names before they're written to the warehouse:

```
remap_transitions(raw: list[StageTransition], steps) -> list[StageTransition]
# each raw transition whose (signal|stage) matches a step's matcher
# is rewritten stage=<step.name>; unmatched transitions are dropped
# (or kept under an "_unmatched" stage — see open questions).
```

Result: the warehouse `stage` column holds the user's step names, so
`Contract.states.wip` (step names) lines up automatically and every
downstream metric reflects the user's workflow. Contracts with **no
steps** skip remapping → today's adapter-native behavior (backward
compatible).

### 3. Builder + dry-run alignment

- The builder chips already separate "Labels in the repo" from
  "Lifecycle events" — wire those to emit `label:`/`event:` typed
  matchers instead of bare strings.
- `bucket_items_by_step` (dry-run) and `remap_transitions` (materialise)
  must share the **same** matcher-evaluation function so the preview is
  faithful to what materialise will do. (Today they diverge — that's the
  root of the confusion.)

## Boundaries

- **Always:** keep a no-steps contract working exactly as today
  (adapter-native stages); share one matcher evaluator between
  preview and materialise.
- **Ask first:** changing the warehouse `stage` semantics (remap vs
  raw); dropping vs bucketing unmatched transitions; the YAML schema
  change.
- **Never:** silently change existing windowed/role contracts' output
  without a migration path.

## Open questions (need a decision before implementation)

1. **Remap at materialise (rewrite `stage`) vs. remap at query time
   (keep raw, map in the view)?** Materialise-time is simpler downstream
   but requires re-materialising when steps change; query-time is
   flexible but spreads matcher logic into every metric.
2. **Unmatched transitions:** drop them, or keep under an `_unmatched`
   stage surfaced in CFD/aging (so the user sees coverage gaps)?
3. **Do we actually need cross-criteria AND** (e.g., "open AND
   label:blocked")? If yes, it's a bigger change (snapshot state, not
   stream) and should be its own spec.
4. **Schema migration:** bare-string `matches` stay valid (= `stage`
   match); do we auto-upgrade the builder's existing contracts' chips to
   typed matchers, or only new ones?

## Success criteria

- A UI-built GitHub PR workflow ("Open / In Review / Merged" with event
  matchers) materialises so the CFD/aging/throughput bucket by those
  three steps, and `states.wip` highlights "In Review".
- The dry-run preview and the materialised result agree item-for-item
  on bucketing (shared evaluator).
- A no-steps contract is byte-for-byte unchanged.
- New unit tests: typed-matcher evaluation (label/status/event/stage),
  `remap_transitions` (incl. unmatched handling), preview/materialise
  parity; one e2e: build a typed workflow → backfill → dashboard buckets
  by the user's steps.

## Test strategy

- Unit: matcher evaluator + `remap_transitions` (pure, table-driven).
- Component: dry-run preview uses the shared evaluator.
- e2e (offline fixture): build "Open/In Review/Merged" → materialise →
  assert warehouse stages are the step names and WIP filter is "In
  Review".

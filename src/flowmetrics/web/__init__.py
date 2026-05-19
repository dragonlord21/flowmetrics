"""Web UI for the warehouse-app (Slice 2+).

`components/` — one module per metric, each producing a typed
MetricData payload that the matching Jinja partial renders. The same
partial is reused on the single-page dashboard (tile mode) and on
the per-metric detail page (detail mode).

`templates/` — Jinja2 templates. `_partials/` holds the reusable
metric components; top-level templates compose them.
"""

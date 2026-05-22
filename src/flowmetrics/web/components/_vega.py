"""Layer-3 view dispatch — `to_vega(model) -> dict`.

Each chart's view module registers a translator from its model
type to a Vega-Lite spec dict. `vega_spec_json` is the single
Jinja global the chart fragment templates call; it resolves the
right translator by model type.
"""

from __future__ import annotations

import json
from functools import singledispatch
from typing import Any


@singledispatch
def to_vega(model: object) -> dict[str, Any]:
    """Translate a chart model into a Vega-Lite spec dict."""
    raise NotImplementedError(
        f"no Vega translator registered for {type(model).__name__}"
    )


def vega_spec_json(model: object) -> str:
    """Compact JSON spec for embedding in a `vegaEmbed(...)` call —
    the Jinja global the chart fragment templates use."""
    return json.dumps(to_vega(model), separators=(",", ":"))

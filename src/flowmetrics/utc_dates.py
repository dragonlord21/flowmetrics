"""UTC-anchored date formatting — the only sanctioned path from a
date/datetime to a display string in this codebase.

Why this module exists
----------------------

Browser-vs-backend timezone bugs are recurring-easy and silent.
Two specific traps:

1. **`datetime.strftime` on a naive datetime** is interpreted in
   the running process's local time — fine on a UTC server, wrong
   on a developer laptop, untestable in CI without TZ pinning.

2. **Vega-Lite tooltip with `type: temporal`** formats the bound
   value in *browser-local* time. A UTC May 04 dot renders as
   "May 03" for a viewer in PT (UTC-7) and "May 04" for a viewer
   in UTC. Same data, different displays.

The fix is to format dates in Python (UTC) and pass the
already-formatted string to the chart as a nominal field. This
module provides the two formatters every component should use:

  - `to_utc_iso_date(d)` — "YYYY-MM-DD"
  - `to_utc_display_date(d)` — "Mon DD, YYYY" (e.g. "May 04, 2026")

Naive datetimes are rejected loudly so the silent-local-time
fallthrough cannot recur. Pair this module with
`tests/test_chart_tooltip_safety.py`, which forbids any chart in
the codebase from using `type: temporal` on a tooltip date field —
the same bug from a different angle.
"""

from __future__ import annotations

from datetime import UTC, date, datetime


def _ensure_utc_date(d: date | datetime) -> date:
    """Common path: produce the UTC calendar date for d.

    - `date`         → returned as-is (no time, no TZ).
    - aware datetime → converted to UTC then truncated to date.
    - naive datetime → ValueError (the silent-bug vector).
    """
    if isinstance(d, datetime):
        if d.tzinfo is None:
            raise ValueError(
                f"naive datetime not allowed: {d!r}. Specify a "
                "timezone (use datetime(..., tzinfo=UTC) for UTC) "
                "so the conversion is explicit. Naive datetimes are "
                "the local-time-vs-UTC silent-bug vector this module "
                "exists to prevent."
            )
        return d.astimezone(UTC).date()
    if isinstance(d, date):
        return d
    raise TypeError(
        f"expected date or datetime; got {type(d).__name__}: {d!r}"
    )


def to_utc_iso_date(d: date | datetime) -> str:
    """Return "YYYY-MM-DD" for d, anchored on UTC."""
    return _ensure_utc_date(d).isoformat()


def to_utc_display_date(d: date | datetime) -> str:
    """Return "%b %d, %Y" (e.g. "May 04, 2026") for d, anchored on
    UTC. Use this for tooltip / dashboard display strings — never
    raw `strftime` on a date from outside this module.
    """
    return _ensure_utc_date(d).strftime("%b %d, %Y")

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


def attach_utc(d: datetime | None) -> datetime | None:
    """Re-attach `tzinfo=UTC` to a naive datetime that we know came
    from a UTC source.

    DuckDB strips timezone info when reading aware-UTC TIMESTAMP
    columns from Parquet — the value is still UTC, we've just lost
    the marker. This helper is the canonical place that handles
    that warehouse-read boundary; every component renderer used to
    inline its own private `_aware()` doing exactly this.

    Workflow:
      - `None`          → `None`   (passthrough)
      - naive datetime  → aware UTC (re-attach the dropped marker)
      - aware datetime  → returned unchanged (do not convert; if
                          the upstream value carries a non-UTC tz,
                          that's a different bug and shouldn't be
                          silently rewritten here)

    Distinct from `to_utc_iso_date` / `to_utc_display_date`, which
    REJECT naive datetimes — those are user-facing formatters where
    silent local-time interpretation is the silent-bug vector. This
    helper is the opposite path: "I know this is UTC because the
    warehouse stores UTC; restore the marker."
    """
    if d is None:
        return None
    if d.tzinfo is None:
        return d.replace(tzinfo=UTC)
    return d

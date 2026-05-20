"""Two user-facing date-range concepts for the dashboard:

- **View window** — clamps chart x-axes. Display-only; doesn't
  change what feeds the math.
- **Reference period** — the statistical sample. Drives
  percentile thresholds (cycle-time, aging) and the Monte Carlo
  throughput sampling distribution (forecast).

Both windows are inclusive on both endpoints — the UI labels
("From" / "To") match. Same-day is a 1-day window, not 0.

URL state:

    ?view_from=YYYY-MM-DD&view_to=YYYY-MM-DD
    &ref_from=YYYY-MM-DD&ref_to=YYYY-MM-DD

Missing or invalid params → fall back to defaults anchored to
today UTC: 30-day view, 14-day reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


DEFAULT_VIEW_DAYS = 30
DEFAULT_REFERENCE_DAYS = 14


@dataclass(frozen=True)
class Window:
    """An inclusive date range [from_, to]. Same-day is 1 day."""

    from_: date
    to: date

    @property
    def days_inclusive(self) -> int:
        return (self.to - self.from_).days + 1

    @classmethod
    def last_n_days(cls, n: int, *, today: date) -> "Window":
        """Window of `n` inclusive days ending on `today`."""
        return cls(from_=today - timedelta(days=n - 1), to=today)


def parse_windows(
    query: dict[str, str] | dict, today: date
) -> tuple[Window, Window]:
    """Parse `view_from/view_to/ref_from/ref_to` from a query
    dict. Returns `(view_window, reference_period)`. Falls back
    to the documented defaults when params are missing or
    malformed — never raises on user input.
    """
    def _parse(prefix: str, default_days: int) -> Window:
        from_str = query.get(f"{prefix}_from")
        to_str = query.get(f"{prefix}_to")
        if from_str and to_str:
            try:
                return Window(
                    from_=date.fromisoformat(str(from_str)),
                    to=date.fromisoformat(str(to_str)),
                )
            except ValueError:
                pass  # fall through to default
        return Window.last_n_days(default_days, today=today)

    return (
        _parse("view", DEFAULT_VIEW_DAYS),
        _parse("ref", DEFAULT_REFERENCE_DAYS),
    )

"""Behavioural spec for the UTC-anchored date utilities.

These helpers exist because the browser-vs-backend timezone bug —
Vega-Lite's tooltip with type:temporal formatting in local time —
is recurring-easy and silent. Every date-to-display-string
conversion in this codebase must go through these two functions
so the answer is UTC, always, regardless of the runtime's TZ.

Naive datetimes are rejected loudly to prevent the silent
"interpreted as local time" failure mode.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from flowmetrics.utc_dates import to_utc_display_date, to_utc_iso_date

_PT = timezone(timedelta(hours=-7))  # UTC-7 (Pacific Daylight Time)
_TOKYO = timezone(timedelta(hours=9))  # UTC+9


class TestToUtcIsoDate:
    def test_date_returns_iso_string(self):
        assert to_utc_iso_date(date(2026, 5, 4)) == "2026-05-04"

    def test_aware_utc_datetime_returns_its_date(self):
        d = datetime(2026, 5, 4, 23, 59, 0, tzinfo=UTC)
        assert to_utc_iso_date(d) == "2026-05-04"

    def test_pt_datetime_late_evening_is_next_day_utc(self):
        """2026-05-04 22:00 PT == 2026-05-05 05:00 UTC. The UTC
        date is May 5, not May 4. The user's local clock says
        May 4 but the warehouse stores UTC."""
        d = datetime(2026, 5, 4, 22, 0, tzinfo=_PT)
        assert to_utc_iso_date(d) == "2026-05-05"

    def test_tokyo_datetime_early_morning_is_previous_day_utc(self):
        """2026-05-05 08:00 Tokyo == 2026-05-04 23:00 UTC. UTC date
        is May 4 even though Tokyo says May 5."""
        d = datetime(2026, 5, 5, 8, 0, tzinfo=_TOKYO)
        assert to_utc_iso_date(d) == "2026-05-04"

    def test_naive_datetime_raises(self):
        """Naive datetimes (no tzinfo) are the silent-bug vector:
        Python's strftime treats them as if they were already local
        time, which is the very assumption this module exists to
        forbid. Fail loudly."""
        naive = datetime(2026, 5, 4, 12, 0)  # no tzinfo
        with pytest.raises(ValueError, match="naive datetime"):
            to_utc_iso_date(naive)

    def test_rejects_non_date_input(self):
        with pytest.raises(TypeError):
            to_utc_iso_date("2026-05-04")  # type: ignore[arg-type]


class TestToUtcDisplayDate:
    def test_date_returns_human_format(self):
        assert to_utc_display_date(date(2026, 5, 4)) == "May 04, 2026"

    def test_aware_utc_datetime(self):
        d = datetime(2026, 5, 4, 23, 59, 0, tzinfo=UTC)
        assert to_utc_display_date(d) == "May 04, 2026"

    def test_pt_late_evening_displays_next_day(self):
        """Same UTC-shift logic as iso; the display string follows
        the UTC date too."""
        d = datetime(2026, 5, 4, 22, 0, tzinfo=_PT)
        assert to_utc_display_date(d) == "May 05, 2026"

    def test_naive_datetime_raises(self):
        with pytest.raises(ValueError, match="naive datetime"):
            to_utc_display_date(datetime(2026, 5, 4, 12, 0))

    def test_single_digit_day_zero_padded(self):
        """Vega-Lite axis labels are zero-padded ("May 04"); the
        tooltip display should match so visual + tooltip read the
        same. Single-digit days that don't zero-pad break the
        column-matching assertion the chart relies on."""
        assert to_utc_display_date(date(2026, 5, 4)) == "May 04, 2026"
        assert to_utc_display_date(date(2026, 5, 9)) == "May 09, 2026"

"""Test helper: build a filter-query dict from semantic options
so higher-level tests don't hardcode URL param names. A param
rename then touches this one place, not every test.

(The low-level `test_windows` suite deliberately uses raw dicts
— it IS the test of the param workflow.)
"""

from __future__ import annotations


def window_query(
    *,
    preset: str | None = None,
    custom_ending: str | None = None,
    view_days: int | str | None = None,
    ref_days: int | str | None = None,
) -> dict[str, str]:
    """Filter-query dict from semantic options:

    - ``preset``        → ``?period=<preset>``
    - ``custom_ending`` → ``?period=custom&anchor=<date>``
    - ``view_days``     → the Custom view length
    - ``ref_days``      → the Advanced reference length
    """
    q: dict[str, str] = {}
    if preset is not None:
        q["period"] = preset
    if custom_ending is not None:
        q["period"] = "custom"
        q["anchor"] = custom_ending
    if view_days is not None:
        q["view_days"] = str(view_days)
    if ref_days is not None:
        q["ref_days"] = str(ref_days)
    return q

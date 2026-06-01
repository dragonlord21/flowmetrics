"""Human-readable terminal output using `rich`.

Layout for every report type — answer first, detail last:
    1. Headline (one-sentence panel)
    2. Definition (what this report measures)
    3. Key numbers (percentiles or summary stats)
    4. Key insight (actionable interpretation)
    5. Next actions
    6. Caveats
    ─── detail divider ───
    7. Input parameters
    8. Training window (forecast reports only)
    9. Reproduce command
   10. Per-PR breakdown (efficiency reports only)
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from ..report import (
    EfficiencyReport,
    HowManyReport,
    Report,
    WhenDoneReport,
    cli_invocation,
)


def _fmt_duration(td: timedelta) -> str:
    hours = td.total_seconds() / 3600
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


# Characters that look fine in a UTF-8 terminal but become mojibake
# when text output gets redirected, copy-pasted into latin-1-decoding
# tools, or pasted into email/chat clients that mis-detect the
# encoding. Replace them with ASCII fall-backs in the final string;
# HTML output keeps the unicode glyphs since HTML pages declare
# charset=utf-8 explicitly.
_ASCII_SAFE_MAP = {
    "→": "->",
    "←": "<-",
    "—": "--",
    "–": "-",
}


def _ascii_safe(text: str) -> str:
    for u, ascii_ in _ASCII_SAFE_MAP.items():
        text = text.replace(u, ascii_)
    return text


def render(
    report: Report,
    console: Console | None = None,
    *,
    verbose: bool = False,
) -> str:
    """Render to a string. Default is a one-line headline (terse, pipeable).

    Pass `verbose=True` for the full report (tables, interpretation,
    detail block, vocabulary). The verbose form is what you'd pipe to a
    file or read in a terminal.
    """
    buf = StringIO()
    console = console or Console(file=buf, force_terminal=False, width=100)
    if not verbose:
        # Terse: just the one-sentence headline. No rich styling, no panel
        # borders — pipeable to less / grep / a file viewer.
        console.print(report.interpretation.headline)
        return _ascii_safe(buf.getvalue())

    if isinstance(report, EfficiencyReport):
        _render_efficiency(report, console)
    elif isinstance(report, WhenDoneReport):
        _render_when_done(report, console)
    elif isinstance(report, HowManyReport):
        _render_how_many(report, console)
    else:  # pragma: no cover
        raise TypeError(f"unknown report type: {type(report).__name__}")
    return _ascii_safe(buf.getvalue())


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


def _top(console: Console, report: Report) -> None:
    """Headline panel — the answer in one sentence."""
    console.print(Panel.fit(report.interpretation.headline, style="bold cyan"))


def _caveats(console: Console, report: Report) -> None:
    """Aging-style caveat block: divergence + signal-quality warnings
    get a red banner; everything else lists dim and unheadered. Drop
    silently when there's nothing to surface."""
    if not report.interpretation.caveats:
        return
    other: list[str] = []
    for c in report.interpretation.caveats:
        low = c.lower()
        if "diverge" in low or "doesn't resemble" in low:
            console.print(Panel(c, title="⚠ Signal quality", style="red"))
        else:
            other.append(c)
    for c in other:
        console.print(f"  - {c}", style="dim")


def _detail_divider(console: Console) -> None:
    console.print(Rule(title="Detail", style="dim", align="left"))


def _input_table(report: Report, rows: list[tuple[str, str]]) -> Table:
    inp = Table(title="Input parameters", show_header=False)
    inp.add_column("k", style="dim")
    inp.add_column("v")
    for label, value in rows:
        inp.add_row(label, value)
    return inp


def _reproduce(console: Console, report: Report) -> None:
    console.print("[dim]Reproduce this report[/dim]")
    console.print(f"  {cli_invocation(report)}", style="cyan")


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


def _render_efficiency(report: EfficiencyReport, console: Console) -> None:
    _top(console, report)
    r = report.result

    if r.pr_count == 0:
        _detail_divider(console)
        _reproduce(console, report)
        return

    # ── Slowest-first breakdown — the actionable data ───────────
    has_issue_items = any(p.item_id.startswith("I#") for p in r.per_pr)
    item_noun = "item" if has_issue_items else "PR"
    pr_table = Table(
        title=f"Per-{item_noun} breakdown — portfolio FE {r.portfolio_efficiency * 100:.1f}%"
    )
    pr_table.add_column("#")
    pr_table.add_column("Cycle", justify="right")
    pr_table.add_column("Active", justify="right")
    pr_table.add_column("FE", justify="right")
    pr_table.add_column("Title")
    for p in sorted(r.per_pr, key=lambda p: p.efficiency):
        pr_table.add_row(
            f"{p.item_id}",
            _fmt_duration(p.cycle_time),
            _fmt_duration(p.active_time),
            f"{p.efficiency * 100:.1f}%",
            p.title[:60],
        )
    console.print(pr_table)

    _caveats(console, report)
    _detail_divider(console)

    # ── Compact context ─────────────────────────────────────────
    merged_label = "Items completed" if has_issue_items else "PRs merged"
    rows = [
        ("Repo", report.input.repo),
        ("Window", f"{report.input.start} → {report.input.stop}"),
        (merged_label, str(r.pr_count)),
        ("Total cycle time", _fmt_duration(r.total_cycle)),
        ("Total active time", _fmt_duration(r.total_active)),
    ]
    console.print(_input_table(report, rows))
    _reproduce(console, report)


# ---------------------------------------------------------------------------
# Forecast: when-done
# ---------------------------------------------------------------------------


def _render_when_done(report: WhenDoneReport, console: Console) -> None:
    _top(console, report)

    pct = Table(title="Confidence — by what date will all items be done? (read FORWARD)")
    pct.add_column("Confidence")
    pct.add_column("Completion date")
    for p in [50, 70, 85, 95]:
        pct.add_row(f"{p}%", str(report.percentiles[p]))
    console.print(pct)

    _caveats(console, report)
    _detail_divider(console)

    t = report.training
    rows = [
        ("Repo", report.input.repo),
        ("Items to complete", str(report.input.items)),
        ("Forecast start", report.input.start_date.isoformat()),
        (
            "Training window",
            f"{t.window_start} → {t.window_end}  "
            f"({len(t.daily_samples)} days, {t.total_merges} items, "
            f"{t.avg_per_day:.2f}/day)",
        ),
        ("Runs", f"{report.simulation.runs:,}"),
        ("Seed", str(report.simulation.seed) if report.simulation.seed is not None else "random"),
    ]
    console.print(_input_table(report, rows))
    _reproduce(console, report)


# ---------------------------------------------------------------------------
# Forecast: how-many
# ---------------------------------------------------------------------------


def _render_how_many(report: HowManyReport, console: Console) -> None:
    _top(console, report)

    pct = Table(title="Confidence — minimum items we can commit to (read BACKWARD)")
    pct.add_column("Confidence")
    pct.add_column("Items", justify="right")
    for p in [50, 70, 85, 95]:
        pct.add_row(f"{p}%", str(report.percentiles[p]))
    console.print(pct)

    _caveats(console, report)
    _detail_divider(console)

    t = report.training
    days = (report.input.target_date - report.input.start_date).days + 1
    rows = [
        ("Repo", report.input.repo),
        (
            "Forecast window",
            f"{report.input.start_date} → {report.input.target_date}  ({days} days)",
        ),
        (
            "Training window",
            f"{t.window_start} → {t.window_end}  "
            f"({len(t.daily_samples)} days, {t.total_merges} items, "
            f"{t.avg_per_day:.2f}/day)",
        ),
        ("Runs", f"{report.simulation.runs:,}"),
        ("Seed", str(report.simulation.seed) if report.simulation.seed is not None else "random"),
    ]
    console.print(_input_table(report, rows))
    _reproduce(console, report)



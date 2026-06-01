from .compute import (
    FlowEfficiency,
    WindowResult,
    WorkItem,
    aggregate,
    compute_pr_flow,
)
from .forecast import (
    ResultsHistogram,
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
)
from .service import (
    DEFAULT_CACHE_DIR,
    DEFAULT_GAP,
    DEFAULT_MIN_CLUSTER,
    DEFAULT_TRAINING_DAYS,
    flowmetrics_for_window,
    historical_throughput_samples,
    make_github_source,
    make_jira_source,
    this_week_window,
)
from .sources import Source
from .throughput import daily_throughput

try:
    # Generated at build time by hatch-vcs. Untracked; present in
    # any installed flowmetrics dist and any `uv sync`-ed checkout.
    from ._version import __version__
except ImportError:
    # No build has run (raw clone, no editable install). Fall back
    # to a string that's still PEP-440 valid so importers don't
    # blow up; users will see this if they import flowmetrics
    # straight from a clone with no install step.
    __version__ = "0.0.0+unknown"

__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_GAP",
    "DEFAULT_MIN_CLUSTER",
    "DEFAULT_TRAINING_DAYS",
    "FlowEfficiency",
    "ResultsHistogram",
    "Source",
    "WindowResult",
    "WorkItem",
    "__version__",
    "aggregate",
    "backward_percentile",
    "build_histogram",
    "compute_pr_flow",
    "daily_throughput",
    "flowmetrics_for_window",
    "forward_percentile",
    "historical_throughput_samples",
    "make_github_source",
    "make_jira_source",
    "monte_carlo_how_many",
    "monte_carlo_when_done",
    "this_week_window",
]


def main() -> None:  # entry point declared in pyproject.toml
    from .cli import cli

    cli()

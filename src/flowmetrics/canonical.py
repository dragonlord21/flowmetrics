"""Source-agnostic canonical types — the two-table data model.

Every flow metric in this codebase ultimately reads
`StageTransition` rows against a `WorkflowDef`. Source adapters
(GitHub, Jira, …) translate native events into these rows; the
metric layer never sees source-specific words.

The mental model is two tables:

    work_item(id, title, url, source, created_at, …)
    stage_transition(item_id, entered_at, stage, signal)

`WorkflowDef` is the schema for the `stage` column — the ordered
list of stages the user cares about, plus which of those count
as "in progress" for WIP/aging purposes. Two different repos can
have two different `WorkflowDef`s and still flow through the same
metric code.

`StageTransition.signal` carries a value from `flowmetrics.signals`
so the audit trail is in the data itself — anyone reading a row
can tell which underlying source event produced it without
pattern-matching item IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StageTransition:
    """A single row in the canonical stage-transition stream.

    `item_id` should be globally unique across sources — convention
    is `<source>:<repo_or_project>:<kind>:<n>`, e.g.
    `github:acme/widget:pr:42` or `jira:ENG:issue:1234`. Sources
    own ID minting; the metric layer just compares them.

    `entered_at` is when the item moved INTO `stage`. Exit is
    implicit: the next transition for the same `item_id`.
    """

    item_id: str
    entered_at: datetime
    stage: str
    signal: str


@dataclass(frozen=True)
class WorkflowDef:
    """User-supplied workflow definition — ordered stages plus the
    subset that counts as in-progress.

    Treat `stages` as canonical order (left = upstream, right =
    downstream). The first stage is the "open" boundary; the last
    stage is the terminal/complete boundary. `wip_set` is the
    subset that flow-aging / WIP / cycle-time considers active —
    typically excludes the open/backlog stages and any terminal
    stages.
    """

    stages: tuple[str, ...]
    wip_set: frozenset[str]

    def __post_init__(self) -> None:
        if not self.stages:
            raise ValueError("stages must be non-empty")
        unknown = self.wip_set - set(self.stages)
        if unknown:
            raise ValueError(
                f"wip_set contains stages not in stages: {sorted(unknown)}"
            )

    @property
    def first_stage(self) -> str:
        return self.stages[0]

    @property
    def terminal_stage(self) -> str:
        return self.stages[-1]

"""Behavioural spec for canonical.py — source-agnostic types.

`StageTransition` and `WorkflowDef` are the two canonical types
that every metric reads. Sources translate their native events
into transition rows; the metric layer never sees source-specific
words. The types are pure dataclasses — no I/O, no source
coupling.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flowmetrics import signals
from flowmetrics.canonical import StageTransition, WorkflowDef


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestStageTransition:
    def test_construction_with_required_fields(self):
        tx = StageTransition(
            item_id="github:acme/widget:pr:42",
            entered_at=_ts(2026, 5, 1, 9),
            stage="In Progress",
            signal=signals.SIGNAL_GITHUB_PR_CREATED,
        )
        assert tx.item_id == "github:acme/widget:pr:42"
        assert tx.entered_at == _ts(2026, 5, 1, 9)
        assert tx.stage == "In Progress"
        assert tx.signal == signals.SIGNAL_GITHUB_PR_CREATED

    def test_is_frozen(self):
        tx = StageTransition(
            item_id="x",
            entered_at=_ts(2026, 5, 1, 9),
            stage="X",
            signal=signals.SIGNAL_JIRA_STATUS_CHANGED,
        )
        with pytest.raises((AttributeError, TypeError)):
            tx.stage = "Y"  # type: ignore[misc]


class TestWorkflowDef:
    def test_construction_and_membership(self):
        wf = WorkflowDef(
            stages=("Open", "In Progress", "Review", "Done"),
            wip_set=frozenset({"In Progress", "Review"}),
        )
        assert wf.stages == ("Open", "In Progress", "Review", "Done")
        assert "In Progress" in wf.wip_set
        assert "Done" not in wf.wip_set

    def test_validates_wip_subset_of_stages(self):
        """wip_set must be a subset of stages — a stage flagged as
        WIP that isn't even on the workflow is a configuration
        error."""
        with pytest.raises(ValueError, match="wip_set"):
            WorkflowDef(
                stages=("Open", "Done"),
                wip_set=frozenset({"In Progress"}),  # not in stages
            )

    def test_validates_nonempty_stages(self):
        with pytest.raises(ValueError, match="stages"):
            WorkflowDef(stages=(), wip_set=frozenset())

    def test_is_frozen(self):
        wf = WorkflowDef(stages=("A",), wip_set=frozenset())
        with pytest.raises((AttributeError, TypeError)):
            wf.stages = ("B",)  # type: ignore[misc]

    def test_first_stage_and_terminal_helpers(self):
        wf = WorkflowDef(
            stages=("A", "B", "C"),
            wip_set=frozenset({"B"}),
        )
        assert wf.first_stage == "A"
        assert wf.terminal_stage == "C"

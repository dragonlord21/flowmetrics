"""Behavioural spec for compute_aging_from_stream.

The canonical-stream version of compute_aging. Same return
shape (AgingItem) so renderers don't change; the difference is
what's on the *input* side - this consumer reads the two-table
Stream so it works for any source (or stitched Issue+PR flow)
without per-source logic.

Loaded from the canonical fixture github_issue_pr_stitched.json
so the test exercises a real Issue+PR stitched journey.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from flowmetrics import signals
from flowmetrics.aging import AgingItem, compute_aging_from_stream
from flowmetrics.canonical import StageTransition, WorkflowDef
from flowmetrics.stream import Stream, StreamItem, load_stream_from_json

FIXTURES = Path(__file__).parent / "fixtures" / "canonical"


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _toy_stream() -> Stream:
    wf = WorkflowDef(
        stages=("Open", "Triaged", "In Progress", "Done"),
        wip_set=frozenset({"Triaged", "In Progress"}),
    )
    items = [
        StreamItem("x:1", "in-flight",  None,
                   _ts(2026, 5, 1), None),
        StreamItem("x:2", "done", "https://example.com/2",
                   _ts(2026, 5, 1), _ts(2026, 5, 8)),
    ]
    txs = [
        StageTransition("x:1", _ts(2026, 5, 1), "Open",        "s"),
        StageTransition("x:1", _ts(2026, 5, 3), "Triaged",     "s"),
        StageTransition("x:2", _ts(2026, 5, 1), "Open",        "s"),
        StageTransition("x:2", _ts(2026, 5, 5), "In Progress", "s"),
        StageTransition("x:2", _ts(2026, 5, 8), "Done",        "s"),
    ]
    return Stream(items=items, transitions=txs, workflow=wf)


class TestComputeAgingFromStream:
    def test_excludes_items_whose_current_stage_is_not_in_wip(self):
        s = _toy_stream()
        out = compute_aging_from_stream(s, asof=date(2026, 5, 10))
        # x:2 is in Done (not in WIP) - excluded.
        assert {a.item_id for a in out} == {"x:1"}

    def test_age_days_is_vacanti_cd_minus_sd_plus_one(self):
        s = _toy_stream()
        out = compute_aging_from_stream(s, asof=date(2026, 5, 10))
        a = next(a for a in out if a.item_id == "x:1")
        # CD - SD + 1: (May 10 - May 1) + 1 = 10.
        assert a.age_days == (date(2026, 5, 10) - date(2026, 5, 1)).days + 1

    def test_current_state_is_the_stage_at_asof(self):
        s = _toy_stream()
        out = compute_aging_from_stream(s, asof=date(2026, 5, 10))
        a = next(a for a in out if a.item_id == "x:1")
        assert a.current_state == "Triaged"

    def test_url_flows_through_when_present(self):
        s = _toy_stream()
        # bring x:2 into WIP "as of" early so it surfaces
        out = compute_aging_from_stream(s, asof=date(2026, 5, 6))
        a = next(a for a in out if a.item_id == "x:2")
        assert a.url == "https://example.com/2"

    def test_excludes_items_not_yet_created_on_asof(self):
        s = _toy_stream()
        # April 30 - neither item exists yet.
        out = compute_aging_from_stream(s, asof=date(2026, 4, 30))
        assert out == []

    def test_max_age_days_caps_the_list(self):
        s = _toy_stream()
        # x:1 is age 9 on May 10 - drop with cap 5.
        out = compute_aging_from_stream(s, asof=date(2026, 5, 10), max_age_days=5)
        assert {a.item_id for a in out} == set()

    def test_result_is_aging_item_dataclass_for_renderer_compat(self):
        s = _toy_stream()
        out = compute_aging_from_stream(s, asof=date(2026, 5, 10))
        assert all(isinstance(a, AgingItem) for a in out)


class TestComputeAgingFromIssuePRFixture:
    def test_loads_stitched_fixture_and_reflects_pr_close(self):
        """The stitched fixture has an Issue closed by a PR-merge.
        At asof=May 1, the Issue is in WIP (Triaged). At asof=April 20,
        the Issue is Done - not in WIP - so excluded.
        """
        s = load_stream_from_json(FIXTURES / "github_issue_pr_stitched.json")
        out_pre = compute_aging_from_stream(s, asof=date(2026, 4, 18))
        out_post = compute_aging_from_stream(s, asof=date(2026, 4, 20))
        # Pre-merge: Issue is in Review (WIP); PR is in Review (not in WIP - PR workflow not modeled here).
        # Post-merge: Issue is Done - excluded from WIP.
        pre_ids = {a.item_id for a in out_pre}
        post_ids = {a.item_id for a in out_post}
        assert "github:acme/widget:issue:101" in pre_ids
        assert "github:acme/widget:issue:101" not in post_ids

    def test_stitched_terminal_carries_pr_closes_issue_signal(self):
        """Sanity-check: the fixture really IS stitched (i.e. it
        models the spec, not just any old issue that happened to
        end in Done)."""
        s = load_stream_from_json(FIXTURES / "github_issue_pr_stitched.json")
        issue_txs = list(s.transitions_for("github:acme/widget:issue:101"))
        assert issue_txs[-1].signal == signals.SIGNAL_GITHUB_PR_CLOSES_ISSUE

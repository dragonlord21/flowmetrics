"""Behavioural spec for PR-lifecycle status_intervals derivation.

The PR_SEARCH_QUERY already returns timelineItems (ReadyForReviewEvent,
ConvertToDraftEvent, ReviewRequestedEvent, PullRequestReview,
MergedEvent, etc.). _pr_node_to_events was throwing this away,
producing PRs with empty `status_intervals` — which made the CFD
collapse to a degenerate Open→Merged 2-band chart for every GitHub
sample.

This module's `pr_lifecycle_intervals` walks the timeline and
produces a chronological `list[StatusInterval]` covering the
canonical PR workflow stages:

    Awaiting Review → (Changes Requested ↔ Approved)* → Merged

with a Draft segment when ConvertToDraftEvent appears.

The PR's createdAt is the start. If the timeline has a
ReadyForReviewEvent, the PR started as a draft (the event marks
the transition to Awaiting Review).
"""

from __future__ import annotations

from datetime import UTC, datetime

from flowmetrics.sources.github import pr_lifecycle_intervals


def _node(created: str, merged: str, timeline: list[dict]) -> dict:
    return {
        "createdAt": created,
        "mergedAt": merged,
        "timelineItems": {"nodes": timeline},
    }


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestPrLifecycleIntervals:
    def test_simple_pr_awaiting_review_then_merged(self):
        """No reviews, no drafts — PR sits in Awaiting Review from
        creation to merge."""
        node = _node(
            "2026-05-01T10:00:00Z", "2026-05-01T14:00:00Z",
            [{"__typename": "MergedEvent", "createdAt": "2026-05-01T14:00:00Z"}],
        )
        intervals = pr_lifecycle_intervals(node)
        stages = [iv.status for iv in intervals]
        assert stages == ["Awaiting Review", "Merged"]
        assert intervals[0].start == _ts(2026, 5, 1, 10, 0, 0)
        assert intervals[0].end == _ts(2026, 5, 1, 14, 0, 0)
        assert intervals[1].start == _ts(2026, 5, 1, 14, 0, 0)

    def test_started_as_draft_then_ready_for_review(self):
        """PR was created as a draft; ReadyForReviewEvent moves it
        to Awaiting Review."""
        node = _node(
            "2026-05-01T10:00:00Z", "2026-05-01T16:00:00Z",
            [
                {"__typename": "ReadyForReviewEvent", "createdAt": "2026-05-01T12:00:00Z"},
                {"__typename": "MergedEvent", "createdAt": "2026-05-01T16:00:00Z"},
            ],
        )
        intervals = pr_lifecycle_intervals(node)
        stages = [iv.status for iv in intervals]
        assert stages == ["Draft", "Awaiting Review", "Merged"]
        assert intervals[0].start == _ts(2026, 5, 1, 10, 0, 0)
        assert intervals[0].end == _ts(2026, 5, 1, 12, 0, 0)

    def test_review_changes_requested_then_approved(self):
        node = _node(
            "2026-05-01T10:00:00Z", "2026-05-02T10:00:00Z",
            [
                {"__typename": "PullRequestReview",
                 "submittedAt": "2026-05-01T15:00:00Z", "state": "CHANGES_REQUESTED"},
                {"__typename": "PullRequestReview",
                 "submittedAt": "2026-05-02T08:00:00Z", "state": "APPROVED"},
                {"__typename": "MergedEvent", "createdAt": "2026-05-02T10:00:00Z"},
            ],
        )
        intervals = pr_lifecycle_intervals(node)
        stages = [iv.status for iv in intervals]
        assert stages == [
            "Awaiting Review", "Changes Requested", "Approved", "Merged",
        ]

    def test_convert_to_draft_returns_pr_to_draft(self):
        node = _node(
            "2026-05-01T10:00:00Z", "2026-05-03T10:00:00Z",
            [
                {"__typename": "ConvertToDraftEvent", "createdAt": "2026-05-01T14:00:00Z"},
                {"__typename": "ReadyForReviewEvent", "createdAt": "2026-05-02T10:00:00Z"},
                {"__typename": "MergedEvent", "createdAt": "2026-05-03T10:00:00Z"},
            ],
        )
        intervals = pr_lifecycle_intervals(node)
        stages = [iv.status for iv in intervals]
        assert stages == ["Awaiting Review", "Draft", "Awaiting Review", "Merged"]

    def test_unmerged_pr_returns_no_terminal_merged_stage(self):
        """In-flight PR (no mergedAt) ends in its last reviewed stage."""
        node = _node(
            "2026-05-01T10:00:00Z", None,
            [
                {"__typename": "PullRequestReview",
                 "submittedAt": "2026-05-01T15:00:00Z", "state": "APPROVED"},
            ],
        )
        intervals = pr_lifecycle_intervals(node)
        stages = [iv.status for iv in intervals]
        # In-flight: no terminal Merged. Last interval is Approved
        # without a closing end-time (or uses the latest event ts).
        assert stages[-1] == "Approved"
        assert "Merged" not in stages

    def test_commented_review_does_not_change_stage(self):
        """A COMMENTED review state isn't a workflow transition."""
        node = _node(
            "2026-05-01T10:00:00Z", "2026-05-02T10:00:00Z",
            [
                {"__typename": "PullRequestReview",
                 "submittedAt": "2026-05-01T15:00:00Z", "state": "COMMENTED"},
                {"__typename": "MergedEvent", "createdAt": "2026-05-02T10:00:00Z"},
            ],
        )
        intervals = pr_lifecycle_intervals(node)
        stages = [iv.status for iv in intervals]
        assert stages == ["Awaiting Review", "Merged"]

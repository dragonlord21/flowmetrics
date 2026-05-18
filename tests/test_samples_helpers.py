"""Behavioural spec for the pure helpers in scripts/generate_samples.py.

The orchestration that calls the CLI and writes files is integration
territory — exercised manually. The pure parts (repo config, index
template, README rewrite) are testable.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# scripts/ isn't on the package path; add it for the test.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from generate_samples import (
    REPOS,
    Repo,
    SampleSet,
    build_index_html,
)

# ---------------------------------------------------------------------------
# Repo configuration
# ---------------------------------------------------------------------------


class TestRepoConfig:
    def test_calcmark_go_calcmark_included(self):
        slugs = [r.slug for r in REPOS]
        assert "CalcMark/go-calcmark" in slugs

    def test_each_repo_has_archetype_label(self):
        for r in REPOS:
            assert r.slug
            assert r.archetype
            assert "/" in r.slug, f"slug should be owner/name: {r.slug}"

    def test_at_most_eight_repos_to_respect_api_quota(self):
        # Keep the runtime cost bounded. Cap covers 5 GitHub + a couple of
        # Jira projects without blowing through anyone's API budget.
        assert len(REPOS) <= 8

    def test_includes_at_least_one_jira_source(self):
        """Demo set advertises Jira parity — must include >=1 Jira entry."""
        assert any(r.cache_subdir == "jira" for r in REPOS)
        assert any("--jira-url" in r.cli_args for r in REPOS)


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


def _sample_set(
    slug: str,
    *,
    with_cfd: bool = False,
    with_aging: bool = False,
    with_efficiency: bool = True,
    with_when_done: bool = True,
    with_how_many: bool = True,
    with_scatterplot: bool = True,
) -> SampleSet:
    d = slug.replace("/", "_")

    def _p(name: str, ext: str, present: bool) -> Path | None:
        return Path(f"samples/{d}/{name}.{ext}") if present else None

    return SampleSet(
        repo=Repo(slug=slug, archetype="test", cli_args=["--repo", slug]),
        efficiency_html=_p("efficiency", "html", with_efficiency),
        efficiency_json=_p("efficiency", "json", with_efficiency),
        efficiency_text=_p("efficiency", "txt", with_efficiency),
        when_done_html=_p("forecast-when-done", "html", with_when_done),
        when_done_json=_p("forecast-when-done", "json", with_when_done),
        when_done_text=_p("forecast-when-done", "txt", with_when_done),
        how_many_html=_p("forecast-how-many", "html", with_how_many),
        how_many_json=_p("forecast-how-many", "json", with_how_many),
        how_many_text=_p("forecast-how-many", "txt", with_how_many),
        scatterplot_html=_p("scatterplot", "html", with_scatterplot),
        scatterplot_json=_p("scatterplot", "json", with_scatterplot),
        scatterplot_text=_p("scatterplot", "txt", with_scatterplot),
        cfd_html=_p("cfd", "html", with_cfd),
        cfd_json=_p("cfd", "json", with_cfd),
        cfd_text=_p("cfd", "txt", with_cfd),
        aging_html=_p("aging", "html", with_aging),
        aging_json=_p("aging", "json", with_aging),
        aging_text=_p("aging", "txt", with_aging),
    )


class TestBuildIndexHtml:
    def test_is_complete_html_document(self):
        out = build_index_html(
            [_sample_set("astral-sh/uv")], datetime(2026, 5, 12, 14, 30, tzinfo=UTC)
        )
        assert "<!doctype html>" in out.lower()
        assert "</html>" in out

    def test_every_repo_appears(self):
        sets = [_sample_set("astral-sh/uv"), _sample_set("CalcMark/go-calcmark")]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        assert "astral-sh/uv" in out
        assert "CalcMark/go-calcmark" in out

    def test_links_to_every_format(self):
        sets = [_sample_set("astral-sh/uv")]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        assert "efficiency.html" in out
        assert "efficiency.json" in out
        assert "forecast-when-done.html" in out
        assert "forecast-how-many.html" in out

    def test_generated_at_rendered(self):
        out = build_index_html(
            [_sample_set("astral-sh/uv")], datetime(2026, 5, 12, 14, 30, 15, tzinfo=UTC)
        )
        assert "2026-05-12" in out

    def test_cfd_aging_columns_show_links_when_present(self):
        sets = [_sample_set("acme/jira", with_cfd=True, with_aging=True)]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        assert "cfd.html" in out
        assert "aging.html" in out

    def test_cfd_renders_na_when_absent(self):
        """GitHub repos skip CFD per DECISIONS.md #9 — should read 'n/a'."""
        sets = [_sample_set("github/repo", with_cfd=False, with_aging=True)]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        assert "n/a" in out
        # The Aging column should still link, the CFD column shouldn't
        assert "cfd.html" not in out
        assert "aging.html" in out

    def test_aging_renders_na_when_absent(self):
        sets = [_sample_set("repo/no-aging", with_cfd=False, with_aging=False)]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        assert out.count("n/a") >= 2  # both CFD and Aging cells

    def test_no_broken_links_when_a_report_failed_to_generate(self):
        """Real-world scenario: rust-lang/rust's efficiency + forecast
        commands time out (504), so those files never get written. The
        index must NOT emit links to files that don't exist on disk —
        it should render `n/a` for those cells, matching the existing
        CFD-absent behavior."""
        sets = [_sample_set(
            "rust-lang/rust",
            with_efficiency=False,
            with_when_done=False,
            with_how_many=False,
            with_aging=True,
            with_scatterplot=True,
        )]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        # No href should point to a known-missing file.
        assert 'href="rust-lang_rust/efficiency.html"' not in out
        assert 'href="rust-lang_rust/forecast-when-done.html"' not in out
        assert 'href="rust-lang_rust/forecast-how-many.html"' not in out
        # Aging + scatterplot DID generate — those links should be present.
        assert 'href="rust-lang_rust/aging.html"' in out
        assert 'href="rust-lang_rust/scatterplot.html"' in out

    def test_includes_decisions_pointer_for_na_explanation(self):
        """The reader needs to know why some cells are blank."""
        sets = [_sample_set("github/repo", with_cfd=False, with_aging=True)]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        assert "DECISIONS.md" in out


class TestReferenceSection:
    """Pages-published samples include a 'Reference' section linking back
    to the source markdown in the GitHub repo (README + docs/*.md).

    The site serves only `samples/`, so cross-doc reading happens on
    GitHub.com where markdown renders natively.
    """

    def test_links_to_readme_and_every_docs_markdown(self):
        out = build_index_html(
            [_sample_set("astral-sh/uv")],
            datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
        )
        # Every reference doc must appear as a github.com blob URL
        for doc in [
            "README.md",
            "docs/DECISIONS.md",
            "docs/METRICS.md",
            "docs/FORECAST.md",
            "docs/GLOSSARY.md",
        ]:
            assert f"github.com/dvhthomas/flowmetrics/blob/main/{doc}" in out, (
                f"missing reference link to {doc}"
            )

    def test_reference_section_has_heading(self):
        out = build_index_html(
            [_sample_set("astral-sh/uv")],
            datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
        )
        assert "Reference" in out


# ---------------------------------------------------------------------------
# Orchestration: --offline plumbing
# ---------------------------------------------------------------------------


class TestOfflineFlag:
    """When the samples script is invoked with --offline, every
    underlying `flow` invocation should carry --offline so cache
    misses surface as errors instead of silent live fetches.
    Lets you safely refresh samples after a spec/template change
    without burning API quota — and proves the cache covers the
    set."""

    def _captured_calls(self, monkeypatch, *, offline: bool) -> list[tuple[str, ...]]:
        import generate_samples
        calls: list[tuple[str, ...]] = []

        def fake_run(*args: str) -> str:
            calls.append(tuple(args))
            return ""

        monkeypatch.setattr(generate_samples, "_run_cli", fake_run)

        repo = generate_samples.Repo(
            slug="acme/widget",
            archetype="test",
            cli_args=["--repo", "acme/widget"],
            cache_subdir="github",
            cfd_workflow="Open,Merged",
            aging_workflow="Open,Review,Approved",
        )
        generate_samples._produce_one_repo(
            repo,
            history_end="2026-05-15",
            target_date="2026-05-30",
            offline=offline,
        )
        return calls

    def test_offline_true_plumbs_dashes_offline_into_every_flow_invocation(self, monkeypatch):
        calls = self._captured_calls(monkeypatch, offline=True)
        assert calls, "expected at least one flow invocation"
        missing = [c for c in calls if "--offline" not in c]
        assert not missing, (
            f"--offline should propagate to every flow invocation. "
            f"Missing in {len(missing)} of {len(calls)} calls."
        )

    def test_offline_false_omits_dashes_offline(self, monkeypatch):
        calls = self._captured_calls(monkeypatch, offline=False)
        assert calls
        leaked = [c for c in calls if "--offline" in c]
        assert not leaked, (
            f"--offline must NOT be added when offline=False. "
            f"Leaked into {len(leaked)} calls."
        )


class TestOfflineReusesWindowFromExistingSample:
    """When --offline is set and a previous sample exists for a repo,
    the script should reuse the previous window so cache hits.
    Otherwise day-to-day clock drift (today vs yesterday) causes
    100% cache misses after a single calendar day."""

    def test_offline_reuses_window_when_existing_efficiency_json_present(
        self, monkeypatch, tmp_path
    ):
        import json

        import generate_samples
        # Stand up a fake samples dir with an existing efficiency.json
        samples_dir = tmp_path / "samples"
        repo_dir = samples_dir / "acme_widget"
        repo_dir.mkdir(parents=True)
        previous_window = {
            "input": {
                "repo": "acme/widget",
                "start": "2026-05-01",
                "stop": "2026-05-07",
            }
        }
        (repo_dir / "efficiency.json").write_text(json.dumps(previous_window))
        monkeypatch.setattr(generate_samples, "SAMPLES_DIR", samples_dir)

        calls: list[tuple[str, ...]] = []
        def fake_run(*args: str) -> str:
            calls.append(tuple(args))
            return ""
        monkeypatch.setattr(generate_samples, "_run_cli", fake_run)

        repo = generate_samples.Repo(
            slug="acme/widget",
            archetype="test",
            cli_args=["--repo", "acme/widget"],
        )
        # Pass TODAY-derived window — script should ignore it and
        # use the recovered window for the efficiency call.
        generate_samples._produce_one_repo(
            repo,
            history_end="2026-05-15",
            target_date="2026-05-30",
            offline=True,
        )
        # The first efficiency call's --start should be the
        # recovered date, not 2026-05-09 (which today's history_end
        # would imply).
        efficiency_calls = [c for c in calls if c[0] == "efficiency"]
        assert efficiency_calls
        first = efficiency_calls[0]
        assert "--start" in first
        start_idx = first.index("--start")
        assert first[start_idx + 1] == "2026-05-01", (
            f"--offline should recover prior window, got start={first[start_idx + 1]}"
        )

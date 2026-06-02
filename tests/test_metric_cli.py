"""`flow metric ...` — text+JSON metric extraction for agents.

Every metric subcommand takes EITHER `--workflow-name NAME` (DB/YAML
store lookup via `--workflows-dir`) OR `--workflow-yaml PATH` (direct
file). Source + stages live in the workflow definition; the CLI
doesn't accept inline `--repo` / `--workflow` / `--wip-labels`.

Subcommands:
  throughput  — daily completion counts in a window
  cumulative  — cumulative flow diagram (state counts over time)
  aging       — in-flight items × current state × age
  cycle-time  — completed-item cycle times + P50/P85/P95

Output: text (default, one-line headline) or `--format json`
(versioned envelope). NO HTML, NO charts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli

FIXTURE_CACHE = str(Path(__file__).parent / "fixtures" / "cache")
# The pinned cache covers astral-sh/uv for early-May 2026.
_REPO = "astral-sh/uv"
_START = "2026-05-04"
_STOP = "2026-05-10"


def _write_workflow_yaml(workflows_dir: Path, name: str, **fields) -> Path:
    """Seed a workflow YAML into the dir. WorkflowStore picks it up
    via the YAML-fallback path; no wizard needed."""
    workflows_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name,
        "source": "github",
        "repo": _REPO,
        "start": _START,
        "stop": _STOP,
        "steps": [
            {"name": "Draft", "wip": True},
            {"name": "Awaiting Review", "wip": True},
            {"name": "Changes Requested", "wip": True},
            {"name": "Approved", "wip": True},
        ],
    }
    payload.update(fields)
    path = workflows_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump({"workflow": payload}))
    return path


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


@pytest.fixture
def workflows_dir(tmp_path):
    wf = tmp_path / "contracts"
    _write_workflow_yaml(wf, "astral-uv-week")
    return wf


class TestMetricGroup:
    def test_metric_group_lists_four_subcommands(self):
        result = _invoke("metric", "--help")
        assert result.exit_code == 0, result.output
        for cmd in ("throughput", "cumulative", "aging", "cycle-time"):
            assert cmd in result.output, f"missing subcommand: {cmd}"


class TestThroughput:
    def test_text_headline_names_repo_and_total(self, workflows_dir):
        result = _invoke(
            "metric", "throughput",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(workflows_dir),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        assert _REPO in result.output
        assert "items completed" in result.output.lower()

    def test_json_envelope_carries_per_day_samples(self, workflows_dir):
        result = _invoke(
            "metric", "throughput",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(workflows_dir),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
            "--format", "json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.metric.throughput.v1"
        samples = payload["daily_samples"]
        assert isinstance(samples, list)
        assert len(samples) == 7
        assert payload["summary"]["total_items"] == sum(samples)
        # Input echoes the workflow name + source so the consumer can
        # match the output back to its driving config.
        assert payload["input"]["workflow"] == "astral-uv-week"
        assert payload["input"]["repo"] == _REPO


class TestCumulative:
    def test_text_headline_names_workflow_and_end_wip(self, tmp_path):
        wf = tmp_path / "contracts"
        # CFD uses the workflow's stages; simpler 2-stage layout for
        # the test fixture.
        _write_workflow_yaml(wf, "astral-uv-week", steps=[
            {"name": "Open", "wip": True},
            {"name": "Merged", "wip": False},
        ])
        result = _invoke(
            "metric", "cumulative",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(wf),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        assert _REPO in result.output

    def test_json_envelope_carries_per_sample_state_counts(self, tmp_path):
        wf = tmp_path / "contracts"
        _write_workflow_yaml(wf, "astral-uv-week", steps=[
            {"name": "Open", "wip": True},
            {"name": "Merged", "wip": False},
        ])
        result = _invoke(
            "metric", "cumulative",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(wf),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
            "--format", "json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.metric.cumulative.v1"
        points = payload["points"]
        assert points
        for pt in points:
            assert "sampled_on" in pt
            assert "counts_by_state" in pt


class TestAging:
    _ASOF = "2026-05-10"

    def test_text_headline_names_in_flight_count(self, workflows_dir):
        result = _invoke(
            "metric", "aging",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(workflows_dir),
            "--asof", self._ASOF,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "in-flight" in out or "in flight" in out or "items" in out

    def test_json_envelope_lists_in_flight_items(self, workflows_dir):
        result = _invoke(
            "metric", "aging",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(workflows_dir),
            "--asof", self._ASOF,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
            "--format", "json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.metric.aging.v1"
        items = payload["items"]
        assert isinstance(items, list)
        if items:
            for it in items:
                assert "item_id" in it
                assert "current_state" in it
                assert "age_days" in it
        assert "cycle_time_percentiles_days" in payload


class TestCycleTime:
    def test_text_headline_names_percentiles(self, workflows_dir):
        result = _invoke(
            "metric", "cycle-time",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(workflows_dir),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        assert "P85" in result.output or "p85" in result.output.lower()

    def test_json_envelope_lists_per_item_cycle_times(self, workflows_dir):
        result = _invoke(
            "metric", "cycle-time",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(workflows_dir),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
            "--format", "json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.metric.cycle_time.v1"
        items = payload["items"]
        assert items, "expected at least one completed item"
        for it in items:
            assert "item_id" in it
            assert "completed_at" in it
            assert "cycle_time_days" in it
        percentiles = payload["percentiles_days"]
        for p in (50, 70, 85, 95):
            assert str(p) in percentiles or p in percentiles


class TestWorkflowYamlPath:
    """`--workflow-yaml PATH` lets you query a workflow that isn't
    in the store (e.g. a file someone hands you for a one-off
    analysis without writing to your DB)."""

    def test_yaml_path_runs_without_workflows_dir(self, tmp_path):
        # Write the YAML somewhere other than a configured workflows-dir.
        yaml_path = tmp_path / "demo.yaml"
        yaml_path.write_text(yaml.safe_dump({
            "workflow": {
                "name": "demo",
                "source": "github",
                "repo": _REPO,
            }
        }))
        result = _invoke(
            "metric", "throughput",
            "--workflow-yaml", str(yaml_path),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        assert _REPO in result.output


class TestRequiredInput:
    def test_neither_workflow_name_nor_yaml_errors(self, tmp_path):
        result = _invoke(
            "metric", "throughput",
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code != 0
        msg = result.output.lower()
        assert "workflow-name" in msg or "workflow-yaml" in msg

    def test_both_workflow_name_and_yaml_errors(self, tmp_path, workflows_dir):
        yaml_path = tmp_path / "demo.yaml"
        yaml_path.write_text(yaml.safe_dump({
            "workflow": {
                "name": "demo", "source": "github", "repo": _REPO,
            }
        }))
        result = _invoke(
            "metric", "throughput",
            "--workflow-name", "astral-uv-week",
            "--workflows-dir", str(workflows_dir),
            "--workflow-yaml", str(yaml_path),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower()

    def test_unknown_workflow_name_errors_with_clear_message(self, tmp_path):
        wf = tmp_path / "contracts"
        wf.mkdir()
        result = _invoke(
            "metric", "throughput",
            "--workflow-name", "no-such-workflow",
            "--workflows-dir", str(wf),
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code != 0
        assert "no-such-workflow" in result.output

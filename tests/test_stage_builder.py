"""Stage builder — the "Stages" fieldset of the new-contract wizard.

The user clicks "Discover stages", which POSTs to
`/api/internal/workflows/_probe-stages`. The server runs a bounded
materialize into a scratch dir, extracts distinct stage names from
the transitions table, deletes the scratch dir, and returns
{stages: [...]}. The result is cached for 15 minutes per source
target so the user can iterate without re-paying the API call.

Tests inject a fake probe so the suite never touches the network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flowmetrics.app import create_app


@pytest.fixture
def workspace(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    data = tmp_path / "data"
    return contracts, data


def _post(client, payload, mock_probe=None):
    headers = {"X-Requested-With": "fetch"}
    if mock_probe is not None:
        client.app.state.probe_stages = mock_probe
    return client.post(
        "/api/internal/workflows/_probe-stages",
        json=payload, headers=headers,
    )


class TestProbeStages:
    def test_github_returns_review_cycle_stages(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = _post(
                client,
                {"source": "github", "repo": "astral-sh/uv"},
                mock_probe=lambda kind, target: {
                    "stages": [
                        "Draft", "Awaiting Review", "Changes Requested",
                        "Approved", "Merged",
                    ],
                },
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Draft" in body["stages"]
        assert "Merged" in body["stages"]

    def test_no_stages_returns_actionable_hint(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = _post(
                client,
                {"source": "github", "repo": "owner/empty-repo"},
                mock_probe=lambda kind, target: {
                    "stages": [],
                    "hint": "no PRs in the last 30 days; widen the window",
                },
            )
        body = r.json()
        assert body["stages"] == []
        assert "30 days" in body["hint"]

    def test_unknown_source_returns_422(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = _post(client, {"source": "carrier-pigeon"})
        assert r.status_code == 422


class TestProbeStagesCache:
    """Same source target probed twice within 15 minutes shouldn't
    re-run the bounded materialize. The cache key is the source
    target tuple (kind + repo OR kind + jira_url + jira_project)."""

    def test_repeat_call_hits_cache_not_probe(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)

        calls: list[dict] = []

        def probe(kind, target):
            calls.append({"kind": kind, "target": dict(target)})
            return {"stages": ["A", "B"]}

        with TestClient(app) as client:
            payload = {"source": "github", "repo": "owner/x"}
            r1 = _post(client, payload, mock_probe=probe)
            r2 = _post(client, payload)
        assert r1.json() == r2.json()
        # Second call hit the cache → probe ran ONCE.
        assert len(calls) == 1

    def test_different_target_misses_cache(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        calls: list[dict] = []
        def probe(kind, target):
            calls.append({"kind": kind, "target": dict(target)})
            return {"stages": [(target.get("repo") or "x") + "-stage"]}
        with TestClient(app) as client:
            _post(client, {"source": "github", "repo": "a/b"}, mock_probe=probe)
            _post(client, {"source": "github", "repo": "c/d"})
        assert len(calls) == 2

    def test_force_flag_busts_cache(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        calls = []
        def probe(kind, target):
            calls.append(1)
            return {"stages": ["X"]}
        with TestClient(app) as client:
            payload = {"source": "github", "repo": "owner/x"}
            _post(client, payload, mock_probe=probe)
            # ?force=true skips the cache check.
            r = client.post(
                "/api/internal/workflows/_probe-stages?force=true",
                json=payload, headers={"X-Requested-With": "fetch"},
            )
            assert r.status_code == 200
        assert len(calls) == 2


# TestWizardStagesUI removed in C3 — the three-bucket Stages UI is
# gone. test_steps_editor.py asserts the new Steps editor DOM.

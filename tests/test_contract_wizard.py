"""New-workflow wizard — UI + probe-source endpoint.

The wizard lives at `/admin/workflows/new`. A single form: name +
label, source picker, and the steps editor (there is no date-window
field — data is fetched via the Data Source page's backfill after
save). The source field's on-blur fires `POST
/api/internal/workflows/_probe-source` to confirm the repo / project
actually exists at the source.

These tests cover the page render + the probe endpoint. The full
end-to-end save flow (form → validate → PUT → "fetch data" panel) is
exercised in the builder Playwright E2E (separate file).
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


class TestWizardPage:
    def test_renders_at_admin_contracts_new(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/admin/workflows/new")
        assert r.status_code == 200
        html = r.text
        # Three required fields: name, source, repo/jira.
        assert 'name="name"' in html
        assert 'name="source"' in html
        # Both source-specific input groups present (one will be hidden).
        assert 'name="repo"' in html
        assert 'name="jira_url"' in html
        assert 'name="jira_project"' in html
        assert 'name="allowed_issuetypes"' in html

    def test_links_to_wizard_from_home(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            home = client.get("/").text
        # "+ New workflow" affordance lands users in the wizard.
        assert "/admin/workflows/new" in home


class TestProbeSourceEndpoint:
    """`POST /api/internal/workflows/_probe-source` confirms the
    target (GitHub repo or Jira project) actually exists. The
    request body is `{source, repo?, jira_url?, jira_project?}`;
    the response is `{ok: bool, label?: str, error?: str}`.

    Tests inject a fake HTTP probe to avoid real network calls."""

    def _post(self, client, body, mock_probe=None):
        headers = {"X-Requested-With": "fetch"}
        # The endpoint reads the probe callable from app.state when
        # set; production wiring uses httpx.
        if mock_probe is not None:
            client.app.state.probe_source = mock_probe
        return client.post(
            "/api/internal/workflows/_probe-source",
            json=body, headers=headers,
        )

    def test_github_repo_exists_returns_ok(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            # Probe sees `source=github, repo=astral-sh/uv` and says yes.
            r = self._post(
                client,
                {"source": "github", "repo": "astral-sh/uv"},
                mock_probe=lambda kind, target: {"ok": True, "label": "Astral uv"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["label"] == "Astral uv"

    def test_github_repo_missing_returns_not_ok_with_error(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = self._post(
                client,
                {"source": "github", "repo": "does-not/exist"},
                mock_probe=lambda kind, target: {
                    "ok": False, "error": "repo not found (404)",
                },
            )
        body = r.json()
        assert body["ok"] is False
        assert "not found" in body["error"]

    def test_jira_project_dispatches_with_url_and_project(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        seen: dict = {}

        def probe(kind, target):
            seen["kind"] = kind
            seen["target"] = target
            return {"ok": True, "label": "BIGTOP"}

        with TestClient(app) as client:
            r = self._post(
                client,
                {
                    "source": "jira",
                    "jira_url": "https://issues.apache.org/jira",
                    "jira_project": "BIGTOP",
                },
                mock_probe=probe,
            )
        assert r.status_code == 200
        assert seen["kind"] == "jira"
        # Target shape is a dict carrying both fields.
        assert seen["target"]["jira_project"] == "BIGTOP"
        assert seen["target"]["jira_url"].startswith("https://")

    def test_missing_source_returns_400(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = self._post(client, {"repo": "a/b"})
        assert r.status_code == 422

# tests/test_app_routing_filters.py
from __future__ import annotations

import pytest
import duckdb
from pathlib import Path
from unittest.mock import MagicMock
from fastapi import Request
from flowmetrics.app import _keep_filters, WorkflowView
from flowmetrics.workflow import Workflow

class MockTemplateContext:
    def __init__(self, request):
        self.context = {"request": request}

    def get(self, key, default=None):
        return self.context.get(key, default)

def test_keep_filters_appends_issuetype_query_param():
    mock_request = MagicMock(spec=Request)
    mock_request.url.query = "period=last-30-days&issuetype=Story&issuetype=Bug"
    
    ctx = MockTemplateContext(mock_request)
    
    # We pass pass_context wrapper function the context manually
    res = _keep_filters(ctx, "/workflows/my_wf")
    assert "/workflows/my_wf" in res
    assert "period=last-30-days" in res
    assert "issuetype=Story" in res
    assert "issuetype=Bug" in res


def test_workflow_view_parses_issuetype_list():
    # Mock WorkflowView constructor inputs
    mock_db = MagicMock()
    mock_meta = MagicMock()
    mock_meta.archived_at = None
    mock_meta.workflow = Workflow(name="my_wf", source="jira", jira_url="http://jira", jira_project="TEST")
    mock_db.get_meta.return_value = mock_meta
    
    mock_request = MagicMock(spec=Request)
    # MultiDict query params mock
    mock_request.query_params.getlist.return_value = ["Story", "Bug"]
    
    # Mock open_warehouse and completion_date_range / latest_materialized_at inside __init__
    import flowmetrics.app
    original_open = flowmetrics.app.open_warehouse
    original_range = flowmetrics.app.completion_date_range
    
    flowmetrics.app.open_warehouse = MagicMock()
    flowmetrics.app.completion_date_range = MagicMock(return_value=(None, None))
    
    try:
        view = WorkflowView(
            workflow_id="my_wf",
            contracts_dir=Path("/tmp"),
            data_dir=Path("/tmp"),
            query={},
            contracts_db=mock_db,
            request=mock_request
        )
        
        assert view.selected_issuetypes == ["Story", "Bug"]
    finally:
        flowmetrics.app.open_warehouse = original_open
        flowmetrics.app.completion_date_range = original_range

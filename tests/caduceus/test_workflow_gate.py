"""Regression tests for the Workflow tool's Caduceus opt-in gate.

With Caduceus disabled the Loom must refuse to run, even though the tool is
present in the schema — so a stock session can never spawn an orchestration.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import tools.workflow_tool as wt


def _agent(enabled):
    return SimpleNamespace(
        caduceus=SimpleNamespace(enabled=enabled),
        tool_progress_callback=None,
    )


def test_refuses_without_parent_agent():
    out = json.loads(wt.workflow_tool(script="meta={}", parent_agent=None))
    assert out["success"] is False


def test_refuses_when_caduceus_disabled():
    out = json.loads(wt.workflow_tool(script="meta={}", parent_agent=_agent(False)))
    assert out["success"] is False
    assert "Caduceus" in out["error"]


def test_runs_when_caduceus_enabled(monkeypatch):
    captured = {}

    def _fake_run_workflow(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(ok=True, run_id="wf_abc123", result={"x": 1},
                               stats={}, meta={"name": "t"}, script_path=None, error=None)

    monkeypatch.setattr("agent.workflow.engine.run_workflow", _fake_run_workflow)

    out = json.loads(wt.workflow_tool(script="meta={}", parent_agent=_agent(True)))

    assert out["success"] is True          # gate passed, engine ran
    assert out["runId"] == "wf_abc123"
    assert captured  # run_workflow was actually invoked

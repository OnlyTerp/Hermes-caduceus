"""Tests for Caduceus /local mode — local GPU-served workflow workers.

Covers the three layers, all fully offline (hooks + health are injected):

* LocalModelManager — manifest parse, profile selection, the load/unload/swap
  state machine (exclusivity, idempotency, profile switch), worker routing, and
  health-timeout failure;
* LocalGate — model+profile affinity: same-model leaves share slots, a different
  model drains-then-swaps, and only one model is ever resident;
* worker creds injection — run_workflow_leaf honours an explicit creds_override
  (the local endpoint) and bypasses tier/router resolution.
"""
from __future__ import annotations

import asyncio

import pytest

from agent import local_manager as lm
from agent.workflow.local_gate import LocalGate


def _manifest():
    return {
        "default_worker": "qwen",
        "models": [
            {
                "id": "qwen", "endpoint": "http://h:8004/v1", "served_model_name": "qwen-local",
                "group": "gpu", "max_context": 262144, "card": "general",
                "load": "qwen_load", "unload": "qwen_unload", "health": "http://h:8004/health",
                "profiles": [
                    {"slots": 4, "ctx": 32768, "picker": "4x32k", "default": True},
                    {"slots": 2, "ctx": 131072, "picker": "2x131k"},
                    {"slots": 1, "ctx": 262144, "picker": "256k"},
                ],
            },
            {
                "id": "gemma", "endpoint": "http://h:8083/v1", "served_model_name": "gemma-local",
                "group": "gpu", "max_context": 262144, "card": "reasoning",
                "load": "gemma_load", "unload": "gemma_unload", "health": "http://h:8083/health",
                "profiles": [{"slots": 1, "ctx": 262144, "picker": "g262", "default": True}],
            },
        ],
    }


def _mgr(calls=None):
    calls = calls if calls is not None else []

    def hook(cmd, env, timeout):
        calls.append((cmd, env.get("LOCAL_MODEL_ID"), env.get("LOCAL_PROFILE_PICKER")))
        return 0, "ok"

    m = lm.LocalModelManager(_manifest(), run_hook=hook, health_ok=lambda u, t=4.0: True,
                             sleep=lambda s: None)
    return m, calls


# ── manifest + profile selection ───────────────────────────────────────


def test_parse_and_capacity():
    m, _ = _mgr()
    assert m.has_models()
    assert {x.id for x in m.models()} == {"qwen", "gemma"}
    assert m.default_worker_id == "qwen"
    assert m.capacity("qwen") == 4 and m.capacity("gemma") == 1
    assert m.max_capacity() == 4


def test_select_profile_by_context():
    m, _ = _mgr()
    q = m.model("qwen")
    assert q.select_profile().slots == 4                     # widest by default
    assert q.select_profile(want_ctx=120000).picker == "2x131k"
    assert q.select_profile(want_ctx=200000).picker == "256k"
    assert q.select_profile(want_ctx=10**9).picker == "256k"  # best-effort, never empty


def test_resolve_worker_id_semantics():
    m, _ = _mgr()
    assert m.resolve_worker_id("local:gemma") == "gemma"
    assert m.resolve_worker_id("qwen") == "qwen"
    assert m.resolve_worker_id(None) == "qwen"                # untagged -> default
    assert m.resolve_worker_id("unknown", "local") == "qwen"  # local intent -> default
    assert m.resolve_worker_id("gpt-5.5", "openrouter") is None   # explicit cloud -> escape
    assert m.resolve_worker_id("anything-cloud") is None


# ── load / unload / swap state machine ─────────────────────────────────


def test_ensure_idempotent_and_creds():
    m, calls = _mgr()
    c = m.ensure("qwen")
    assert c["base_url"] == "http://h:8004/v1" and c["model"] == "qwen-local"
    assert m.loaded() == ("qwen", "4x32k") and m.current_slots() == 4
    n = len(calls)
    m.ensure("qwen")  # same profile -> no new hooks
    assert len(calls) == n


def test_profile_switch_reloads_same_model():
    m, calls = _mgr()
    m.ensure("qwen")
    calls.clear()
    m.ensure("qwen", want_ctx=200000)
    assert m.loaded() == ("qwen", "256k") and m.current_slots() == 1
    assert calls == [("qwen_unload", "qwen", None), ("qwen_load", "qwen", "256k")]


def test_exclusivity_swap_unloads_conflict():
    m, calls = _mgr()
    m.ensure("qwen")
    calls.clear()
    m.ensure("gemma")
    assert m.loaded() == ("gemma", "g262")
    assert calls == [("qwen_unload", "qwen", None), ("gemma_load", "gemma", "g262")]


def test_unload_all():
    m, calls = _mgr()
    m.ensure("qwen")
    calls.clear()
    m.unload_all()
    assert m.loaded() is None
    assert calls == [("qwen_unload", "qwen", None)]


def test_health_timeout_raises():
    def hook(cmd, env, timeout):
        return 0, "ok"
    m = lm.LocalModelManager(_manifest(), run_hook=hook,
                             health_ok=lambda u, t=4.0: False, sleep=lambda s: None)
    m.load_timeout = 0.01
    with pytest.raises(lm.LocalModelError):
        m.ensure("qwen")
    assert m.loaded() is None  # failed load leaves no phantom state


def test_load_hook_failure_raises():
    def hook(cmd, env, timeout):
        return 1, "boom"
    m = lm.LocalModelManager(_manifest(), run_hook=hook,
                             health_ok=lambda u, t=4.0: True, sleep=lambda s: None)
    with pytest.raises(lm.LocalModelError):
        m.ensure("qwen")


# ── LocalGate: model+profile affinity ──────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def test_gate_same_model_shares_slots_no_reload():
    m, calls = _mgr()
    gate = LocalGate(m)

    async def main():
        log = []

        async def leaf(name):
            await gate.acquire({"model": "local:qwen"})
            log.append(m.loaded())
            await asyncio.sleep(0.02)
            await gate.release()

        await asyncio.gather(leaf("a"), leaf("b"), leaf("c"))
        return log

    log = _run(main())
    assert all(x == ("qwen", "4x32k") for x in log)
    # Exactly one load hook for qwen (shared), no unloads.
    assert calls == [("qwen_load", "qwen", "4x32k")]


def test_gate_different_model_drains_then_swaps_once():
    m, calls = _mgr()
    gate = LocalGate(m)

    async def main():
        async def leaf(model, hold):
            await gate.acquire({"model": model})
            await asyncio.sleep(hold)
            await gate.release()

        await leaf("local:qwen", 0.01)
        calls.clear()
        await leaf("local:gemma", 0.01)

    _run(main())
    assert m.loaded() == ("gemma", "g262")
    assert calls == [("qwen_unload", "qwen", None), ("gemma_load", "gemma", "g262")]


def test_gate_caps_concurrency_to_slots():
    m, _ = _mgr()
    gate = LocalGate(m)

    async def main():
        peak = {"n": 0, "cur": 0}

        async def leaf():
            await gate.acquire({"model": "local:qwen"})
            peak["cur"] += 1
            peak["n"] = max(peak["n"], peak["cur"])
            await asyncio.sleep(0.02)
            peak["cur"] -= 1
            await gate.release()

        await asyncio.gather(*[leaf() for _ in range(10)])
        return peak["n"]

    peak = _run(main())
    assert peak <= 4  # qwen default profile has 4 slots


def test_gate_cloud_leaf_returns_none():
    m, _ = _mgr()
    gate = LocalGate(m)

    async def main():
        return await gate.acquire({"model": "gpt-5.5", "provider": "openrouter"})

    assert _run(main()) is None  # explicit cloud model escapes the gate


# ── caduceus state wiring ──────────────────────────────────────────────


def test_local_manager_for_state(monkeypatch):
    from agent import caduceus as cad
    lm.reset_local_manager()
    cfg = {"caduceus": {"enabled": True, "local": dict(_manifest(), enabled=True)}}
    st = cad.state_from_config(cfg)
    mgr = cad.local_manager_for(st)
    assert mgr is not None and mgr.has_models()
    # Off when caduceus off, or local disabled, or no models.
    assert cad.local_manager_for(cad.state_from_config(
        {"caduceus": {"enabled": False, "local": dict(_manifest(), enabled=True)}})) is None
    lm.reset_local_manager()
    assert cad.local_manager_for(cad.state_from_config(
        {"caduceus": {"enabled": True, "local": {"enabled": False, "models": []}}})) is None
    lm.reset_local_manager()


def test_local_workflow_hint(monkeypatch):
    from agent import caduceus as cad
    lm.reset_local_manager()
    st = cad.state_from_config({"caduceus": {"enabled": True,
                                             "local": dict(_manifest(), enabled=True)}})
    hint = cad.local_workflow_hint(st)
    assert hint and "local:qwen" in hint and "local:gemma" in hint
    assert "orchestrator" in hint.lower()
    lm.reset_local_manager()


# ── worker creds injection into the leaf ───────────────────────────────


def test_run_workflow_leaf_honors_creds_override(monkeypatch):
    from tools import delegate_tool as dt

    captured = {}

    class _FakeChild:
        def __init__(self):
            self.thinking_callback = None
            self.stream_delta_callback = None
            self.tool_progress_callback = None
            self._delegate_saved_tool_names = []
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.model = "qwen-local"

    def fake_build_child_agent(**kwargs):
        captured.update(kwargs)
        return _FakeChild()

    def fake_run_single_child(task_index, goal, child=None, parent_agent=None, **kw):
        return {"summary": "ok", "status": "done", "model": child.model, "api_calls": 1,
                "duration_seconds": 0.1}

    # Sentinel: _resolve_leaf_creds must NOT be called when creds_override is set.
    def boom(*a, **k):
        raise AssertionError("_resolve_leaf_creds called despite creds_override")

    monkeypatch.setattr(dt, "_build_child_agent", fake_build_child_agent)
    monkeypatch.setattr(dt, "_run_single_child", fake_run_single_child)
    monkeypatch.setattr(dt, "_resolve_leaf_creds", boom)

    creds = {"base_url": "http://h:8004/v1", "api_key": "local",
             "model": "qwen-local", "provider": "custom", "api_mode": "chat_completions"}
    res = dt.run_workflow_leaf(object(), "do it", creds_override=creds)

    assert res["status"] == "done"
    assert captured["override_base_url"] == "http://h:8004/v1"
    assert captured["override_api_key"] == "local"
    assert captured["model"] == "qwen-local"
    assert captured["override_provider"] == "custom"
    assert captured["override_api_mode"] == "chat_completions"

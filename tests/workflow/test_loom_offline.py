"""Offline self-tests for the Caduceus Loom engine (no network).

Mocks ``tools.delegate_tool.run_workflow_leaf`` so the scheduler, sandbox,
structured-output, budget, resume, and quality-pattern logic are exercised
deterministically without any model call.
"""
import json
from types import SimpleNamespace

import pytest

import agent.caduceus as cad
import tools.delegate_tool as dt
from agent.workflow import engine
from agent.workflow.sandbox import SandboxError, validate


def _make_parent(budget=None):
    st = cad.CaduceusState(enabled=True, budget_tokens=budget)
    st.worker = {"provider": "", "model": "fake-worker"}
    return SimpleNamespace(
        session_id="test-sess", caduceus=st,
        tool_progress_callback=None, model="fake-orch", provider="fake",
    )


def _fake_leaf(parent_agent, prompt, **kw):
    if "JSON Schema" in prompt:
        if "isReal" in prompt:
            out = json.dumps({"isReal": True, "title": "x"})
        elif "refuted" in prompt:
            out = json.dumps({"refuted": False})
        elif "score" in prompt:
            out = json.dumps({"score": 7.5})
        elif "findings" in prompt:
            out = json.dumps({"findings": [{"title": "A"}, {"title": "B"}]})
        else:
            out = json.dumps({"ok": True})
    else:
        out = f"answer:{prompt[:20]}"
    cbs = kw.get("callbacks") or {}
    if cbs.get("on_text"):
        cbs["on_text"](out)
    return {"text": out, "status": "completed", "input_tokens": 10,
            "output_tokens": 25, "model": "fake-worker", "api_calls": 1, "duration_seconds": 0.0}


@pytest.fixture(autouse=True)
def _mock_leaf(monkeypatch):
    monkeypatch.setattr(dt, "run_workflow_leaf", _fake_leaf)


def _run(script, args=None, resume=None, cfg=None, events=None):
    def emit(et, pl):
        if events is not None:
            events.append((et, pl))
    return engine.run_workflow(parent_agent=_make_parent((cfg or {}).get("_budget")),
                               emit=emit, script=script, args=args,
                               resume_from=resume, config=cfg or {})


PIPE = '''
meta = {"name": "review", "description": "review + verify", "phases": [{"title":"Review"},{"title":"Verify"}]}
FINDINGS = {"type":"object","properties":{"findings":{"type":"array"}},"required":["findings"]}
VERDICT = {"type":"object","properties":{"isReal":{"type":"boolean"}},"required":["isReal"]}
DIMS = [{"key":"bugs","prompt":"find bugs, return findings"},{"key":"perf","prompt":"find perf, return findings"}]
async def main():
    async def review(d):
        return await agent(d["prompt"], label="review:"+d["key"], phase="Review", schema=FINDINGS)
    async def verify(rev, d, i):
        async def vone(f):
            v = await agent("verify isReal: "+f["title"], phase="Verify", schema=VERDICT)
            return {**f, "verdict": v}
        return await parallel([(lambda f=f: vone(f)) for f in rev["findings"]])
    results = await pipeline(DIMS, review, verify)
    confirmed = [f for r in results if r for f in r if f and f["verdict"]["isReal"]]
    return {"confirmed": len(confirmed)}
'''


def test_pipeline_no_barrier_confirms_all():
    events = []
    res = _run(PIPE, events=events)
    assert res.ok, res.error
    assert res.result["confirmed"] == 4
    et = [e[0] for e in events]
    assert "workflow.start" in et and "workflow.complete" in et
    assert et.count("workflow.agent.spawn") == 6  # 2 reviews + 4 verifies


def test_parallel_barrier_maps_throw_to_none():
    script = '''
meta = {"name":"par","description":"barrier"}
async def main():
    def boom():
        raise ValueError("nope")
    res = await parallel([(lambda: agent("a")), (lambda: agent("b")), boom])
    return {"count": len(res), "nones": sum(1 for x in res if x is None)}
'''
    res = _run(script)
    assert res.ok and res.result == {"count": 3, "nones": 1}


def test_adversarial_verify_pattern():
    script = '''
meta = {"name":"qv","description":"verify"}
async def main():
    v = await adversarial_verify("claim", n=3)
    return {"survives": v["survives"], "votes": len(v["votes"])}
'''
    events = []
    res = _run(script, events=events)
    assert res.ok and res.result["survives"] is True and res.result["votes"] == 3
    assert any(e[0] == "workflow.verify" for e in events)


def test_budget_hard_ceiling_stops_loop():
    script = '''
meta = {"name":"bud","description":"loop"}
async def main():
    n = 0
    while budget.total and budget.remaining() > 0:
        await agent("more")
        n += 1
        if n > 50: break
    return {"n": n}
'''
    res = _run(script, cfg={"_budget": 40, "default_budget_tokens": 40})
    assert res.ok
    assert 1 <= res.result["n"] <= 3  # ~25 out tokens/leaf vs 40 ceiling


def test_max_agents_backstop():
    script = '''
meta = {"name":"runaway","description":"loop"}
async def main():
    n = 0
    while True:
        await agent("x")
        n += 1
    return n
'''
    res = _run(script, cfg={"max_agents": 5})
    # The runaway loop hits the backstop -> RuntimeError -> ok=False.
    assert not res.ok and "backstop" in (res.error or "")


@pytest.mark.parametrize("bad,why", [
    ("import os", "imports"),
    ("x = eval('1')", "eval"),
    ("y = ().__class__", "dunder"),
    ("z = open('/etc/passwd')", "open"),
])
def test_sandbox_blocks(bad, why):
    with pytest.raises(SandboxError):
        validate(f"meta={{}}\nasync def main():\n    {bad}\n    return 1\n")


def test_structured_output_retries_then_validates():
    calls = {"n": 0}

    def flaky_leaf(parent_agent, prompt, **kw):
        calls["n"] += 1
        # First attempt returns invalid JSON, second returns valid.
        if calls["n"] == 1:
            out = "not json at all"
        else:
            out = json.dumps({"isReal": True})
        return {"text": out, "status": "completed", "input_tokens": 5, "output_tokens": 5}

    dt.run_workflow_leaf = flaky_leaf
    try:
        script = '''
meta = {"name":"s","description":"schema"}
S = {"type":"object","properties":{"isReal":{"type":"boolean"}},"required":["isReal"]}
async def main():
    return await agent("give isReal", schema=S)
'''
        res = _run(script, cfg={"schema_max_retries": 2})
        assert res.ok and res.result == {"isReal": True}
        assert calls["n"] == 2  # retried once
    finally:
        dt.run_workflow_leaf = _fake_leaf


def test_sandbox_tolerates_js_flavored_script():
    """Fenced + const/export (the common LLM slip) normalize and run."""
    from agent.workflow.sandbox import normalize_script
    js = (
        "```python\n"
        'export const meta = {"name": "t", "description": "d"}\n'
        "const ITEMS = [1, 2, 3]\n"
        "async def main():\n"
        '    return {"n": len(ITEMS)}\n'
        "```"
    )
    norm = normalize_script(js)
    assert norm.strip().startswith("meta =")
    assert "const " not in norm and "```" not in norm
    res = _run(js)
    assert res.ok and res.result == {"n": 3}


def test_sandbox_arrow_function_gives_python_hint():
    from agent.workflow.sandbox import SandboxError, validate
    with pytest.raises(SandboxError) as exc:
        validate("meta={'name':'x','description':'y'}\nasync def main():\n    f = (x => x)\n")
    assert "PYTHON, not JavaScript" in str(exc.value)


def test_resume_cache_hit():
    script = '''
meta = {"name":"res","description":"resume"}
async def main():
    a = await agent("step one")
    b = await agent("step two")
    return [a, b]
'''
    r1 = _run(script)
    assert r1.ok
    events = []
    r2 = _run(script, resume=r1.run_id, events=events)
    assert r2.ok and r2.result == r1.result
    cached = sum(1 for e in events if e[0] == "workflow.agent.done" and e[1].get("cached"))
    assert cached == 2
    assert r2.stats["tokens"] == 0  # no new tokens on a full cache hit


# ── B1/B2/B3 regression tests: the "5-hour run restarted from step 1" bug ──

def test_auto_resume_without_explicit_id():
    """Re-invoking the SAME script in the SAME session auto-resumes — no
    resumeFromRunId needed. This is the fix for a re-invocation after a failed
    run replaying all ~200 subagents from scratch (the token bonfire)."""
    script = '''
meta = {"name":"ar","description":"auto-resume"}
async def main():
    a = await agent("step one")
    b = await agent("step two")
    return [a, b]
'''
    r1 = _run(script)
    assert r1.ok
    events = []
    r2 = _run(script, events=events)  # NOTE: no resume= passed
    assert r2.ok and r2.result == r1.result
    assert r2.stats["tokens"] == 0          # nothing re-ran
    assert r2.stats["resumed_from"] == r1.run_id
    assert r2.stats["replayed_leaves"] == 2
    cached = sum(1 for e in events if e[0] == "workflow.agent.done" and e[1].get("cached"))
    assert cached == 2


def test_auto_resume_skips_when_script_changed():
    """A different script must NOT auto-resume off an unrelated prior run."""
    s1 = 'meta={"name":"a","description":"d"}\nasync def main():\n    return await agent("alpha task")\n'
    s2 = 'meta={"name":"a","description":"d"}\nasync def main():\n    return await agent("beta task")\n'
    r1 = _run(s1)
    assert r1.ok
    r2 = _run(s2)
    assert r2.ok
    assert r2.stats["resumed_from"] is None
    assert r2.stats["tokens"] > 0           # beta actually ran


def test_auto_resume_can_be_disabled():
    script = 'meta={"name":"a","description":"d"}\nasync def main():\n    return await agent("only step")\n'
    r1 = _run(script)
    assert r1.ok
    r2 = _run(script, cfg={"auto_resume": False})
    assert r2.ok
    assert r2.stats["resumed_from"] is None
    assert r2.stats["tokens"] > 0           # re-ran because auto-resume off


def test_parallel_fanout_resumes_fully():
    """B3: identical fan-out calls resume to a 100% cache hit. The old global
    spawn-order key could miss under parallel/pipeline reordering."""
    script = '''
meta = {"name":"fan","description":"parallel identical"}
async def main():
    outs = await parallel([(lambda i=i: agent("same prompt")) for i in range(8)])
    return {"n": sum(1 for x in outs if x)}
'''
    r1 = _run(script)
    assert r1.ok and r1.result["n"] == 8
    events = []
    r2 = _run(script, events=events)
    assert r2.ok and r2.result["n"] == 8
    assert r2.stats["tokens"] == 0          # all 8 replayed from cache
    cached = sum(1 for e in events if e[0] == "workflow.agent.done" and e[1].get("cached"))
    assert cached == 8


def test_call_key_is_occurrence_keyed_not_spawn_order():
    from agent.workflow.journal import call_key, call_signature
    opts = {"model": "m", "schema": None, "label": "ignored"}
    # Whitespace-normalized prompt + occurrence are what matter.
    assert call_key("do  X", opts, "P", 0) == call_key("do X", opts, "P", 0)
    # Different occurrence → different key (the k-th identical twin).
    assert call_key("do X", opts, "P", 0) != call_key("do X", opts, "P", 1)
    # Signature ignores label / non-result opts but honors model + phase.
    assert call_signature("p", {"label": "a"}, "P") == call_signature("p", {"label": "b"}, "P")
    assert call_signature("p", {"model": "m1"}, "P") != call_signature("p", {"model": "m2"}, "P")
    assert call_signature("p", {}, "P1") != call_signature("p", {}, "P2")


def test_failed_leaf_not_cached_reruns_on_resume():
    """A transient failure must re-run live on resume, not be pinned as a
    permanent None (the old code journaled every leaf as status='done')."""
    state = {"fail_next": True}

    def flaky(parent_agent, prompt, **kw):
        if "boom" in prompt and state["fail_next"]:
            state["fail_next"] = False
            return {"text": "", "status": "failed", "input_tokens": 1, "output_tokens": 1}
        return {"text": f"ok:{prompt[:10]}", "status": "completed",
                "input_tokens": 5, "output_tokens": 5}

    dt.run_workflow_leaf = flaky
    try:
        script = '''
meta = {"name":"fl","description":"flaky"}
async def main():
    good = await agent("step good")
    bad = await agent("step boom")
    return {"good": good, "bad": bad}
'''
        r1 = _run(script)
        assert r1.ok and r1.result["bad"] is None       # failed → None first time
        r2 = _run(script)                                # auto-resume
        assert r2.ok
        assert r2.result["good"] == r1.result["good"]    # success replayed from cache
        assert r2.result["bad"] == "ok:step boom"        # failure re-ran and succeeded
    finally:
        dt.run_workflow_leaf = _fake_leaf


def test_failure_payload_points_to_resume_not_rewrite():
    """B1: the Workflow tool's failure guidance must carry the runId so the
    model resumes instead of restarting from scratch."""
    import tools.workflow_tool as wt
    parent = _make_parent()
    bad_js = 'meta={"name":"x","description":"y"}\nasync def main():\n    f = (z => z)\n    return 1\n'
    out = json.loads(wt.workflow_tool(script=bad_js, parent_agent=parent))
    assert out["success"] is False
    assert "resume" in out and out["runId"] in out["resume"]
    assert "fix" in out  # syntax-flavored error still gets the Python hint

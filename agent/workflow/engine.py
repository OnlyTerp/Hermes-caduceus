"""Top-level Loom orchestration: :func:`run_workflow`.

Resolves the script source (inline / scriptPath / saved name), validates and
compiles it in the sandbox, wires the budget/journal/emitter/scheduler/runner,
runs the script's ``async main()`` on a dedicated event loop, and returns a
:class:`WorkflowResult`. Designed to be called synchronously from the Workflow
tool handler (it blocks until the workflow completes, streaming ``workflow.*``
events the whole time).
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .budget import Budget, BudgetExceeded
from .dsl import build_dsl
from .events import WorkflowEmitter
from .journal import Journal, load_resume_cache
from .runner import LeafRunner
from .sandbox import SandboxError, compile_workflow
from .scheduler import WorkflowScheduler


@dataclass
class WorkflowResult:
    run_id: str
    ok: bool
    result: Any = None
    error: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    stats: Dict[str, Any] = field(default_factory=dict)
    script_path: Optional[str] = None


def _new_run_id() -> str:
    return "wf_" + uuid.uuid4().hex[:10]


def _session_workflows_dir(parent_agent) -> Optional[str]:
    try:
        from hermes_constants import get_hermes_home
        sid = getattr(parent_agent, "session_id", None) or "default"
        base = os.path.join(str(get_hermes_home()), "workflows", str(sid))
        os.makedirs(base, exist_ok=True)
        return base
    except Exception:
        return None


def _load_saved_workflow(name: str) -> Optional[str]:
    """Load a saved workflow script by name from .hermes/workflows/<name>.py."""
    try:
        from hermes_constants import get_hermes_home
        for cand in (f"{name}.py", f"{name}.workflow.py", name):
            path = os.path.join(str(get_hermes_home()), "workflows", "saved", cand)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return f.read()
    except Exception:
        pass
    return None


def _resolve_source(script: Optional[str], name: Optional[str], script_path: Optional[str]) -> str:
    if script_path:
        with open(script_path, encoding="utf-8") as f:
            return f.read()
    if name:
        src = _load_saved_workflow(name)
        if src is None:
            raise SandboxError(f"unknown saved workflow: {name}")
        return src
    if script:
        return script
    raise SandboxError("Workflow requires one of: script, name, or scriptPath")


def run_workflow(
    *,
    parent_agent,
    emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    script: Optional[str] = None,
    name: Optional[str] = None,
    script_path: Optional[str] = None,
    args: Any = None,
    resume_from: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> WorkflowResult:
    """Run a workflow to completion and return its result.

    ``config`` is the resolved ``caduceus.workflow`` section (concurrency,
    budget, timeouts, retries, persistence). ``emit`` receives
    ``(event_type, payload)`` for every ``workflow.*`` event.
    """
    run_id = _new_run_id()
    cfg = dict(config or {})
    emitter = WorkflowEmitter(emit, run_id)

    # Resolve concurrency + budget from Caduceus state / config.
    cstate = None
    try:
        from agent import caduceus as _cad
        cstate = getattr(parent_agent, "caduceus", None)
        concurrency = _cad.resolve_concurrency(cstate)
        budget_total = cstate.budget_tokens if cstate else None
    except Exception:
        concurrency = 8
        budget_total = None

    # /local: build the GPU-aware worker gate when local mode is active. Workers
    # route through it (capped to serving slots, hot-swapped on demand); the
    # orchestrator stays on the cloud/session model. None when /local is off.
    local_gate = None
    try:
        from agent import caduceus as _cad2
        from agent.workflow.local_gate import LocalGate

        _mgr = _cad2.local_manager_for(cstate)
        if _mgr is not None and _mgr.has_models():
            local_gate = LocalGate(_mgr)
    except Exception as _lg_exc:  # pragma: no cover - defensive
        emitter.log(f"local mode unavailable: {_lg_exc}")
        local_gate = None
    if cfg.get("default_budget_tokens"):
        budget_total = budget_total or cfg.get("default_budget_tokens")
    max_agents = int(cfg.get("max_agents", 1000) or 1000)
    run_timeout = float(cfg.get("run_timeout_seconds", 0) or 0)
    schema_retries = int(cfg.get("schema_max_retries", 2) or 0)
    persist = bool(cfg.get("persist_scripts", True))

    # Session dir + script persistence + resume.
    sess_dir = _session_workflows_dir(parent_agent) if persist else None
    run_dir = os.path.join(sess_dir, run_id) if sess_dir else None
    resume_cache: Dict[str, Any] = {}
    if resume_from and sess_dir:
        resume_cache = load_resume_cache(sess_dir, resume_from)

    budget = Budget(budget_total)
    journal = Journal(run_dir, resume_cache=resume_cache)
    runner = LeafRunner(parent_agent, emitter, budget, journal, schema_max_retries=schema_retries)
    scheduler = WorkflowScheduler(runner, emitter, budget, concurrency=concurrency,
                                  max_agents=max_agents, local_gate=local_gate)

    # Resolve + persist source.
    try:
        source = _resolve_source(script, name, script_path)
    except (SandboxError, OSError) as exc:
        emitter.error(message=str(exc))
        return WorkflowResult(run_id=run_id, ok=False, error=str(exc))

    persisted_path = script_path
    if run_dir and not script_path:
        try:
            os.makedirs(run_dir, exist_ok=True)
            persisted_path = os.path.join(run_dir, "script.py")
            with open(persisted_path, "w", encoding="utf-8") as f:
                f.write(source)
        except Exception:
            persisted_path = None

    # Nested workflow() support (one level), sharing this run's scheduler.
    async def _nested(name_or_ref: Any, child_args: Any, sched: WorkflowScheduler) -> Any:
        if isinstance(name_or_ref, dict) and name_or_ref.get("scriptPath"):
            with open(name_or_ref["scriptPath"], encoding="utf-8") as f:
                child_src = f.read()
        elif isinstance(name_or_ref, str):
            child_src = _load_saved_workflow(name_or_ref)
            if child_src is None:
                raise RuntimeError(f"unknown saved workflow: {name_or_ref}")
        else:
            raise RuntimeError("workflow() expects a saved name or {'scriptPath': ...}")
        child_dsl = build_dsl(sched, budget, child_args)
        child_meta, child_main = compile_workflow(child_src, child_dsl)
        sched.log(f"▸ {child_meta.get('name', 'workflow')}")
        prev = sched.depth
        sched.depth = 1
        try:
            return await child_main()
        finally:
            sched.depth = prev

    scheduler.nested_runner = _nested

    # Compile + run.
    dsl = build_dsl(scheduler, budget, args)
    started = time.monotonic()
    try:
        meta, main = compile_workflow(source, dsl)
    except SandboxError as exc:
        emitter.error(message=str(exc))
        scheduler.shutdown()
        return WorkflowResult(run_id=run_id, ok=False, error=str(exc), script_path=persisted_path)

    emitter.start(
        name=meta.get("name", "workflow"),
        description=meta.get("description", ""),
        phases=meta.get("phases", []),
        budget_total=budget.total,
        concurrency_cap=concurrency,
    )

    result_holder: Dict[str, Any] = {}

    def _runner_thread() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            coro = main()
            if run_timeout > 0:
                coro = asyncio.wait_for(coro, timeout=run_timeout)
            result_holder["result"] = loop.run_until_complete(coro)
        except BudgetExceeded as exc:
            result_holder["result"] = None
            result_holder["budget_stop"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            result_holder["error"] = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    import threading
    t = threading.Thread(target=_runner_thread, name=f"loom-{run_id}", daemon=True)
    t.start()
    t.join()
    scheduler.shutdown()

    ms = int((time.monotonic() - started) * 1000)
    stats = {
        "agents": scheduler._agent_count,
        "tokens": budget.spent(),
        "ms": ms,
        "concurrency": concurrency,
        "budget_total": budget.total,
    }

    if "error" in result_holder:
        err = result_holder["error"]
        emitter.error(message=f"{type(err).__name__}: {err}")
        emitter.complete(result_summary=f"error: {err}", agents=scheduler._agent_count,
                         input_tokens=0, output_tokens=budget.spent(), ms=ms)
        return WorkflowResult(run_id=run_id, ok=False, error=f"{type(err).__name__}: {err}",
                              meta=meta, stats=stats, script_path=persisted_path)

    result = result_holder.get("result")
    summary = _result_summary(result)
    if result_holder.get("budget_stop"):
        summary = f"[budget reached] {summary}"
    emitter.complete(result_summary=summary, agents=scheduler._agent_count,
                     input_tokens=0, output_tokens=budget.spent(), ms=ms)
    return WorkflowResult(run_id=run_id, ok=True, result=result, meta=meta, stats=stats,
                          script_path=persisted_path)


def _result_summary(result: Any, limit: int = 400) -> str:
    if result is None:
        return "(no return value)"
    try:
        import json
        s = json.dumps(result, ensure_ascii=False, default=str) if not isinstance(result, str) else result
    except Exception:
        s = str(result)
    s = s.strip()
    return s[:limit] + ("…" if len(s) > limit else "")

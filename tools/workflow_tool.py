"""The ``Workflow`` tool — Caduceus's entry point into the Loom engine.

Registers a ``Workflow`` tool whose schema mirrors UltraCode's. The handler
runs the workflow to completion (streaming live ``workflow.*`` events to the
desktop/CLI via the agent's ``tool_progress_callback``) and returns the final
result, the ``runId`` (for resume), the persisted ``scriptPath``, and run stats.

The full model-visible description (the standing opt-in policy, the script API,
the quality patterns) lives in :data:`agent.caduceus.WORKFLOW_TOOL_DESCRIPTION`.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from tools.registry import registry

logger = logging.getLogger(__name__)


WORKFLOW_SCHEMA = {
    "name": "Workflow",
    "parameters": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "Self-contained workflow script (restricted Python). Must define a "
                    "`meta` dict (name, description, phases) and an async `main()` that "
                    "uses agent()/parallel()/pipeline()/phase()/log(). Pass inline — it is "
                    "auto-persisted under the session dir and its path returned for resume."
                ),
            },
            "name": {
                "type": "string",
                "description": "Name of a saved workflow (from .hermes/workflows/saved/). Resolves to a script.",
            },
            "description": {"type": "string", "description": "Ignored — set the description in meta."},
            "title": {"type": "string", "description": "Ignored — set the title in meta."},
            "args": {
                "description": (
                    "Optional value exposed to the script as the global `args`, verbatim. "
                    "Pass arrays/objects as actual JSON values, not a JSON-encoded string."
                ),
            },
            "scriptPath": {
                "type": "string",
                "description": (
                    "Path to a workflow script on disk. Takes precedence over script/name. "
                    "Edit the persisted script and re-invoke with the same scriptPath to iterate."
                ),
            },
            "resumeFromRunId": {
                "type": "string",
                "description": (
                    "Run id of a prior Workflow invocation to resume from. Usually unnecessary: "
                    "re-invoking the SAME script in the same session AUTO-RESUMES from the most "
                    "recent matching run, so completed subagents replay from cache for free. Pass "
                    "this only to force-pin a specific run's journal. Unchanged agent() calls "
                    "return cached results instantly; the first edited/new call runs live."
                ),
                "pattern": "^wf_[a-z0-9-]{6,}$",
            },
        },
        "additionalProperties": False,
    },
}


def _build_description() -> str:
    try:
        from agent.caduceus import WORKFLOW_TOOL_DESCRIPTION
        return WORKFLOW_TOOL_DESCRIPTION
    except Exception:
        return "Execute a workflow script that orchestrates multiple subagents deterministically."


def _emit_via_agent(parent_agent):
    """Return an emit(event_type, payload) that bridges to the agent callbacks."""
    cb = getattr(parent_agent, "tool_progress_callback", None)
    if cb is None:
        return None

    def _emit(event_type: str, payload: Dict[str, Any]) -> None:
        try:
            cb(event_type, **payload)
        except TypeError:
            # Older callbacks may not accept arbitrary kwargs; degrade to preview.
            try:
                cb(event_type, preview=json.dumps(payload, default=str)[:200])
            except Exception:
                pass
        except Exception:
            pass

    return _emit


def workflow_tool(
    script: Optional[str] = None,
    name: Optional[str] = None,
    args: Any = None,
    script_path: Optional[str] = None,
    resume_from_run_id: Optional[str] = None,
    parent_agent=None,
    task_id: str = None,
) -> str:
    from agent.workflow.engine import run_workflow

    if parent_agent is None:
        return json.dumps({"success": False, "error": "Workflow requires an orchestrating agent context."})

    # Resolve the caduceus.workflow runtime config.
    try:
        from hermes_cli.config import load_config_readonly
        wf_cfg = (load_config_readonly().get("caduceus") or {}).get("workflow") or {}
    except Exception:
        wf_cfg = {}

    emit = _emit_via_agent(parent_agent)
    try:
        res = run_workflow(
            parent_agent=parent_agent,
            emit=emit,
            script=script,
            name=name,
            script_path=script_path,
            args=args,
            resume_from=resume_from_run_id,
            config=wf_cfg,
        )
    except Exception as exc:
        logger.exception("Workflow run failed")
        return json.dumps({"success": False, "error": f"{type(exc).__name__}: {exc}"})

    payload: Dict[str, Any] = {
        "success": res.ok,
        "runId": res.run_id,
        "result": res.result,
        "stats": res.stats,
        "workflow": res.meta.get("name") if res.meta else None,
    }
    if res.script_path:
        payload["scriptPath"] = res.script_path
    replayed = (res.stats or {}).get("replayed_leaves") or 0
    resumed_from = (res.stats or {}).get("resumed_from")
    if not res.ok:
        payload["error"] = res.error
        # CRITICAL: re-invoking the SAME script in this session auto-resumes from
        # this run's journal — completed subagents replay from cache, only the
        # failed/remaining work runs live. Do NOT rewrite the script from scratch
        # or you forfeit the cache and re-run everything (huge token waste).
        payload["resume"] = (
            f"To finish this run, re-invoke Workflow with the SAME script (or "
            f"scriptPath={res.script_path!r}). Completed subagents auto-resume from "
            f"cache; pass resumeFromRunId={res.run_id!r} to force-pin this run's "
            f"journal. Only fix the specific failing part — keep everything else "
            f"byte-for-byte identical so its cache keys still match."
        )
        # Syntax fix only when the error looks like a compile/sandbox problem.
        err_l = (res.error or "").lower()
        if any(t in err_l for t in ("sandbox", "syntax", "javascript", "compile", "invalid")):
            payload["fix"] = (
                "Scripts are PYTHON, not JavaScript. Use this exact shape and re-call Workflow:\n"
                "meta = {\"name\": \"...\", \"description\": \"...\", \"phases\": [{\"title\": \"...\"}]}\n"
                "async def main():\n"
                "    phase(\"...\")\n"
                "    out = await agent(\"<prompt>\", label=\"...\", phase=\"...\")\n"
                "    return {\"result\": out}\n"
                "No const/let/var, no `=>` (use `lambda x:` or `async def`), no imports, no markdown fences."
            )
    if res.ok:
        note = (
            "Workflow complete. Read `result` and decide the next phase. To iterate, "
            "edit the script at `scriptPath` and re-invoke with that scriptPath — "
            "unchanged subagents auto-resume from cache, so only edited/new work re-runs."
        )
        if replayed:
            note = (
                f"Workflow complete (auto-resumed {replayed} subagent(s) from "
                f"{resumed_from}, so they cost no new tokens). " + note
            )
        payload["note"] = note
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"success": res.ok, "runId": res.run_id, "result": str(res.result)})


def check_requirements() -> bool:
    # Always available — the standing opt-in policy in the description gates usage.
    return True


registry.register(
    name="Workflow",
    toolset="caduceus",
    schema={**WORKFLOW_SCHEMA, "description": _build_description()},
    handler=lambda args, **kw: workflow_tool(
        script=args.get("script"),
        name=args.get("name"),
        args=args.get("args"),
        script_path=args.get("scriptPath"),
        resume_from_run_id=args.get("resumeFromRunId"),
        parent_agent=kw.get("parent_agent"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_requirements,
    emoji="⚕",
    description=_build_description(),
    max_result_size_chars=200_000,
)

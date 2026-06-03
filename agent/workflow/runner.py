"""Leaf execution for the Loom.

Each ``agent()`` call becomes one delegate child run on the worker tier. The
:class:`LeafRunner` wraps :func:`tools.delegate_tool.run_workflow_leaf` with:

* **journal cache** — instant replay on resume when the call is unchanged;
* **structured output** — schema instruction + parse/validate + retry;
* **empty-result retry** — transparent re-run of transient blips;
* **budget** — pre-check (hard ceiling) + post-accounting;
* **events** — spawn/status/delta/tokens/done emitted around the run.

``run_blocking`` is synchronous (it drives a child agent in this thread); the
scheduler invokes it via ``loop.run_in_executor`` so many leaves run
concurrently without blocking the asyncio loop.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from . import reliability, structured
from .budget import Budget, BudgetExceeded
from .events import WorkflowEmitter
from .journal import Journal, call_key


class LeafRunner:
    def __init__(self, parent_agent, emitter: WorkflowEmitter, budget: Budget,
                 journal: Journal, *, schema_max_retries: int = 2,
                 empty_retries: int = reliability.DEFAULT_EMPTY_RETRIES):
        self.parent_agent = parent_agent
        self.emitter = emitter
        self.budget = budget
        self.journal = journal
        self.schema_max_retries = max(0, int(schema_max_retries))
        self.empty_retries = max(0, int(empty_retries))

    def run_blocking(self, agent_id: str, prompt: str, opts: Dict[str, Any], call_index: int) -> Any:
        """Run one leaf to completion and return its result (text or object).

        Returns ``None`` on hard failure (so ``[x for x in r if x]`` degrades
        gracefully). Raises :class:`BudgetExceeded` only via the scheduler's
        pre-check — by the time we're here the slot was already granted.
        """
        opts = opts or {}
        phase = opts.get("phase")
        schema = opts.get("schema")
        label = opts.get("label") or (prompt[:48] + ("…" if len(prompt) > 48 else ""))
        model_override = opts.get("model")
        role = opts.get("role") or "leaf"
        key = call_key(prompt, opts, phase, call_index)

        # Resume cache hit → instant replay.
        hit, cached = self.journal.lookup(key)
        if hit:
            self.emitter.agent_done(agent_id=agent_id, status="done", cached=True,
                                    summary=_summarize(cached))
            return cached

        self.emitter.agent_status(agent_id=agent_id, status="running")
        callbacks = {
            "on_reasoning": lambda t: self.emitter.agent_delta(agent_id=agent_id, kind="reasoning", text=t),
            "on_text": lambda t: self.emitter.agent_delta(agent_id=agent_id, kind="text", text=t),
        }

        started = time.monotonic()
        value, status, in_tok, out_tok = self._execute(
            agent_id, prompt, schema, role, model_override, callbacks, opts,
        )
        ms = int((time.monotonic() - started) * 1000)

        self.budget.add(out_tok)
        self.emitter.agent_tokens(agent_id=agent_id, input_tokens=in_tok, output_tokens=out_tok)
        self.emitter.agent_done(agent_id=agent_id, status=status, summary=_summarize(value),
                                input_tokens=in_tok, output_tokens=out_tok, ms=ms)
        self.journal.record(key, prompt=prompt, phase=phase, result=value, status="done",
                            tokens={"in": in_tok, "out": out_tok}, label=label)
        return value

    def _execute(self, agent_id, prompt, schema, role, model_override, callbacks, opts):
        """Run the leaf with structured-output + empty-result retries."""
        from tools.delegate_tool import run_workflow_leaf

        provider_override = opts.get("provider")
        toolsets = opts.get("toolsets")
        # /local mode: the scheduler's GPU gate resolves a local worker's
        # endpoint+model and stashes the creds bundle here so the leaf targets
        # the local server directly (bypassing tier/router resolution).
        creds_override = opts.get("_local_creds")
        total_in = total_out = 0

        if schema:
            attempts = self.schema_max_retries + 1
            feedback = ""
            for attempt in range(attempts):
                full_prompt = prompt + structured.build_instruction(schema) + feedback
                res = run_workflow_leaf(
                    self.parent_agent, full_prompt, toolsets=toolsets, role=role,
                    model_override=model_override, provider_override=provider_override,
                    agent_type=opts.get("agent_type"), callbacks=callbacks,
                    agent_index=_seq(agent_id), creds_override=creds_override,
                )
                total_in += res.get("input_tokens", 0)
                total_out += res.get("output_tokens", 0)
                if reliability.is_hard_failure(res) and not res.get("text"):
                    if attempt < attempts - 1:
                        continue
                    return None, "failed", total_in, total_out
                ok, value, err = structured.parse_and_validate(res.get("text", ""), schema)
                if ok:
                    return value, "done", total_in, total_out
                feedback = (
                    f"\n\n[Your previous response was invalid: {err} "
                    f"Return ONLY a corrected JSON value.]"
                )
            return None, "failed", total_in, total_out

        # Plain text leaf with empty-result retry.
        attempts = self.empty_retries + 1
        last_res: Dict[str, Any] = {}
        for attempt in range(attempts):
            res = run_workflow_leaf(
                self.parent_agent, prompt, toolsets=toolsets, role=role,
                model_override=model_override, provider_override=provider_override,
                agent_type=opts.get("agent_type"), callbacks=callbacks,
                agent_index=_seq(agent_id), creds_override=creds_override,
            )
            last_res = res
            total_in += res.get("input_tokens", 0)
            total_out += res.get("output_tokens", 0)
            if not reliability.is_retryable(res):
                break
        if reliability.is_hard_failure(last_res) and not last_res.get("text"):
            return None, "failed", total_in, total_out
        return last_res.get("text") or None, "done", total_in, total_out


def _seq(agent_id: str) -> int:
    """Derive a small int index from an agent id for the delegate log prefix."""
    try:
        return int(agent_id.rsplit("-", 1)[-1])
    except Exception:
        return 0


def _summarize(value: Any, limit: int = 240) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        s = value
    else:
        try:
            import json as _json
            s = _json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            s = str(value)
    s = s.strip()
    return s[:limit] + ("…" if len(s) > limit else "")

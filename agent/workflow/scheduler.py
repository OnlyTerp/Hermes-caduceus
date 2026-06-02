"""Asyncio dataflow scheduler + DSL primitives for the Loom.

Runs the workflow script's ``async main()`` on a dedicated event loop. ``agent()``
returns an awaitable backed by a bounded worker pool (concurrency cap); excess
calls queue. ``pipeline()`` streams each item through all stages independently
(NO barrier — item A can be in stage 3 while B is in stage 1, the latency win);
``parallel()`` is a barrier that gathers all and maps failures to ``None``.

Determinism: ``time``/``random``/wall-clock are absent from the script body, so
the only nondeterminism is model output inside leaves — which the journal caches
for resume.
"""

from __future__ import annotations

import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, Optional

from .budget import Budget, BudgetExceeded
from .events import WorkflowEmitter
from .runner import LeafRunner


async def _maybe_await(x: Any) -> Any:
    if inspect.isawaitable(x):
        return await x
    return x


def _call_stage(stage: Callable, prev: Any, item: Any, index: int) -> Any:
    """Call a pipeline stage with the right arity (1, 2, or 3 args)."""
    try:
        sig = inspect.signature(stage)
        params = [
            p for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())
    except (TypeError, ValueError):
        params, has_varargs = [None], False
    n = len(params)
    if has_varargs or n >= 3:
        return stage(prev, item, index)
    if n == 2:
        return stage(prev, item)
    return stage(prev)


class WorkflowScheduler:
    def __init__(self, runner: LeafRunner, emitter: WorkflowEmitter, budget: Budget,
                 *, concurrency: int, max_agents: int, depth: int = 0):
        self.runner = runner
        self.emitter = emitter
        self.budget = budget
        self.concurrency = max(1, int(concurrency))
        self.max_agents = max(1, int(max_agents))
        self.depth = depth
        self._sem = asyncio.Semaphore(self.concurrency)
        self._executor = ThreadPoolExecutor(max_workers=self.concurrency,
                                            thread_name_prefix="loom")
        self._agent_count = 0
        self._current_phase: Optional[str] = None
        self._phase_index = 0
        self._run_prefix = (emitter.run_id or "wf").replace("wf_", "")[:6]
        # Engine hook for nested workflow() calls (set by the engine).
        self.nested_runner: Optional[Callable] = None

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)

    # ---- DSL: phase / log --------------------------------------------
    def phase(self, title: str) -> None:
        self._current_phase = str(title)
        self._phase_index += 1
        self.emitter.phase(phase=self._current_phase, index=self._phase_index)

    def log(self, message: str) -> None:
        self.emitter.log(message)

    # ---- DSL: agent --------------------------------------------------
    async def agent(self, prompt: str, *, label: Optional[str] = None, phase: Optional[str] = None,
                    schema: Optional[dict] = None, model: Optional[str] = None,
                    isolation: Optional[str] = None, agent_type: Optional[str] = None,
                    **extra) -> Any:
        self.budget.check()  # hard ceiling — raises BudgetExceeded
        self._agent_count += 1
        if self._agent_count > self.max_agents:
            raise RuntimeError(
                f"Caduceus workflow exceeded the {self.max_agents}-agent backstop "
                f"(runaway loop?)."
            )
        idx = self._agent_count
        agent_id = f"{self._run_prefix}-{idx}"
        eff_phase = phase or self._current_phase
        provider = None
        if model and ":" in model:
            provider, _, model = model.partition(":")
            provider = provider.strip() or None
            model = model.strip()
        opts = {
            "label": label, "phase": eff_phase, "schema": schema, "model": model,
            "provider": provider, "isolation": isolation, "agent_type": agent_type,
        }
        opts.update(extra)
        self.emitter.agent_spawn(
            agent_id=agent_id,
            label=label or (str(prompt)[:48] + ("…" if len(str(prompt)) > 48 else "")),
            phase=eff_phase, model=model,
        )
        self.emitter.agent_status(agent_id=agent_id, status="queued")
        async with self._sem:
            loop = asyncio.get_running_loop()
            try:
                result = await loop.run_in_executor(
                    self._executor, self.runner.run_blocking, agent_id, str(prompt), opts, idx,
                )
            except BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 — leaf failures resolve to None
                self.emitter.agent_done(agent_id=agent_id, status="failed", summary=str(exc)[:200])
                self.emitter.error(message=str(exc), agent_id=agent_id)
                return None
        self.emitter.budget(spent=self.budget.spent(), total=self.budget.total)
        return result

    # ---- DSL: parallel (barrier) -------------------------------------
    async def parallel(self, thunks: List[Callable]) -> List[Any]:
        thunks = list(thunks)
        self.emitter.parallel_barrier(phase=self._current_phase, count=len(thunks))

        async def _safe(thunk: Callable) -> Any:
            try:
                return await _maybe_await(thunk())
            except BudgetExceeded:
                raise
            except Exception:
                return None

        return list(await asyncio.gather(*[_safe(t) for t in thunks]))

    # ---- DSL: pipeline (no barrier) ----------------------------------
    async def pipeline(self, items, *stages: Callable) -> List[Any]:
        items = list(items)
        stages = list(stages)

        async def _run_item(item: Any, i: int) -> Any:
            cur = item
            for s_idx, stage in enumerate(stages):
                self.emitter.pipeline_stage(item_index=i, stage_index=s_idx)
                try:
                    cur = await _maybe_await(_call_stage(stage, cur, item, i))
                except BudgetExceeded:
                    raise
                except Exception:
                    return None
            return cur

        return list(await asyncio.gather(*[_run_item(it, i) for i, it in enumerate(items)]))

    # ---- DSL: workflow (nested, one level) ---------------------------
    async def workflow(self, name_or_ref: Any, args: Any = None) -> Any:
        if self.depth >= 1:
            raise RuntimeError("workflow() nesting is one level deep only")
        if self.nested_runner is None:
            raise RuntimeError("nested workflows are not available in this run")
        return await self.nested_runner(name_or_ref, args, self)

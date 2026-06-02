"""``workflow.*`` event emission for the Loom.

A :class:`WorkflowEmitter` wraps a low-level ``emit(event_type, payload)``
callback (provided by the Workflow tool handler, which bridges to the
tui_gateway / CLI renderers) and offers typed, thread-safe helpers for every
workflow event. Per-agent text/reasoning deltas are coalesced (throttled) so a
high-fan-out run can't flood the WebSocket.

Events mirror the design's protocol (see DESIGN.md §9):
  workflow.start / phase / agent.spawn / agent.status / agent.delta /
  agent.tokens / agent.done / parallel.barrier / pipeline.stage / verify /
  budget / log / complete / error
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional


class WorkflowEmitter:
    def __init__(
        self,
        emit: Optional[Callable[[str, Dict[str, Any]], None]],
        run_id: str,
        *,
        delta_interval: float = 0.08,
    ):
        self._emit = emit
        self.run_id = run_id
        self._delta_interval = delta_interval
        self._lock = threading.Lock()
        # Per-agent delta coalescing: {agent_id: {"buf": str, "last": ts, "kind": str}}
        self._delta_buf: Dict[str, Dict[str, Any]] = {}

    # ---- low-level ----------------------------------------------------
    def _send(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._emit is None:
            return
        payload = dict(payload)
        payload.setdefault("run_id", self.run_id)
        payload.setdefault("ts", time.time())
        try:
            self._emit(event_type, payload)
        except Exception:
            # Never let UI emission break the workflow.
            pass

    # ---- lifecycle ----------------------------------------------------
    def start(self, *, name: str, description: str, phases: list, budget_total, concurrency_cap: int) -> None:
        self._send("workflow.start", {
            "name": name, "description": description, "phases": phases,
            "budget_total": budget_total, "concurrency_cap": concurrency_cap,
        })

    def phase(self, *, phase: str, index: int) -> None:
        self._send("workflow.phase", {"phase": phase, "index": index})

    def agent_spawn(self, *, agent_id: str, label: str, phase: Optional[str], model: Optional[str], parent_id: Optional[str] = None) -> None:
        self._send("workflow.agent.spawn", {
            "agent_id": agent_id, "label": label, "phase": phase, "model": model, "parent_id": parent_id,
        })

    def agent_status(self, *, agent_id: str, status: str) -> None:
        self._send("workflow.agent.status", {"agent_id": agent_id, "status": status})

    def agent_delta(self, *, agent_id: str, kind: str, text: str) -> None:
        """Coalesce per-agent deltas; flush at most every ``delta_interval``."""
        if not text:
            return
        now = time.time()
        flush: Optional[Dict[str, Any]] = None
        with self._lock:
            rec = self._delta_buf.get(agent_id)
            if rec is None:
                rec = {"buf": "", "last": 0.0, "kind": kind}
                self._delta_buf[agent_id] = rec
            if rec["kind"] != kind and rec["buf"]:
                flush = {"agent_id": agent_id, "kind": rec["kind"], "text": rec["buf"]}
                rec["buf"] = ""
            rec["kind"] = kind
            rec["buf"] += text
            if now - rec["last"] >= self._delta_interval:
                payload = {"agent_id": agent_id, "kind": rec["kind"], "text": rec["buf"]}
                rec["buf"] = ""
                rec["last"] = now
            else:
                payload = None
        if flush:
            self._send("workflow.agent.delta", flush)
        if payload:
            self._send("workflow.agent.delta", payload)

    def _flush_agent_delta(self, agent_id: str) -> None:
        with self._lock:
            rec = self._delta_buf.pop(agent_id, None)
        if rec and rec.get("buf"):
            self._send("workflow.agent.delta", {"agent_id": agent_id, "kind": rec["kind"], "text": rec["buf"]})

    def agent_tokens(self, *, agent_id: str, input_tokens: int, output_tokens: int) -> None:
        self._send("workflow.agent.tokens", {"agent_id": agent_id, "in": input_tokens, "out": output_tokens})

    def agent_done(self, *, agent_id: str, status: str = "done", summary: Optional[str] = None,
                   input_tokens: int = 0, output_tokens: int = 0, ms: int = 0, cached: bool = False) -> None:
        self._flush_agent_delta(agent_id)
        self._send("workflow.agent.done", {
            "agent_id": agent_id, "status": status, "summary": summary,
            "in": input_tokens, "out": output_tokens, "ms": ms, "cached": cached,
        })

    def parallel_barrier(self, *, phase: Optional[str], count: int) -> None:
        self._send("workflow.parallel.barrier", {"phase": phase, "count": count})

    def pipeline_stage(self, *, item_index: int, stage_index: int) -> None:
        self._send("workflow.pipeline.stage", {"item_index": item_index, "stage_index": stage_index})

    def verify(self, *, finding_id: str, votes: list, result: str) -> None:
        self._send("workflow.verify", {"finding_id": finding_id, "votes": votes, "result": result})

    def budget(self, *, spent: int, total) -> None:
        self._send("workflow.budget", {"spent": spent, "total": total})

    def log(self, message: str) -> None:
        self._send("workflow.log", {"message": str(message)})

    def complete(self, *, result_summary: str, agents: int, input_tokens: int, output_tokens: int, ms: int) -> None:
        self._send("workflow.complete", {
            "result_summary": result_summary, "agents": agents,
            "in": input_tokens, "out": output_tokens, "ms": ms, "runId": self.run_id,
        })

    def error(self, *, message: str, agent_id: Optional[str] = None) -> None:
        self._send("workflow.error", {"message": str(message), "agent_id": agent_id})

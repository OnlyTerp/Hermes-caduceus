"""Resume & caching for Loom runs.

Every ``agent()`` result is written to
``<session_dir>/workflows/<run_id>/journal.jsonl``, keyed by a stable hash of
``(normalized_prompt, opts, phase, occurrence)`` where ``occurrence`` is the
k-th call sharing that signature (assigned in program order, NOT global spawn
order — so resume survives ``parallel()`` / ``pipeline()`` reordering). When a
run is resumed — explicitly via ``Workflow(resumeFromRunId=...)`` or
automatically when the same script re-runs in the same session — the prior
run's journal is loaded and unchanged ``agent()`` calls return cached results
instantly; the first edited/new call and everything after runs live.
Same script + same args ⇒ 100% cache hit, even across fan-out.

Per-agent transcripts are written as ``agent-<id>.jsonl`` so the desktop
timeline scrubber can replay a run.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Any, Dict, Optional


def _stable_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


# Opts that change the *result* of a leaf and therefore participate in its
# cache identity. Spawn order / agent id deliberately do NOT — see call_key.
_KEYED_OPT_FIELDS = ("schema", "model", "provider", "agent_type", "isolation")


def call_signature(prompt: str, opts: Dict[str, Any], phase: Optional[str]) -> str:
    """Order-independent identity of an ``agent()`` call.

    Two calls with the same normalized prompt, result-affecting opts, and phase
    share a signature regardless of when they were spawned. This is what makes
    resume survive ``parallel()`` / ``pipeline()`` fan-out, where the asyncio
    interleaving (and thus global spawn order) is not stable across runs.
    """
    norm = " ".join((prompt or "").split())
    keyed_opts = {k: opts.get(k) for k in _KEYED_OPT_FIELDS if opts.get(k) is not None}
    return _stable_dumps([norm, keyed_opts, phase or ""])


def call_key(prompt: str, opts: Dict[str, Any], phase: Optional[str], occurrence: int) -> str:
    """Deterministic cache key for one ``agent()`` call.

    ``occurrence`` is the 0-based index of this call *among calls that share its
    signature* (not the global spawn counter). N identical fan-out calls — e.g.
    ``adversarial_verify``'s skeptics — are interchangeable, so mapping the k-th
    identical call to the k-th cached result is correct no matter which physical
    task finished first. This is the fix for resume cache-misses under parallel
    fan-out: a re-run that issues the same set of calls reproduces the same keys.
    """
    payload = call_signature(prompt, opts, phase) + "#" + str(int(occurrence))
    return "a_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


class Journal:
    """Append-only JSONL journal with same-session resume support."""

    def __init__(self, run_dir: Optional[str], *, resume_cache: Optional[Dict[str, Any]] = None):
        self.run_dir = run_dir
        self._lock = threading.Lock()
        self._cache: Dict[str, Any] = dict(resume_cache or {})
        self._path = os.path.join(run_dir, "journal.jsonl") if run_dir else None
        if run_dir:
            try:
                os.makedirs(run_dir, exist_ok=True)
            except Exception:
                self._path = None

    # ---- resume -------------------------------------------------------
    def lookup(self, key: str):
        """Return ``(hit, value)`` from the resume cache."""
        if key in self._cache:
            return True, self._cache[key]
        return False, None

    # ---- recording ----------------------------------------------------
    def record(self, key: str, *, prompt: str, phase: Optional[str], result: Any,
               status: str, tokens: Dict[str, int], label: str) -> None:
        with self._lock:
            self._cache[key] = result
            if not self._path:
                return
            row = {
                "key": key, "label": label, "phase": phase, "status": status,
                "result": result, "tokens": tokens, "prompt": (prompt or "")[:2000],
            }
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            except Exception:
                pass

    def write_transcript(self, agent_id: str, entries: list) -> None:
        if not self.run_dir:
            return
        try:
            path = os.path.join(self.run_dir, f"agent-{agent_id}.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass


def load_resume_cache(session_workflows_dir: str, resume_run_id: str) -> Dict[str, Any]:
    """Load a prior run's journal into a {key: result} cache for resume."""
    cache: Dict[str, Any] = {}
    path = os.path.join(session_workflows_dir, resume_run_id, "journal.jsonl")
    if not os.path.exists(path):
        return cache
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                key = row.get("key")
                if key and row.get("status") in (None, "done", "completed", "cached"):
                    cache[key] = row.get("result")
    except Exception:
        pass
    return cache

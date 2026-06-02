"""Reliability policy for the Loom (UltraCode-Shim parity).

The deep reliability fixes — empty-turn auto-retry, stalled-stream idle timeout,
tool-call sequence repair — already live inside Hermes's core conversation loop
(``run_agent``) and apply to every leaf for free. This module adds the
Loom-level policy layered on top of a leaf run:

* **empty-result retry** — a leaf that returns no usable text and no error
  (a transient blip) is transparently re-run, capped;
* **failed → None** semantics — a hard failure resolves the leaf to ``None`` so
  ``[x for x in results if x]`` patterns degrade gracefully;
* **per-agent / per-run timeouts** are enforced by the child run path and the
  scheduler respectively.
"""

from __future__ import annotations

from typing import Any, Dict

# Default leaf retry budget for empty/transient results.
DEFAULT_EMPTY_RETRIES = 1


def is_empty_result(res: Dict[str, Any]) -> bool:
    """True when a leaf completed but produced no usable text (a blip)."""
    status = (res or {}).get("status")
    text = (res or {}).get("text") or ""
    return status in (None, "completed", "done") and not text.strip()


def is_retryable(res: Dict[str, Any]) -> bool:
    """True when re-running the leaf is worthwhile (transient, not a hard error)."""
    if is_empty_result(res):
        return True
    status = (res or {}).get("status")
    # A timeout may be transient under load; one retry is reasonable.
    return status == "timeout"


def is_hard_failure(res: Dict[str, Any]) -> bool:
    """True when the leaf failed in a way that should resolve to None."""
    status = (res or {}).get("status")
    return status in ("error", "failed", "interrupted", "timeout")

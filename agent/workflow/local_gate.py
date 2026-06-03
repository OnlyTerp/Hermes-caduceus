"""GPU-aware admission gate for ``/local`` workflow workers.

A single GPU is exclusive and VRAM-bounded, so workflow workers that run on
local models can't simply fan out to the Loom's full concurrency. This gate
enforces **model + profile affinity** on top of the
:class:`~agent.local_manager.LocalModelManager`:

* leaves of the **currently-loaded** ``(model, profile)`` run concurrently up to
  that profile's **slot count**;
* a leaf needing a **different** ``(model, profile)`` waits for the in-flight
  batch to drain, then triggers exactly one **serialized hot-swap**.

The net effect: fan-out is capped to real serving slots, and work is batched by
model so the GPU swaps as little as possible. The orchestrator never goes
through here — it stays on the cloud/session model.

Asyncio-native: the model swap (a blocking subprocess hook) runs in an executor
while the admission lock is held, which is safe because a swap only happens when
nothing is in flight.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Tuple


class LocalGate:
    def __init__(self, manager, *, default_ctx: int = 0):
        self.manager = manager
        self.default_ctx = int(default_ctx or 0)
        self._cond = asyncio.Condition()
        self._cur_key: Optional[Tuple[str, str]] = None  # (model_id, profile_key)
        self._cur_slots = 0
        self._inflight = 0

    @property
    def max_slots(self) -> int:
        try:
            return self.manager.max_capacity()
        except Exception:
            return 1

    def _target(self, opts: Dict[str, Any]):
        """Resolve (model_id, profile_key, slots, want_ctx) for a leaf, or None."""
        model_id = self.manager.resolve_worker_id(opts.get("model"), opts.get("provider"))
        if not model_id:
            return None
        m = self.manager.model(model_id)
        if m is None:
            return None
        want_ctx = 0
        try:
            want_ctx = int(opts.get("min_ctx") or self.default_ctx or 0)
        except (TypeError, ValueError):
            want_ctx = self.default_ctx
        prof = m.select_profile(want_ctx=want_ctx)
        return model_id, prof.key(), prof.slots, want_ctx

    def _can_admit(self, key: Tuple[str, str]) -> bool:
        if self._cur_key is None:
            return True
        if self._cur_key == key:
            return self._inflight < self._cur_slots
        # Different model/profile: only once the current batch has fully drained.
        return self._inflight == 0

    async def acquire(self, opts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Admit a leaf, loading/swapping the GPU model if needed.

        Returns the local credential bundle to run the leaf with, or ``None`` if
        no local model applies (caller falls back to the normal path).
        """
        target = self._target(opts)
        if target is None:
            return None
        model_id, prof_key, prof_slots, want_ctx = target
        key = (model_id, prof_key)
        async with self._cond:
            while not self._can_admit(key):
                await self._cond.wait()
            if self._cur_key != key:
                # Guaranteed nothing in flight here (see _can_admit). Swap under
                # the lock so no other leaf is admitted mid-swap.
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None, lambda: self.manager.ensure(model_id, want_ctx=want_ctx)
                )
                self._cur_key = key
                self._cur_slots = max(1, prof_slots)
            self._inflight += 1
            return self.manager.creds(model_id)

    async def release(self) -> None:
        async with self._cond:
            if self._inflight > 0:
                self._inflight -= 1
            self._cond.notify_all()

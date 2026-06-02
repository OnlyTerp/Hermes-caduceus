"""Shared token budget for a Loom run.

A single :class:`Budget` is shared across the workflow's main loop and every
leaf (UltraCode parity: the pool is shared, not per-workflow). ``total`` is a
HARD ceiling — once ``spent()`` reaches it, further ``agent()`` calls raise
:class:`BudgetExceeded` so the script's ``while budget.remaining() > N`` loops
terminate deterministically.
"""

from __future__ import annotations

import threading
from typing import Optional


class BudgetExceeded(Exception):
    """Raised by the scheduler when an agent() call would exceed the budget."""


class Budget:
    """Thread-safe shared output-token budget exposed to the workflow script."""

    def __init__(self, total: Optional[int] = None):
        self.total: Optional[int] = int(total) if total else None
        self._spent = 0
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        if not n:
            return
        with self._lock:
            self._spent += int(n)

    def spent(self) -> int:
        with self._lock:
            return self._spent

    def remaining(self) -> float:
        if self.total is None:
            return float("inf")
        with self._lock:
            return max(0, self.total - self._spent)

    def exhausted(self) -> bool:
        if self.total is None:
            return False
        with self._lock:
            return self._spent >= self.total

    def check(self) -> None:
        """Raise BudgetExceeded if the hard ceiling has been reached."""
        if self.exhausted():
            raise BudgetExceeded(
                f"Caduceus workflow budget exhausted ({self.spent()}/{self.total} output tokens)"
            )

"""The Loom — Caduceus's dynamic-workflow engine.

The Loom runs a sandboxed Python *workflow script* that orchestrates many
delegate subagents deterministically (UltraCode parity): ``agent()`` spawns one
leaf on the worker tier, ``parallel()`` fans out with a barrier, ``pipeline()``
streams items through stages without a barrier, ``phase()``/``log()`` narrate,
and ``budget`` enforces a shared token ceiling. Structured-output schemas,
resume/caching, and live ``workflow.*`` events make it observable and reliable.

Public entry point: :func:`agent.workflow.engine.run_workflow`.
"""

from .engine import run_workflow, WorkflowResult  # noqa: F401
from .budget import Budget, BudgetExceeded  # noqa: F401
from .events import WorkflowEmitter  # noqa: F401

__all__ = ["run_workflow", "WorkflowResult", "Budget", "BudgetExceeded", "WorkflowEmitter"]

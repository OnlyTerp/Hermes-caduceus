"""Restricted execution of model-authored workflow scripts.

The workflow script only needs to *orchestrate* — all real side effects happen
inside delegated agents (which keep Hermes's normal tool guardrails/approvals).
So the script body runs with a tightly restricted namespace and is AST-validated
before execution:

* no ``import`` / ``from ... import``;
* no dunder attribute access (blocks ``().__class__.__bases__`` sandbox escapes);
* no dangerous builtins (``eval``/``exec``/``open``/``__import__``/...);
* a small allowlist of pure builtins + the ``json`` and ``math`` modules;
* the DSL hooks (agent/parallel/pipeline/phase/log/workflow/budget/args) plus
  quality-pattern helpers, injected by :mod:`agent.workflow.dsl`.

``time``/``random``/wall-clock are intentionally absent — they would break
resume (UltraCode forbids ``Date.now``/``Math.random`` for the same reason).
"""

from __future__ import annotations

import ast
import builtins as _builtins
import json
import math
from typing import Any, Dict, Tuple


class SandboxError(Exception):
    """Raised when a workflow script violates the sandbox policy or fails to parse."""


# Builtins the script may use. Deliberately excludes eval/exec/open/import/etc.
_SAFE_BUILTIN_NAMES = (
    "len", "range", "sorted", "min", "max", "sum", "enumerate", "zip",
    "map", "filter", "dict", "list", "set", "tuple", "str", "int", "float",
    "bool", "abs", "any", "all", "round", "reversed", "isinstance", "repr",
    "format", "print", "frozenset", "bytes", "chr", "ord", "hash", "divmod",
    "pow", "slice", "iter", "next", "hasattr",
    # exceptions for try/except inside scripts
    "Exception", "ValueError", "KeyError", "IndexError", "TypeError",
    "RuntimeError", "StopIteration", "ZeroDivisionError", "ArithmeticError",
    "AttributeError", "NotImplementedError",
)
_SAFE_BUILTINS = {
    name: getattr(_builtins, name)
    for name in _SAFE_BUILTIN_NAMES
    if hasattr(_builtins, name)
}
_SAFE_BUILTINS.update({"True": True, "False": False, "None": None})

# Forbidden builtin/global names the script must never reference.
_FORBIDDEN_NAMES = {
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "exit",
    "quit", "help", "memoryview", "object", "super", "type", "classmethod",
    "staticmethod", "property", "__builtins__", "__loader__", "__spec__",
}


class _Validator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        self.errors.append("imports are not allowed in workflow scripts")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        self.errors.append("imports are not allowed in workflow scripts")

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if isinstance(node.attr, str) and node.attr.startswith("__") and node.attr.endswith("__"):
            self.errors.append(f"dunder attribute access is not allowed: .{node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load) and node.id in _FORBIDDEN_NAMES:
            self.errors.append(f"use of '{node.id}' is not allowed in workflow scripts")
        self.generic_visit(node)


def validate(source: str) -> ast.AST:
    """Parse and AST-validate the script. Raises SandboxError on any violation."""
    try:
        tree = ast.parse(source, filename="<workflow>", mode="exec")
    except SyntaxError as exc:
        raise SandboxError(f"workflow script syntax error: {exc}") from exc
    v = _Validator()
    v.visit(tree)
    if v.errors:
        raise SandboxError("; ".join(dict.fromkeys(v.errors)))
    return tree


def build_globals(dsl: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the restricted global namespace for the script body."""
    g: Dict[str, Any] = {
        "__builtins__": dict(_SAFE_BUILTINS),
        "json": json,
        "math": math,
    }
    g.update(dsl)
    return g


def compile_workflow(source: str, dsl: Dict[str, Any]) -> Tuple[Dict[str, Any], Any]:
    """Validate, compile, and execute the script body to extract ``meta`` + ``main``.

    Returns ``(meta, main)`` where ``main`` is the (un-awaited) async function
    defined by the script. Raises :class:`SandboxError` on policy violation or
    when ``meta``/``main`` are missing.
    """
    tree = validate(source)
    code = compile(tree, filename="<workflow>", mode="exec")
    g = build_globals(dsl)
    try:
        exec(code, g)  # noqa: S102 — restricted namespace, AST-validated above
    except SandboxError:
        raise
    except Exception as exc:
        raise SandboxError(f"workflow script failed to load: {exc}") from exc

    meta = g.get("meta")
    if not isinstance(meta, dict):
        raise SandboxError("workflow script must define a `meta` dict (name, description, phases)")
    if not meta.get("name") or not meta.get("description"):
        raise SandboxError("workflow meta must include `name` and `description`")
    main = g.get("main")
    if main is None or not callable(main):
        raise SandboxError("workflow script must define an async `main()` function")
    return meta, main

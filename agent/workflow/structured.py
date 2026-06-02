"""Structured output for workflow leaves.

When ``agent(..., schema=S)`` is set, the leaf is instructed to return ONLY a
JSON value matching ``S``; the returned text is parsed and validated with
``jsonschema``. On mismatch the runner retries with the validator error fed back
(UltraCode: "validation happens at the tool-call layer so the model retries on
mismatch"). Returns the parsed object — no brittle parsing in the script.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional, Tuple

try:
    import jsonschema
    _HAVE_JSONSCHEMA = True
except Exception:  # pragma: no cover
    jsonschema = None
    _HAVE_JSONSCHEMA = False


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def build_instruction(schema: dict) -> str:
    """Build the system instruction appended to a schema'd leaf prompt."""
    try:
        schema_str = json.dumps(schema, indent=2, ensure_ascii=False)
    except Exception:
        schema_str = str(schema)
    return (
        "\n\n---\nYour entire response MUST be a single JSON value that validates "
        "against this JSON Schema. Output ONLY the JSON — no prose, no markdown "
        "fences, no commentary. Your final text IS the return value.\n\n"
        f"JSON Schema:\n{schema_str}"
    )


def _extract_json(text: str) -> Optional[str]:
    """Best-effort extraction of a JSON value from model text."""
    if not text:
        return None
    text = text.strip()
    # Fenced block first.
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    # Whole string looks like JSON.
    if text[:1] in "{[" or text[:1] in '"' or text[:1].isdigit() or text.startswith("-"):
        return text
    # Find the first balanced {...} or [...] span.
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            c = text[i]
            if c == open_c:
                depth += 1
            elif c == close_c:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def parse_and_validate(text: str, schema: dict) -> Tuple[bool, Any, Optional[str]]:
    """Parse ``text`` as JSON and validate against ``schema``.

    Returns ``(ok, value, error)``. On success ``ok=True`` and ``value`` is the
    parsed object. On failure ``ok=False`` and ``error`` is a short message
    suitable for feeding back to the model on retry.
    """
    raw = _extract_json(text)
    if raw is None:
        return False, None, "No JSON value found in the response. Return ONLY the JSON."
    try:
        value = json.loads(raw)
    except Exception as exc:
        return False, None, f"Response was not valid JSON: {exc}. Return ONLY a valid JSON value."
    if _HAVE_JSONSCHEMA and isinstance(schema, dict):
        try:
            jsonschema.validate(value, schema)
        except jsonschema.ValidationError as exc:  # type: ignore[attr-defined]
            path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            return False, None, f"JSON did not match schema at {path}: {exc.message}"
        except Exception as exc:
            return False, None, f"Schema validation error: {exc}"
    return True, value, None

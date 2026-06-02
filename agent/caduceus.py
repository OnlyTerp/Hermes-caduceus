"""Caduceus — Hermes-native dynamic-workflow mode.

Caduceus is Hermes's native port of Claude Code's "UltraCode": it combines
``xhigh`` reasoning effort, a *standing* opt-in to the Workflow tool, an
orchestrator/worker two-model split, and the "Loom" dynamic-workflow engine
(see :mod:`agent.workflow`).

This module is the single source of truth for:

* the per-session :class:`CaduceusState` (mode flag, tiers, budget, reminder
  bookkeeping);
* the model-visible **prompt stack** — the standing reminder, the
  enter/sparse/exit lifecycle reminders, the workflow-keyword reminder, and the
  full ``Workflow`` tool description (ported verbatim where model-visible,
  adapted only for Python/Hermes tool names);
* the abstract ``effort`` → provider ``reasoning_config`` mapping; and
* orchestrator/worker tier resolution used by ``delegate_task`` and the Loom.

It is intentionally self-contained so Caduceus stays an *additive* fork: hot
files (run_agent, conversation_loop, system_prompt, delegate_tool) touch it only
through small, well-named hooks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Model-visible prompt strings
#
# These are ported from the UltraCode reverse-engineering (claude-ultracode-re)
# and rebranded to "Caduceus". The wording that *drives behavior* is preserved
# verbatim; only the proper noun and JS→Python tool details change.
# ---------------------------------------------------------------------------

# Standing reminder, injected into the system prompt's stable tier while the
# mode is active (mirrors UltraCode's placement right after the skills
# inventory). This is the line that converts mode-on into a standing opt-in.
STANDING_REMINDER = (
    "Caduceus is on: optimize for the most exhaustive, correct answer — not the "
    "fastest or cheapest. Use the Workflow tool on every substantive task; token "
    "cost is not a constraint. See the Workflow tool's **Caduceus** section and "
    "quality patterns. Solo only on conversational/trivial turns."
)

# Lifecycle reminders, injected as per-turn meta notes (cache-friendly: they go
# into the current user message, never the cached system-prompt prefix).
ENTER_REMINDER_FULL = STANDING_REMINDER
ENTER_REMINDER_SPARSE = (
    "Caduceus is still on — use the Workflow tool; see its Caduceus section."
)
SPARSE_REMINDER = ENTER_REMINDER_SPARSE
EXIT_REMINDER = (
    "Caduceus is off — the Workflow tool's standard opt-in rule applies again."
)

# Separate opt-in path: the user literally typed "workflow"/"workflows".
WORKFLOW_KEYWORD_REMINDER = (
    'The user included the keyword "workflow" or "workflows", which means you '
    "should use the Workflow tool to fulfill their request."
)


def wrap_reminder(text: str) -> str:
    """Wrap a reminder as a model-visible meta block."""
    return f"<system-reminder>\n{text}\n</system-reminder>"


# The full ``Workflow`` tool description. Ported from
# claude-ultracode-re/evidence/WORKFLOW_TOOL_PROMPT.md (the captured
# ``Workflow.description``) with these edits: the script body is restricted
# Python (async ``main()``) rather than JavaScript; "subagent" is a Hermes
# ``delegate_task`` child running on the worker tier; tool reachability is via
# Hermes toolsets; and the standing-opt-in clause is rebranded to **Caduceus**.
WORKFLOW_TOOL_DESCRIPTION = """\
Execute a workflow script that orchestrates multiple subagents deterministically. Workflows run in the background — this tool returns immediately with a run id, and a <workflow-notification> tool result arrives when the workflow completes. Watch live progress in the Orchestration Theater (desktop) or with /caduceus.

A workflow structures work across many agents — to be comprehensive (decompose and cover in parallel), to be confident (independent perspectives and adversarial checks before committing), or to take on scale one context can't hold (migrations, audits, broad sweeps). The script is where you encode that structure: what fans out, what verifies, what synthesizes.

ONLY call this tool when the user has explicitly opted into multi-agent orchestration. Workflows can spawn dozens of agents and consume a large amount of tokens; the user must request that scale, not have it inferred. Explicit opt-in means one of:
- The user included the "workflow" or "workflows" keyword (you'll see a system-reminder confirming it).
- Caduceus is on (a system-reminder confirms it) — see **Caduceus** below.
- The user directly asked you to run a workflow or use multi-agent orchestration in their own words ("run a workflow", "fan out agents", "orchestrate this with subagents"). The ask must be in the user's words — a task that would merely benefit from a workflow does not count.
- The user invoked a skill or slash command whose instructions tell you to call Workflow.
- The user asked you to run a specific named or saved workflow.

For any other task — even one that would clearly benefit from parallelism — do NOT call this tool. Use delegate_task for individual subagents, or briefly describe what a multi-agent workflow could do and how much it would roughly cost, and ask the user whether to run it. Mention they can include "workflow" in a future message to skip the ask.

When you do call it, the right move is often **hybrid**: scout inline first (list the files, find the channels, scope the diff) to discover the work-list, then call Workflow to pipeline over it. You don't need to know the shape before the *task* — only before the *orchestration step*.

Common single-phase workflows you can chain across turns:
- **Understand** — parallel readers over relevant subsystems → structured map
- **Design** — judge panel of N independent approaches → scored synthesis
- **Review** — dimensions → find → adversarially verify (example below)
- **Research** — multi-modal sweep → deep-read → synthesize
- **Migrate** — discover sites → transform each (worktree isolation) → verify

For larger work, run several in sequence — read each result before deciding the next phase. You stay in the loop; each workflow is one well-scoped fan-out.

**Caduceus.** When a system-reminder confirms Caduceus is on, that opt-in is standing: author and run a workflow for every substantive task by default. The goal is the most exhaustive, correct answer you can produce — token cost is not a constraint. For multi-phase work (understand → design → implement → review), that often means several workflows in sequence — one per phase — so you stay in the loop between them. The quality patterns below (adversarial verify, multi-modal sweep, completeness critic, loop-until-dry) are the tools; pick what fits the task. Lean toward orchestrating with workflows and adversarially verifying your findings — unless the work is trivial or already verified. Solo only on conversational turns or trivial mechanical edits. When a reminder says Caduceus is off, revert to the opt-in rule above.

Pass the script inline via `script` — do not write it to a file first. Every invocation automatically persists its script to a file under the session directory and returns the path in the tool result. To iterate on a workflow, edit that file and re-invoke Workflow with `{"scriptPath": "<path>"}` instead of resending the full script.

Every script must define `meta` and an async `main()`:

    meta = {
        "name": "find-flaky-tests",
        "description": "Find flaky tests and propose fixes",  # one-line
        "phases": [                                            # one entry per phase() call
            {"title": "Scan", "detail": "grep test logs for retries"},
            {"title": "Fix", "detail": "one agent per flaky test"},
        ],
    }

    async def main():
        phase("Scan")
        flaky = await agent("grep CI logs for retry markers", schema=FLAKY_SCHEMA)
        ...
        return {"flaky": flaky}

The `meta` object must be a PURE LITERAL — no variables, function calls, or interpolation. Required: `name`, `description`. Optional: `whenToUse`, `phases`. Use the SAME phase titles in meta["phases"] as in phase() calls — titles are matched exactly; a phase() call with no matching meta entry just gets its own progress group.

Script body hooks (all injected as globals — do NOT import them):
- agent(prompt: str, *, label=None, phase=None, schema=None, model=None, isolation=None, agent_type=None) -> awaitable — spawn a subagent (a Hermes delegate_task child on the worker tier). Without `schema`, returns its final text as a string. With `schema` (a JSON Schema dict), the subagent is forced to return a validated object via a structured-output tool — no parsing needed. Returns None if the agent fails or is skipped (filter with `[x for x in results if x]`). `label` overrides the display label. `phase` explicitly assigns this agent to a progress group (use this inside pipeline()/parallel() stages to avoid races on the global phase() state). `model` overrides the model for this one call — default to omitting it (the agent inherits the worker tier, which is almost always correct); set it only when you're highly confident a different tier fits, e.g. escalating one hard synthesis to the orchestrator tier. `isolation='worktree'` runs the agent in a fresh git worktree — EXPENSIVE, use ONLY when agents mutate files in parallel and would otherwise conflict. `agent_type` selects a custom subagent type instead of the default.
- pipeline(items, *stages) -> awaitable[list] — run each item through all stages independently, NO barrier between stages. Item A can be in stage 3 while item B is still in stage 1. This is the DEFAULT for multi-stage work. Wall-clock = slowest single-item chain, not sum-of-slowest-per-stage. Every stage callable receives (prev_result, original_item, index). A stage that raises drops that item to None and skips its remaining stages.
- parallel(thunks) -> awaitable[list] — run zero-arg callables concurrently. This is a BARRIER: awaits all before returning. A thunk that raises (or whose agent errors) resolves to None — the call itself never raises, so filter out None before using the results. Use ONLY when you genuinely need all results together.
- log(message: str) -> None — emit a narrator line above the progress tree.
- phase(title: str) -> None — start a new phase; subsequent agent() calls group under it.
- args — the value passed as Workflow's `args` input, verbatim (None if not provided). Pass arrays/objects as actual JSON values, NOT a JSON-encoded string.
- budget — an object with `.total` (int or None), `.spent()` and `.remaining()`. The pool is shared across the main loop and all leaves. `.total` is a HARD ceiling: once `.spent()` reaches `.total`, further agent() calls raise. Use for dynamic loops: `while budget.total and budget.remaining() > 50_000: ...`.
- workflow(name_or_ref, args=None) -> awaitable — run another workflow inline as a sub-step (one level deep; nesting deeper raises). Pass a saved name (str) or {"scriptPath": "..."}. The child shares this run's concurrency cap, agent counter, and token budget.

Subagents are told their final text IS the return value (not a human-facing message), so they return raw data. For structured output, use the schema option — validation happens at the tool-call layer so the model retries on mismatch.

Workflow agents reach the session's tools through Hermes toolsets (the worker inherits the orchestrator's enabled toolsets, intersected for safety).

Scripts are restricted Python: an async `main()` you define is awaited, and the DSL hooks above are injected as globals. No imports, no filesystem or network from the script body, no `eval`/`exec`. Standard pure helpers are available (len, range, sorted, min, max, sum, enumerate, zip, map, filter, dict/list/set/tuple/str/int/float/bool, abs, any, all, round, and the `json` and `math` modules). `time`, `random`, and wall-clock are UNAVAILABLE in the script body — they would break resume; pass timestamps in via `args`, stamp results after the workflow returns, and for randomness vary the agent prompt/label by index.

DEFAULT TO pipeline(). Only reach for a barrier (parallel between stages) when you genuinely need ALL prior-stage results together.

A barrier is correct ONLY when stage N needs cross-item context from all of stage N-1:
- Dedup/merge across the full result set before expensive downstream work
- Early-exit if the total count is zero ("0 bugs found → skip verification entirely")
- Stage N's prompt references "the other findings" for comparison

A barrier is NOT justified by "I need to flatten/map/filter first" (do it inside a pipeline stage), "the stages are conceptually separate" (that's what pipeline models), or "it's cleaner code" (barrier latency is real).

Concurrent agent() calls are capped per workflow (default min(16, cpu-2)) — excess calls queue and run as slots free. You can still pass 100 items to parallel()/pipeline() and they all complete; only ~cap run at any moment. Total agent count across a workflow's lifetime is capped (default 1000) as a runaway backstop.

The canonical multi-stage pattern — pipeline by default, each dimension verifies as soon as its review completes:

    meta = {
        "name": "review-changes",
        "description": "Review changed files across dimensions, verify each finding",
        "phases": [{"title": "Review"}, {"title": "Verify"}],
    }
    DIMENSIONS = [{"key": "bugs", "prompt": "..."}, {"key": "perf", "prompt": "..."}]

    async def main():
        async def review_stage(d):
            return await agent(d["prompt"], label="review:" + d["key"], phase="Review", schema=FINDINGS_SCHEMA)

        async def verify_stage(review, d, i):
            async def verify_one(f):
                v = await agent("Adversarially verify: " + f["title"], phase="Verify", schema=VERDICT_SCHEMA)
                return {**f, "verdict": v}
            return await parallel([(lambda f=f: verify_one(f)) for f in review["findings"]])

        results = await pipeline(DIMENSIONS, review_stage, verify_stage)
        confirmed = [f for r in results if r for f in r if f and f["verdict"]["isReal"]]
        return {"confirmed": confirmed}

Quality patterns — common shapes; pick by task and compose freely:
- Adversarial verify: spawn N independent skeptics per finding, each prompted to REFUTE. Kill if a majority refute. Prevents plausible-but-wrong findings from surviving.
- Perspective-diverse verify: give each verifier a distinct lens (correctness, security, perf, does-it-reproduce) instead of N identical refuters — diversity catches failure modes redundancy can't.
- Judge panel: generate N independent attempts from different angles, score with parallel judges, synthesize from the winner while grafting the best ideas from runners-up.
- Loop-until-dry: for unknown-size discovery (bugs, issues, edge cases), keep spawning finders until K consecutive rounds return nothing new. Dedup vs everything seen so far, not just confirmed.
- Multi-modal sweep: parallel agents each searching a different way (by-container, by-content, by-entity, by-time).
- Completeness critic: a final agent that asks "what's missing — modality not run, claim unverified, source unread?" What it finds becomes the next round of work.
- No silent caps: if a workflow bounds coverage (top-N, no-retry, sampling), log() what was dropped.

Scale to what the user asked for. "find any bugs" → a few finders, single-vote verify. "thoroughly audit this" or "be comprehensive" → larger finder pool, 3–5 vote adversarial pass, synthesis stage. When unsure, lean toward thoroughness for research/review/audit and toward brevity for quick checks. Compose novel harnesses when the task calls for it (tournament brackets, self-repair loops, staged escalation).

Use this tool for multi-step orchestration where control flow should be deterministic (loops, conditionals, fan-out) rather than model-driven.

The tool result includes a runId. To resume after a pause, kill, or script edit, relaunch with `{"scriptPath": ..., "resumeFromRunId": ...}` — the longest unchanged prefix of agent() calls returns cached results instantly; the first edited/new call and everything after it runs live. Same script + same args → 100% cache hit."""


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

_UNSET = object()


@dataclass
class CaduceusState:
    """Per-session Caduceus state, carried on the AIAgent.

    Defaults are the "off" state. :func:`state_from_config` seeds it from the
    ``caduceus:`` config section; the /caduceus command and the desktop toggle
    mutate it at runtime.
    """

    enabled: bool = False
    effort: str = "xhigh"
    apply_effort_to_worker: bool = False
    orchestrator: Dict[str, str] = field(default_factory=lambda: {"provider": "", "model": ""})
    worker: Dict[str, str] = field(default_factory=lambda: {"provider": "", "model": ""})
    budget_tokens: Optional[int] = None

    # Workflow/Loom knobs (copied from config for convenience).
    workflow: Dict[str, Any] = field(default_factory=dict)

    # Reminder lifecycle config + bookkeeping.
    enter_style: str = "full"
    turns_between_maintenance: int = 8
    # Set True when the mode flips on; the next turn emits the enter reminder.
    _enter_pending: bool = False
    # Set True when the mode flips off; the next turn emits the exit reminder.
    _exit_pending: bool = False
    # User-turn index of the last maintenance reminder.
    _last_maintenance_turn: int = 0

    # Saved reasoning_config so exiting the mode restores the prior effort.
    _saved_reasoning_config: Any = _UNSET

    # ---- mutation -----------------------------------------------------
    def activate(self) -> None:
        if not self.enabled:
            self.enabled = True
            self._enter_pending = True
            self._exit_pending = False

    def deactivate(self) -> None:
        if self.enabled:
            self.enabled = False
            self._exit_pending = True
            self._enter_pending = False

    # ---- introspection ------------------------------------------------
    def is_split(self) -> bool:
        """True when a distinct worker tier is configured (orchestrator != worker)."""
        w = self.worker or {}
        return bool((w.get("provider") or "").strip() or (w.get("model") or "").strip())

    def summary(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "effort": self.effort,
            "orchestrator": dict(self.orchestrator),
            "worker": dict(self.worker),
            "budget": self.budget_tokens,
            "split": self.is_split(),
        }


def state_from_config(cfg: Optional[Dict[str, Any]]) -> CaduceusState:
    """Build a :class:`CaduceusState` from the ``caduceus:`` config section."""
    c = dict((cfg or {}).get("caduceus") or {}) if cfg else {}
    orch = dict(c.get("orchestrator") or {})
    work = dict(c.get("worker") or {})
    wf = dict(c.get("workflow") or {})
    reminders = dict(c.get("reminders") or {})
    budget = wf.get("default_budget_tokens")
    try:
        budget = int(budget) if budget not in (None, "", "null") else None
    except (TypeError, ValueError):
        budget = None
    return CaduceusState(
        enabled=bool(c.get("enabled", False)),
        effort=str(c.get("effort") or "xhigh"),
        apply_effort_to_worker=bool(c.get("apply_effort_to_worker", False)),
        orchestrator={"provider": str(orch.get("provider") or ""), "model": str(orch.get("model") or "")},
        worker={"provider": str(work.get("provider") or ""), "model": str(work.get("model") or "")},
        budget_tokens=budget,
        workflow=wf,
        enter_style=str(reminders.get("enter") or "full"),
        turns_between_maintenance=int(reminders.get("turns_between_maintenance") or 8),
    )


def get_state(agent: Any) -> Optional[CaduceusState]:
    """Return the agent's CaduceusState, or None if not initialised."""
    return getattr(agent, "caduceus", None)


def is_active(agent: Any) -> bool:
    st = get_state(agent)
    return bool(st and st.enabled)


# ---------------------------------------------------------------------------
# Effort mapping
# ---------------------------------------------------------------------------

def resolve_effort_config(effort: str) -> Optional[Dict[str, Any]]:
    """Map an abstract Caduceus ``effort`` to a Hermes ``reasoning_config``.

    Hermes already threads ``reasoning_config={"enabled": True, "effort": ...}``
    into every provider transport and maps each effort level to the provider's
    real knob (Anthropic adaptive-thinking + effort, OpenAI/Codex effort,
    others best-effort). ``xhigh`` is a first-class Hermes effort, so this is a
    thin wrapper around :func:`hermes_constants.parse_reasoning_effort` that
    degrades gracefully on unknown values.
    """
    try:
        from hermes_constants import parse_reasoning_effort
    except Exception:
        return None
    parsed = parse_reasoning_effort(effort or "")
    # Unknown effort → don't force anything (keep the session's current config).
    return parsed


# ---------------------------------------------------------------------------
# Reminder lifecycle scheduler
# ---------------------------------------------------------------------------

def compute_turn_reminder(state: Optional[CaduceusState], user_turn_count: int) -> Optional[str]:
    """Return the meta-reminder text to inject for this turn, or None.

    Lifecycle (UltraCode parity):
      * **exit**  — emitted once on the first turn after deactivation;
      * **enter** — emitted once on the first turn after activation
        (full or sparse per ``reminders.enter``);
      * **sparse maintenance** — every ``turns_between_maintenance`` user turns
        while the mode stays on.
    """
    if state is None:
        return None
    # Exit takes priority — it can fire even while disabled.
    if state._exit_pending:
        state._exit_pending = False
        return EXIT_REMINDER
    if not state.enabled:
        return None
    if state._enter_pending:
        state._enter_pending = False
        state._last_maintenance_turn = user_turn_count
        return ENTER_REMINDER_FULL if state.enter_style != "sparse" else ENTER_REMINDER_SPARSE
    cadence = max(1, int(state.turns_between_maintenance or 8))
    if user_turn_count - state._last_maintenance_turn >= cadence:
        state._last_maintenance_turn = user_turn_count
        return SPARSE_REMINDER
    return None


def message_has_workflow_keyword(text: Any) -> bool:
    """True if the user's message contains a standalone 'workflow(s)' keyword."""
    if not isinstance(text, str) or not text:
        return False
    low = text.lower()
    return "workflow" in low  # substring is fine; "workflows" matches too


# ---------------------------------------------------------------------------
# Prompt-stack injection helpers (called from system_prompt / conversation_loop)
# ---------------------------------------------------------------------------

def standing_reminder_for_prompt(agent: Any) -> Optional[str]:
    """Return the standing reminder for the system prompt, or None.

    Injected only when the mode is active AND the Workflow tool is actually
    available to the model (so the reminder never references a missing tool).
    """
    if not is_active(agent):
        return None
    valid = getattr(agent, "valid_tool_names", None) or ()
    if "Workflow" not in valid and "workflow" not in valid:
        return None
    return STANDING_REMINDER


# ---------------------------------------------------------------------------
# Tier resolution (used by delegate_task for role-aware tiering)
# ---------------------------------------------------------------------------

def tier_for_role(state: Optional[CaduceusState], role: str) -> Optional[Dict[str, str]]:
    """Return the {provider, model} tier for a delegate role under Caduceus.

    Role-aware tiering (the "smartest integration"):
      * ``orchestrator`` (a nested role='orchestrator' delegate, which itself
        plans + delegates) → the **orchestrator** (heavy) tier;
      * everything else (leaves + plain delegate_task) → the **worker** (fast)
        tier.

    Returns None when the mode is off or the relevant tier is unset (so the
    child inherits the parent's model unchanged).
    """
    if state is None or not state.enabled:
        return None
    if role == "orchestrator":
        tier = state.orchestrator
    else:
        tier = state.worker
        # Solo: no distinct worker configured → fall back to orchestrator tier
        # if one is set, else inherit parent.
        if not ((tier.get("provider") or "").strip() or (tier.get("model") or "").strip()):
            tier = state.orchestrator
    if not tier:
        return None
    provider = (tier.get("provider") or "").strip()
    model = (tier.get("model") or "").strip()
    if not provider and not model:
        return None
    return {"provider": provider, "model": model}


def resolve_concurrency(state: Optional[CaduceusState]) -> int:
    """Resolve the workflow concurrency cap (handles the 'auto' sentinel)."""
    default = min(16, max(2, (os.cpu_count() or 4) - 2))
    if state is None:
        return default
    raw = (state.workflow or {}).get("max_concurrency", "auto")
    if isinstance(raw, str):
        if raw.strip().lower() == "auto":
            return default
        try:
            raw = int(raw)
        except ValueError:
            return default
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return default

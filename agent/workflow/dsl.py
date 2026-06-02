"""DSL namespace for workflow scripts.

Builds the globals injected into the sandboxed script body: the core hooks
(``agent``/``parallel``/``pipeline``/``phase``/``log``/``workflow``), the
``budget`` object, the verbatim ``args``, and a small standard library of
quality-pattern helpers (adversarial verify, judge panel, loop-until-dry,
multi-modal sweep, completeness critic) that compile to the same primitives but
cut boilerplate and standardize the "exciting" Theater moments.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .budget import Budget
from .scheduler import WorkflowScheduler


_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "refuted": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["refuted"],
}


def build_dsl(scheduler: WorkflowScheduler, budget: Budget, args: Any) -> Dict[str, Any]:
    agent = scheduler.agent
    parallel = scheduler.parallel
    pipeline = scheduler.pipeline

    # ---- quality patterns --------------------------------------------
    async def adversarial_verify(claim: str, n: int = 3, threshold: Optional[int] = None) -> Dict[str, Any]:
        """Spawn N skeptics that try to REFUTE ``claim``; survives if < majority refute."""
        n = max(1, int(n))
        thresh = threshold if threshold is not None else (n // 2 + 1)
        votes = await parallel([
            (lambda i=i: agent(
                f"Try to refute this claim: {claim}\nDefault to refuted=true if uncertain.",
                label=f"refute #{i+1}", phase="Verify", schema=_VERDICT_SCHEMA,
            )) for i in range(n)
        ])
        good = [v for v in votes if v]
        refuted = sum(1 for v in good if v.get("refuted"))
        survives = refuted < thresh
        scheduler.emitter.verify(
            finding_id=claim[:40], result="REAL" if survives else "REJECTED",
            votes=[{"lens": "skeptic", "verdict": ("refute" if v.get("refuted") else "confirm")} for v in good],
        )
        return {"survives": survives, "refuted": refuted, "votes": good, "claim": claim}

    async def perspective_verify(claim: str, lenses: List[str], threshold: Optional[int] = None) -> Dict[str, Any]:
        """Verify ``claim`` through N distinct lenses (correctness, security, ...)."""
        lenses = list(lenses)
        thresh = threshold if threshold is not None else (len(lenses) // 2 + 1)
        votes = await parallel([
            (lambda lens=lens: agent(
                f'Judge this via the {lens} lens — is it real/correct? {claim}',
                label=f"verify:{lens}", phase="Verify", schema=_VERDICT_SCHEMA,
            )) for lens in lenses
        ])
        good = [v for v in votes if v]
        confirmed = sum(1 for v in good if not v.get("refuted"))
        survives = confirmed >= thresh
        scheduler.emitter.verify(
            finding_id=claim[:40], result="REAL" if survives else "REJECTED",
            votes=[{"lens": lens, "verdict": ("confirm" if (v and not v.get("refuted")) else "refute")}
                   for lens, v in zip(lenses, votes)],
        )
        return {"survives": survives, "confirmed": confirmed, "votes": good, "claim": claim}

    async def judge_panel(attempts: List[str], criteria: str = "overall quality") -> Dict[str, Any]:
        """Score each attempt with an independent judge; return the ranked winner."""
        score_schema = {
            "type": "object",
            "properties": {"score": {"type": "number"}, "rationale": {"type": "string"}},
            "required": ["score"],
        }
        scored = await parallel([
            (lambda a=a, i=i: agent(
                f"Score this candidate on {criteria} (0-10):\n\n{a}",
                label=f"judge #{i+1}", phase="Judge", schema=score_schema,
            )) for i, a in enumerate(attempts)
        ])
        ranked = sorted(
            [{"attempt": attempts[i], "score": (s or {}).get("score", 0), "rationale": (s or {}).get("rationale", "")}
             for i, s in enumerate(scored)],
            key=lambda r: r["score"], reverse=True,
        )
        return {"winner": ranked[0] if ranked else None, "ranked": ranked}

    async def loop_until_dry(finder: Callable[[int], Any], k: int = 2, key: Optional[Callable] = None) -> List[Any]:
        """Run ``finder(round)`` until K consecutive rounds surface nothing new."""
        seen = set()
        out: List[Any] = []
        dry = 0
        rnd = 0
        keyfn = key or (lambda x: str(x))
        while dry < max(1, int(k)):
            if budget.total and budget.remaining() <= 0:
                break
            found = await _maybe_await_dsl(finder(rnd))
            rnd += 1
            items = found if isinstance(found, list) else ([found] if found else [])
            fresh = [it for it in items if it is not None and keyfn(it) not in seen]
            if not fresh:
                dry += 1
                scheduler.log(f"round {rnd}: dry ({dry}/{k})")
                continue
            dry = 0
            for it in fresh:
                seen.add(keyfn(it))
            out.extend(fresh)
            scheduler.log(f"round {rnd}: +{len(fresh)} new (total {len(out)})")
        return out

    async def multimodal_sweep(searches: List[str], phase: str = "Sweep") -> List[Any]:
        """Parallel agents, each searching a different way; blind to each other."""
        return await parallel([
            (lambda s=s, i=i: agent(s, label=f"sweep #{i+1}", phase=phase))
            for i, s in enumerate(searches)
        ])

    async def completeness_critic(state_summary: str) -> Any:
        """A final critic that asks what's missing — its answer is the next round's work."""
        return await agent(
            "Review this work state and list what's missing — a modality not run, "
            "a claim unverified, a source unread. Be specific and actionable.\n\n"
            f"{state_summary}",
            label="completeness critic", phase="Critique",
        )

    ns: Dict[str, Any] = {
        # core hooks
        "agent": agent,
        "parallel": parallel,
        "pipeline": pipeline,
        "phase": scheduler.phase,
        "log": scheduler.log,
        "workflow": scheduler.workflow,
        "budget": budget,
        "args": args,
        # quality-pattern stdlib
        "adversarial_verify": adversarial_verify,
        "perspective_verify": perspective_verify,
        "judge_panel": judge_panel,
        "loop_until_dry": loop_until_dry,
        "multimodal_sweep": multimodal_sweep,
        "completeness_critic": completeness_critic,
        # small pure helper the canonical examples use
        "flatten": _flatten,
    }
    return ns


async def _maybe_await_dsl(x: Any) -> Any:
    import inspect
    if inspect.isawaitable(x):
        return await x
    return x


def _flatten(seq: Any) -> List[Any]:
    out: List[Any] = []
    for x in seq or []:
        if isinstance(x, (list, tuple)):
            out.extend(x)
        else:
            out.append(x)
    return out

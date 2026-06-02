# Caduceus — Implementation Status

Native port of Claude Code's **UltraCode** into the Hermes desktop app, shipped
as a core fork of `NousResearch/hermes-agent` v0.15.1, installed at
`C:\Users\User\AppData\Local\hermes\hermes-agent\` (branch `caduceus`).

**Status: shipped & verified.** Python backend edits are live on the next
backend start; the desktop renderer was rebuilt and the running app's
`app.asar` was repacked with the new bundle (original backed up to
`apps/desktop/release/win-unpacked/resources/app.asar.precaduceus.bak`).

---

## What Caduceus does (parity with UltraCode, native to Hermes)

1. **The envelope** — `/caduceus on` sets the orchestrator to `xhigh` reasoning
   effort (Hermes already maps `xhigh` per provider) and injects the standing
   "Caduceus is on…" reminder into the system prompt + a per-turn
   enter/sparse/exit reminder lifecycle.
2. **The Loom** — a `Workflow` tool backed by `agent/workflow/`: a sandboxed
   Python script (`async main()`) that orchestrates many delegate subagents via
   `agent() / parallel() / pipeline() / phase() / log() / workflow() / budget`
   with structured-output schemas, a shared token budget (hard ceiling),
   resume/caching, and a quality-pattern stdlib (`adversarial_verify`,
   `judge_panel`, `loop_until_dry`, `multimodal_sweep`, `completeness_critic`,
   `perspective_verify`).
3. **Orchestrator/Worker tiering** — role-aware: the main loop + nested
   `role='orchestrator'` delegates use the heavy tier; workflow leaves + plain
   `delegate_task` use the fast worker tier. Per-call `model=` overrides win.
4. **Orchestration Theater** — a live desktop UI (phase lanes, agent cards with
   model badges + streaming tails, verify duels, budget burn-down, concurrency
   meter) driven by `workflow.*` WebSocket events.

## How to use

* **CLI / desktop terminal:** `/caduceus on` (aliases `/cad`, `/uc`),
  `/caduceus orch <provider:model>`, `/caduceus worker <provider:model>`,
  `/caduceus solo`, `/caduceus budget <tokens>`, `/caduceus status`.
* **Desktop:** click the **⚕ Caduceus** chip in the statusbar to toggle; when a
  workflow runs, a mini-strip appears (click to open the full Theater). The
  Theater header has a **tiers** button (two-slot orchestrator/worker picker).
  Settings ▸ **Caduceus** edits defaults.
* Once on, ask for substantive work; the model authors and runs `Workflow`
  scripts. Saved workflows live in `~/.hermes/workflows/saved/<name>.py`.

## Files

New backend: `agent/caduceus.py`, `agent/workflow/{__init__,engine,scheduler,
runner,dsl,sandbox,structured,journal,reliability,events,budget}.py`,
`tools/workflow_tool.py`, `tests/workflow/test_loom_offline.py`.
New desktop: `src/store/{caduceus,workflow}.ts`,
`src/components/OrchestratorWorkerPicker.tsx`,
`src/components/workflow/{WorkflowTheater,AgentCard,TheaterPanels,theater-format}`.
Modified (additive hooks): `hermes_cli/{config,commands}.py`, `cli.py`,
`gateway/run.py`, `tui_gateway/server.py`, `agent/{agent_init,system_prompt,
conversation_loop}.py`, `tools/delegate_tool.py`, `toolsets.py`, and the desktop
`use-message-stream.ts`, `app-shell.tsx`, `use-statusbar-items.tsx`,
`settings/constants.ts`.

## Verification

* `tests/workflow/test_loom_offline.py` — 11 tests (pipeline no-barrier,
  parallel barrier + None-on-throw, adversarial verify, budget ceiling,
  max-agents backstop, sandbox blocks import/eval/dunder/open, structured-output
  retry, resume cache hit). Green.
* No regressions in delegate (151), command-registry (144), toolset/registry
  (73), and system-prompt/prompt-builder (142) suites.
* Desktop `npm run type-check` and `npm run build` clean; asar repacked.
* End-to-end tool-handler smoke (mocked leaf) confirms the full
  tool → engine → events → callback-bridge path.

## Notes / caveats

* `/caduceus` is `cli_only` (CLI + desktop terminal + desktop chip), not a
  messaging-platform slash — heavy multi-agent orchestration over a chat slash
  isn't a primary use case, and it keeps Slack under its 50-command cap. The
  gateway still honors `caduceus.enabled` from config per message.
* The install tree shows a pre-existing global CRLF-vs-LF git churn (the
  committed blobs are LF; the installed files are CRLF). Real edits were NOT
  committed to avoid burying them in 4.5k line-ending diffs; instead the exact
  changed/new files are backed up under `patches/install-tree/`.
* To revert the desktop UI: restore `app.asar.precaduceus.bak`. To revert the
  backend: `git checkout` the listed files (on the `caduceus` branch).

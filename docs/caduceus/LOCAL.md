# `/local` — local GPU workers for Caduceus workflows

`/local` runs Caduceus workflow **workers** on models served locally on your
machine's GPU (llama.cpp, or any OpenAI-compatible server). The **orchestrator**
(the planning "brain") always stays on your normal session/cloud model — only the
fan-out workers go local.

A single GPU is an exclusive, VRAM-bounded resource, so Caduceus manages the
model lifecycle for you:

- **Hot-swap on demand** — a model is loaded the first time a worker needs it and
  swapped out when a different model is needed. Swaps are **serialized** (never
  two models resident at once, never a half-loaded state).
- **Capacity-aware parallelism** — fan-out is capped to the loaded model's
  **serving slots** (parallel request slots), not the workflow's nominal
  concurrency.
- **Batch by model** — same-model leaves run together so the GPU swaps as little
  as possible.

> `/local` is off by default. With no models declared it's a no-op. It only
> affects **workflow workers** — your normal chat, the orchestrator, and plain
> `delegate_task` are untouched.

---

## Quick start

1. Make sure your local server(s) are reachable on an OpenAI-compatible endpoint
   (e.g. `llama-server` exposes `/v1/chat/completions` and `/health`).
2. Declare your models under `caduceus.local` in `~/.hermes/config.yaml` (see the
   schema + example below), then **restart** Hermes (config is read at startup).
3. Turn it on:
   ```
   /caduceus on      # workflows live under Caduceus
   /local on         # workers run on your GPU
   /local status     # see the catalog + what's loaded
   ```
4. Say **"workflow"** and the orchestrator fans out — workers run on your GPU,
   hot-swapping and parallelizing within each model's slots.

The orchestrator is told the catalog automatically, so it can tag a leaf with
`model="local:<id>"` to pick a specific local model (or omit it to use the
default worker).

---

## The serving-profile model (important)

On one GPU you don't run *N copies* of a model — you run **one instance with N
parallel slots**, and the KV cache is split across them. So a model has several
**profiles**, each a `(slots × context-per-slot)` trade-off. Switching profiles
is a reload.

Declare each profile you can serve. Caduceus picks the **widest** profile (most
slots) whose per-slot context covers the leaf's need, and reloads only when it
has to. The load hook receives the chosen profile via env vars
(`$LOCAL_PROFILE_PICKER`, `$LOCAL_PROFILE_SLOTS`, `$LOCAL_PROFILE_CTX`) so your
script can serve it.

---

## Config schema (`caduceus.local`)

```yaml
caduceus:
  local:
    enabled: false              # /local on|off (persisted)
    default_worker: ""          # model id used for untagged leaves (empty = first)
    unload_on_off: true         # free VRAM when /local turns off
    fallback_to_cloud: false    # on local load/health failure, fail the leaf (false)
    load_timeout_seconds: 180   # max wait for a model's health after its load hook
    health_poll_seconds: 2
    models:
      - id: my-model            # short id you reference as local:my-model
        endpoint: http://HOST:PORT/v1     # OpenAI-compatible base_url
        served_model_name: the-name-the-server-expects
        api_key: local          # placeholder if the server needs none
        api_mode: chat_completions
        provider: custom
        group: gpu              # models sharing a group can't be co-resident
        vram_mb: 23000          # informational
        max_context: 262144
        card: "one-line capability description (shown to the orchestrator)"
        cost: 0.0
        reasoning_split: false  # true if the server splits reasoning_content
        health: http://HOST:PORT/health   # else {endpoint}/models is probed
        load:   "your-load-command"       # run to load (honor $LOCAL_PROFILE_PICKER)
        unload: "your-unload-command"
        status: "your-status-command"     # optional
        profiles:
          - {slots: 4, ctx: 32768,  picker: "4x32k", default: true}
          - {slots: 2, ctx: 131072, picker: "2x131k"}
          - {slots: 1, ctx: 262144, picker: "256k"}
```

| Field | Meaning |
|---|---|
| `id` | Short handle; reference a model as `local:<id>` in `agent(model=...)`. |
| `endpoint` | OpenAI-compatible base URL (usually ends in `/v1`). |
| `served_model_name` | The `model` string the server expects on the wire. |
| `group` | Exclusivity group — any two models with the same group can't be loaded together (one GPU ⇒ one group). |
| `card` | One-line capability summary; shown to the orchestrator so it routes the right work to the right model. |
| `health` | URL polled after a load hook until it returns 2xx; falls back to `{endpoint}/models`. |
| `load` / `unload` | Shell commands. `load` is run with `$LOCAL_PROFILE_PICKER/_SLOTS/_CTX` set so it can serve the requested profile. |
| `profiles` | The `(slots, ctx)` serving configs. `slots` = parallel requests; `ctx` = context per slot. Mark one `default`. |

---

## Worked example — RTX 5090, Qwen + Gemma (WSL2 → Windows)

Two GPU-exclusive models on a 32 GB card (only one resident at a time), reached
across WSL2's NAT at the Windows host. Qwen is served behind a worker-proxy;
Gemma directly. Swaps use the `aeon_swap_*` hooks (each hook's stop tears down
the other server, so the exclusivity group is honored on both sides).

```yaml
caduceus:
  local:
    enabled: true
    default_worker: qwen-35b
    models:
      - id: qwen-35b
        endpoint: http://172.31.176.1:8004/v1        # worker-proxy in front of :8003
        served_model_name: qwen-35b-local
        group: gpu0
        vram_mb: 23000
        max_context: 262144
        card: "Qwen3 35B (A3B, MXFP4) — fast general worker; solid coding/summarize/extract."
        health: http://172.31.176.1:8004/health
        load:   "aeon_swap_llamacpp.sh start"
        unload: "aeon_swap_llamacpp.sh stop"
        status: "aeon_swap_llamacpp.sh status"
        profiles:
          - {slots: 4, ctx: 32768,  picker: "4x32k", default: true}
          - {slots: 4, ctx: 65536,  picker: "4x64k"}
          - {slots: 3, ctx: 87000,  picker: "3x87k"}
          - {slots: 2, ctx: 131072, picker: "2x131k"}
          - {slots: 1, ctx: 262144, picker: "256k-solo"}
      - id: gemma-31b
        endpoint: http://172.31.176.1:8083/v1
        served_model_name: gemma-31b-local
        group: gpu0
        vram_mb: 25000
        max_context: 262144
        reasoning_split: true
        card: "Gemma 4 31B Opus-distill — reasoning model (splits thinking vs answer); best for hard analysis."
        health: http://172.31.176.1:8083/health
        load:   "AEON_SWAP_VIA_SWAP_MODEL=1 aeon_swap_gemma.sh start"
        unload: "AEON_SWAP_VIA_SWAP_MODEL=1 aeon_swap_gemma.sh stop"
        status: "AEON_SWAP_VIA_SWAP_MODEL=1 aeon_swap_gemma.sh status"
        profiles:
          - {slots: 1, ctx: 262144, picker: "gemma-262k", default: true}
          - {slots: 1, ctx: 65536,  picker: "gemma-safe"}
```

> **Multiple profiles need a profile-aware load hook.** The `aeon_swap_*` scripts
> currently load one fixed config. To use more than the default profile, have the
> hook read `$LOCAL_PROFILE_PICKER` (or `$LOCAL_PROFILE_SLOTS` / `$LOCAL_PROFILE_CTX`)
> and pass the matching `--parallel` / `--ctx-size` to `llama-server`. If the hook
> ignores them, the default profile always loads — everything still works, just
> without on-the-fly profile switching.

With this in place, an orchestrator authoring a workflow under `/local` might write:

```python
async def main():
    # research fans out 4-wide on Qwen (its 4x32k profile)
    facts = await parallel([
        lambda: agent(f"Extract key facts from {src}", model="local:qwen-35b")
        for src in sources
    ])
    # the hard synthesis goes to the Gemma reasoner (one swap, one slot)
    return await agent("Synthesize a rigorous brief from: " + join(facts),
                       model="local:gemma-31b")
```

Caduceus runs all four Qwen leaves on the loaded `4x32k` profile (4 parallel
slots, no reload), then drains and hot-swaps once to Gemma for the synthesis.

---

## How it works (internals)

- **`agent/local_manager.py`** — parses the manifest and owns the GPU state
  machine: `ensure(model, want_ctx)` selects a profile, unloads any
  exclusivity-group conflict, runs the load hook, and polls health — all under a
  lock so swaps serialize.
- **`agent/workflow/local_gate.py`** — an asyncio admission gate enforcing
  model+profile **affinity**: leaves of the loaded `(model, profile)` run up to
  its slot count; a leaf needing a different one waits for the in-flight batch to
  drain, then triggers exactly one swap.
- **`tools/delegate_tool.py`** — `run_workflow_leaf(creds_override=…)` points the
  worker child at the local endpoint, bypassing tier/router resolution.

The orchestrator never goes through the gate — it stays on your session model.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/local status` shows "No local models declared" | Add entries under `caduceus.local.models` and **restart** Hermes (config is read at startup). |
| Leaf fails with "did not become healthy" | Check the model's `health` URL is reachable from where Hermes runs (WSL vs Windows host IP), and that the load hook actually starts the server. Raise `load_timeout_seconds` for big models. |
| All workers run on one model despite tags | Tags must be `model="local:<id>"` with an id from your manifest; an unknown id falls back to `default_worker`. |
| Want a worker on the cloud while `/local` is on | Tag it with a real cloud `provider:model` (e.g. `model="openrouter:…"`) — explicitly-named cloud models escape the gate. |
| Swaps thrash between models | Group same-model leaves together in your workflow (keep each `parallel()` within one model). |

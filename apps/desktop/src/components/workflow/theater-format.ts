import type { WorkflowAgentStatus } from '@/store/workflow'

export function formatTokens(n: number): string {
  if (!n) {
    return '0'
  }
  if (n >= 1000) {
    return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k`
  }
  return String(n)
}

export function formatElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(s / 60)
  const r = s % 60
  return m > 0 ? `${m}:${String(r).padStart(2, '0')}` : `${r}s`
}

// One restrained palette built on the app's design tokens: `primary` is the
// single "active" accent, success is a muted emerald, failure is `destructive`,
// and everything idle is neutral (`border`/`muted-foreground`). No competing
// amber/sky/violet, no gradients — the Theater should read like the rest of the
// app, not a toy.

/** Left status bar accent for an agent card. */
export function statusBar(status: WorkflowAgentStatus): string {
  switch (status) {
    case 'running':
    case 'streaming':
      return 'bg-primary'
    case 'done':
      return 'bg-emerald-500'
    case 'failed':
      return 'bg-destructive'
    case 'skipped':
      return 'bg-border'
    default: // queued
      return 'bg-muted-foreground/30'
  }
}

/** Card border/emphasis for an agent's status. */
export function statusRing(status: WorkflowAgentStatus): string {
  switch (status) {
    case 'running':
    case 'streaming':
      return 'border-primary/40'
    case 'failed':
      return 'border-destructive/40'
    case 'done':
    case 'queued':
    case 'skipped':
    default:
      return 'border-border'
  }
}

/** Small status dot. */
export function statusDot(status: WorkflowAgentStatus): string {
  switch (status) {
    case 'running':
    case 'streaming':
      return 'bg-primary animate-pulse'
    case 'done':
      return 'bg-emerald-500'
    case 'failed':
      return 'bg-destructive'
    case 'skipped':
      return 'bg-muted-foreground/30'
    default: // queued
      return 'bg-muted-foreground/40'
  }
}

/** Whether a model id looks like the heavy (orchestrator) tier. */
export function tierAccent(model: null | string, orchestratorModel: string): 'orchestrator' | 'worker' {
  if (model && orchestratorModel && model === orchestratorModel) {
    return 'orchestrator'
  }
  return 'worker'
}

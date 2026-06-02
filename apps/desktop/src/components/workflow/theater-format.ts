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

/** Tailwind classes for an agent status ring/badge. */
export function statusRing(status: WorkflowAgentStatus): string {
  switch (status) {
    case 'queued':
      return 'border-(--ui-stroke-tertiary) text-muted-foreground'
    case 'running':
      return 'border-sky-400/70 text-sky-300'
    case 'streaming':
      return 'border-sky-400 text-sky-200 shadow-[0_0_14px_-2px_var(--tw-shadow-color)] shadow-sky-500/40'
    case 'done':
      return 'border-emerald-400/70 text-emerald-300'
    case 'failed':
      return 'border-rose-500/70 text-rose-300'
    case 'skipped':
      return 'border-(--ui-stroke-tertiary) text-muted-foreground/60'
    default:
      return 'border-(--ui-stroke-tertiary) text-muted-foreground'
  }
}

export function statusDot(status: WorkflowAgentStatus): string {
  switch (status) {
    case 'queued':
      return 'bg-muted-foreground/50'
    case 'running':
      return 'bg-sky-400 animate-pulse'
    case 'streaming':
      return 'bg-sky-300 animate-pulse'
    case 'done':
      return 'bg-emerald-400'
    case 'failed':
      return 'bg-rose-500'
    case 'skipped':
      return 'bg-muted-foreground/30'
    default:
      return 'bg-muted-foreground/50'
  }
}

/** Whether a model id looks like the heavy (orchestrator) tier. */
export function tierAccent(model: null | string, orchestratorModel: string): 'orchestrator' | 'worker' {
  if (model && orchestratorModel && model === orchestratorModel) {
    return 'orchestrator'
  }
  return 'worker'
}

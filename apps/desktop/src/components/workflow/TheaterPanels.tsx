import { Activity, Check, X } from '@/lib/icons'
import { cn } from '@/lib/utils'
import type { WorkflowRun } from '@/store/workflow'

import { AgentCard } from './AgentCard'
import { formatTokens } from './theater-format'

export function BudgetGauge({ spent, total }: { spent: number; total: null | number }) {
  const pct = total ? Math.min(100, Math.round((spent / total) * 100)) : 0
  const near = pct >= 90
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1.5">
      <div className="flex items-center justify-between text-[0.7rem] text-muted-foreground">
        <span>Budget</span>
        <span className="tabular-nums">
          {formatTokens(spent)}
          {total ? ` / ${formatTokens(total)} · ${pct}%` : ' tok · unbounded'}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className={cn('h-full rounded-full transition-all duration-500', near ? 'bg-destructive' : 'bg-primary',
            !total && 'bg-primary/40')}
          style={{ width: total ? `${pct}%` : '100%' }}
        />
      </div>
    </div>
  )
}

export function ConcurrencyMeter({ active, cap, queued }: { active: number; cap: number; queued: number }) {
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1.5">
      <div className="flex items-center justify-between text-[0.7rem] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <Activity className="size-3" /> Concurrency
        </span>
        <span className="tabular-nums">
          {active}/{cap} active{queued > 0 ? ` · ${queued} queued` : ''}
        </span>
      </div>
      <div className="flex h-1.5 gap-px overflow-hidden rounded-full">
        {Array.from({ length: Math.max(1, cap) }).map((_, i) => (
          <div
            key={i}
            className={cn('h-full flex-1 transition-colors duration-300', i < active ? 'bg-primary' : 'bg-muted')}
          />
        ))}
      </div>
    </div>
  )
}

export function Narrator({ lines }: { lines: WorkflowRun['narrator'] }) {
  const last = lines.at(-1)
  if (!last) {
    return null
  }
  return (
    <div className="flex items-center gap-2 truncate text-xs text-muted-foreground">
      <span className="inline-block size-1.5 shrink-0 rounded-full bg-primary motion-safe:animate-pulse" />
      <span key={last.at} className="truncate motion-safe:animate-in motion-safe:fade-in">
        {last.msg}
      </span>
    </div>
  )
}

export function VerifyFeed({ verifies }: { verifies: WorkflowRun['verifies'] }) {
  if (!verifies.length) {
    return null
  }
  const recent = verifies.slice(-8).reverse()
  return (
    <div className="flex flex-col gap-2">
      <div className="text-[0.7rem] font-medium uppercase tracking-wide text-muted-foreground">Verification</div>
      <div className="flex flex-col gap-1.5">
        {recent.map(v => {
          const confirm = v.votes.filter(x => x.verdict === 'confirm').length
          const refute = v.votes.filter(x => x.verdict === 'refute').length
          const real = v.result === 'REAL'
          return (
            <div
              key={`${v.id}-${v.at}`}
              className="flex items-center gap-2 rounded-lg border border-border bg-card px-2.5 py-1.5 text-[0.7rem] motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-right-2"
            >
              <span className="min-w-0 flex-1 truncate text-foreground" title={v.id}>
                {v.id}
              </span>
              <span className="shrink-0 tabular-nums text-muted-foreground">
                {confirm}·{refute}
              </span>
              <span
                className={cn(
                  'inline-flex shrink-0 items-center gap-0.5 rounded-full px-1.5 py-px text-[0.6rem] font-medium',
                  real ? 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400'
                       : 'bg-destructive/12 text-destructive'
                )}
              >
                {real ? <Check className="size-2.5" /> : <X className="size-2.5" />}
                {real ? 'real' : 'rejected'}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

interface PhaseLanesProps {
  run: WorkflowRun
  orchestratorModel: string
}

export function PhaseLanes({ orchestratorModel, run }: PhaseLanesProps) {
  // Group agents by phase, preserving spawn order. Unphased agents go last.
  const groups = new Map<string, string[]>()
  const UNPHASED = '·'
  for (const id of run.order) {
    const a = run.agents[id]
    if (!a) continue
    const key = a.phase || UNPHASED
    const arr = groups.get(key) ?? []
    arr.push(id)
    groups.set(key, arr)
  }

  // Lane order: declared meta phases first, then any extra phases, then unphased.
  const laneTitles: string[] = []
  for (const ph of run.phases) {
    if (!laneTitles.includes(ph.title)) laneTitles.push(ph.title)
  }
  for (const key of groups.keys()) {
    if (key !== UNPHASED && !laneTitles.includes(key)) laneTitles.push(key)
  }
  if (groups.has(UNPHASED)) laneTitles.push(UNPHASED)
  const lanes = laneTitles.filter(t => groups.has(t))

  if (!lanes.length) {
    return (
      <div className="flex h-32 items-center justify-center text-xs text-muted-foreground">
        Weaving the workflow…
      </div>
    )
  }

  return (
    <div className="flex h-full gap-4 overflow-x-auto pb-2">
      {lanes.map(title => {
        const ids = groups.get(title) ?? []
        const done = ids.filter(id => {
          const s = run.agents[id]?.status
          return s === 'done' || s === 'failed' || s === 'skipped'
        }).length
        const pct = ids.length ? Math.round((done / ids.length) * 100) : 0
        return (
          <div key={title} className="flex w-64 shrink-0 flex-col gap-2.5">
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between">
                <span className="truncate text-xs font-semibold text-foreground">
                  {title === UNPHASED ? 'Agents' : title}
                </span>
                <span className="shrink-0 text-[0.7rem] tabular-nums text-muted-foreground">
                  {done}/{ids.length}
                </span>
              </div>
              <div className="h-0.5 overflow-hidden rounded-full bg-muted">
                <div className="h-full rounded-full bg-primary/70 transition-all duration-500" style={{ width: `${pct}%` }} />
              </div>
            </div>
            <div className="flex flex-col gap-2 overflow-y-auto pr-1">
              {ids.map(id => (
                <AgentCard agent={run.agents[id]!} key={id} orchestratorModel={orchestratorModel} />
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}

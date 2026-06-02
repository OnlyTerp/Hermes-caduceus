import { Activity, Check, X } from '@/lib/icons'
import { cn } from '@/lib/utils'
import type { WorkflowRun } from '@/store/workflow'

import { AgentCard } from './AgentCard'
import { formatTokens } from './theater-format'

export function BudgetGauge({ spent, total }: { spent: number; total: null | number }) {
  const pct = total ? Math.min(100, Math.round((spent / total) * 100)) : 0
  const tone = pct >= 90 ? 'bg-rose-500' : pct >= 70 ? 'bg-amber-400' : 'bg-emerald-400'
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1">
      <div className="flex items-center justify-between text-[0.6rem] text-muted-foreground">
        <span>Budget</span>
        <span className="tabular-nums">
          {formatTokens(spent)}
          {total ? ` / ${formatTokens(total)} (${pct}%)` : ' tok · unbounded'}
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted-foreground/15">
        <div
          className={cn('h-full rounded-full transition-all duration-500', total ? tone : 'bg-sky-400/60')}
          style={{ width: total ? `${pct}%` : '100%' }}
        />
      </div>
    </div>
  )
}

export function ConcurrencyMeter({ active, cap, queued }: { active: number; cap: number; queued: number }) {
  const pct = cap ? Math.min(100, Math.round((active / cap) * 100)) : 0
  return (
    <div className="flex min-w-0 flex-1 flex-col gap-1">
      <div className="flex items-center justify-between text-[0.6rem] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <Activity className="size-2.5" /> Concurrency
        </span>
        <span className="tabular-nums">
          {active}/{cap} active{queued > 0 ? ` · ${queued} queued` : ''}
        </span>
      </div>
      <div className="flex h-1.5 gap-px overflow-hidden rounded-full">
        {Array.from({ length: Math.max(1, cap) }).map((_, i) => (
          <div
            key={i}
            className={cn(
              'h-full flex-1 transition-colors duration-300',
              i < active ? 'bg-sky-400' : 'bg-muted-foreground/15'
            )}
          />
        ))}
      </div>
      {/* keep pct referenced for a11y/title without extra UI */}
      <span className="sr-only">{pct}% utilized</span>
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
      <span className="inline-block size-1.5 shrink-0 rounded-full bg-sky-400 motion-safe:animate-pulse" />
      <span key={last.at} className="truncate italic motion-safe:animate-in motion-safe:fade-in">
        {last.msg}
      </span>
    </div>
  )
}

export function VerifyFeed({ verifies }: { verifies: WorkflowRun['verifies'] }) {
  if (!verifies.length) {
    return null
  }
  const recent = verifies.slice(-6).reverse()
  return (
    <div className="flex flex-col gap-1">
      <div className="text-[0.6rem] uppercase tracking-wide text-muted-foreground/70">Verify duels</div>
      {recent.map(v => {
        const confirm = v.votes.filter(x => x.verdict === 'confirm').length
        const refute = v.votes.filter(x => x.verdict === 'refute').length
        const real = v.result === 'REAL'
        return (
          <div
            key={`${v.id}-${v.at}`}
            className="flex items-center gap-2 rounded-md border border-(--ui-stroke-tertiary) bg-(--ui-chat-bubble-background)/50 px-2 py-1 text-[0.65rem] motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-right-2"
          >
            <div className="flex gap-0.5">
              {v.votes.map((vote, i) => (
                <span
                  key={i}
                  className={cn(
                    'size-2 rounded-full',
                    vote.verdict === 'confirm' ? 'bg-emerald-400' : 'bg-rose-400'
                  )}
                  title={vote.lens}
                />
              ))}
            </div>
            <span className="min-w-0 flex-1 truncate text-muted-foreground" title={v.id}>
              {v.id}
            </span>
            <span className="tabular-nums text-muted-foreground/70">
              {confirm}✓ {refute}✗
            </span>
            <span
              className={cn(
                'inline-flex items-center gap-0.5 rounded px-1.5 py-px text-[0.55rem] font-semibold uppercase',
                real ? 'bg-emerald-400/15 text-emerald-300' : 'bg-rose-500/15 text-rose-300'
              )}
            >
              {real ? <Check className="size-2.5" /> : <X className="size-2.5" />}
              {real ? 'real' : 'rejected'}
            </span>
          </div>
        )
      })}
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
    <div className="flex h-full gap-3 overflow-x-auto pb-2">
      {lanes.map(title => {
        const ids = groups.get(title) ?? []
        const done = ids.filter(id => {
          const s = run.agents[id]?.status
          return s === 'done' || s === 'failed' || s === 'skipped'
        }).length
        return (
          <div key={title} className="flex w-60 shrink-0 flex-col gap-2">
            <div className="flex items-center justify-between border-b border-(--ui-stroke-tertiary) pb-1">
              <span className="truncate text-[0.65rem] font-semibold uppercase tracking-wide text-foreground/80">
                {title === UNPHASED ? 'Agents' : title}
              </span>
              <span className="shrink-0 text-[0.6rem] tabular-nums text-muted-foreground">
                {done}/{ids.length}
              </span>
            </div>
            <div className="flex flex-col gap-1.5 overflow-y-auto pr-1">
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

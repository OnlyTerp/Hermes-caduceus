import { Brain, Check, Cpu, Loader2, Wrench, X, Zap } from '@/lib/icons'
import { cn } from '@/lib/utils'
import type { WorkflowAgent } from '@/store/workflow'

import { formatElapsed, formatTokens, statusBar, statusDot, statusRing } from './theater-format'

interface AgentCardProps {
  agent: WorkflowAgent
  orchestratorModel: string
}

const KIND_LABEL: Record<string, string> = { reasoning: 'thinking', text: 'writing', tool: 'tool' }

export function AgentCard({ agent, orchestratorModel }: AgentCardProps) {
  const isOrch = !!agent.model && !!orchestratorModel && agent.model === orchestratorModel
  const active = agent.status === 'running' || agent.status === 'streaming'
  const elapsed = agent.ms ? formatElapsed(agent.ms) : formatElapsed(agent.updatedAt - agent.startedAt)

  return (
    <div
      className={cn(
        'group relative overflow-hidden rounded-lg border bg-card pl-3 pr-2.5 py-2 transition-colors duration-300',
        'motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1',
        statusRing(agent.status)
      )}
    >
      {/* left status accent bar */}
      <span className={cn('absolute inset-y-0 left-0 w-0.5', statusBar(agent.status))} />

      <div className="flex items-center gap-2">
        <span className={cn('size-1.5 shrink-0 rounded-full', statusDot(agent.status))} />
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground" title={agent.label}>
          {agent.label}
        </span>
        {agent.cached ? (
          <span className="shrink-0 rounded bg-muted px-1.5 py-px text-[0.6rem] font-medium uppercase tracking-wide text-muted-foreground">
            cached
          </span>
        ) : agent.status === 'done' ? (
          <Check className="size-3.5 shrink-0 text-emerald-500" />
        ) : agent.status === 'failed' ? (
          <X className="size-3.5 shrink-0 text-destructive" />
        ) : active ? (
          <Loader2 className="size-3.5 shrink-0 animate-spin text-primary" />
        ) : null}
      </div>

      <div className="mt-1.5 flex items-center gap-2 text-[0.7rem] text-muted-foreground">
        <span
          className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-muted-foreground"
          title={isOrch ? 'orchestrator tier' : 'worker tier'}
        >
          {isOrch ? <Zap className="size-2.5 text-foreground/70" /> : <Cpu className="size-2.5" />}
          <span className="max-w-[8rem] truncate">{agent.model || (isOrch ? 'orchestrator' : 'worker')}</span>
        </span>
        <span className="ml-auto shrink-0 tabular-nums">{formatTokens(agent.outTokens)} tok</span>
        <span className="shrink-0 tabular-nums opacity-60">{elapsed}</span>
      </div>

      {(active && agent.tail) || agent.summary ? (
        <div className="mt-1.5 flex items-start gap-1.5 text-[0.7rem] leading-snug text-muted-foreground">
          {active && agent.tail ? (
            <>
              {agent.tailKind === 'reasoning' ? (
                <Brain className="mt-0.5 size-3 shrink-0 opacity-60" />
              ) : agent.tailKind === 'tool' ? (
                <Wrench className="mt-0.5 size-3 shrink-0 opacity-60" />
              ) : null}
              <span className="line-clamp-2">
                {agent.tailKind !== 'text' ? `${KIND_LABEL[agent.tailKind] ?? ''}: ` : ''}
                {agent.tail.slice(-160)}
              </span>
            </>
          ) : (
            <span className="line-clamp-2 opacity-90">{agent.summary}</span>
          )}
        </div>
      ) : null}
    </div>
  )
}

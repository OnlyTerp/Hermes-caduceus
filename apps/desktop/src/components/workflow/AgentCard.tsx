import { Brain, Check, Cpu, Loader2, X, Zap } from '@/lib/icons'
import { cn } from '@/lib/utils'
import type { WorkflowAgent } from '@/store/workflow'

import { formatElapsed, formatTokens, statusDot, statusRing } from './theater-format'

interface AgentCardProps {
  agent: WorkflowAgent
  orchestratorModel: string
}

const KIND_LABEL: Record<string, string> = { reasoning: 'thinking', text: 'writing', tool: 'tool' }

export function AgentCard({ agent, orchestratorModel }: AgentCardProps) {
  const isOrch = !!agent.model && !!orchestratorModel && agent.model === orchestratorModel
  const active = agent.status === 'running' || agent.status === 'streaming' || agent.status === 'queued'
  const elapsed = agent.ms ? formatElapsed(agent.ms) : formatElapsed(agent.updatedAt - agent.startedAt)

  return (
    <div
      className={cn(
        'group relative w-full overflow-hidden rounded-lg border bg-(--ui-chat-bubble-background)/60 px-2.5 py-2 text-[0.7rem] backdrop-blur-sm transition-all duration-300',
        'motion-safe:animate-in motion-safe:fade-in motion-safe:slide-in-from-bottom-1',
        statusRing(agent.status),
        agent.status === 'failed' && 'motion-safe:animate-pulse'
      )}
    >
      {/* streaming sheen */}
      {agent.status === 'streaming' && (
        <span className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-sky-300/80 to-transparent motion-safe:animate-pulse" />
      )}

      <div className="flex items-center gap-1.5">
        <span className={cn('size-1.5 shrink-0 rounded-full', statusDot(agent.status))} />
        <span className="min-w-0 flex-1 truncate font-medium text-foreground" title={agent.label}>
          {agent.label}
        </span>
        {agent.cached ? (
          <span className="shrink-0 rounded bg-muted-foreground/15 px-1 text-[0.55rem] uppercase tracking-wide text-muted-foreground">
            cached
          </span>
        ) : agent.status === 'done' ? (
          <Check className="size-3 shrink-0 text-emerald-400" />
        ) : agent.status === 'failed' ? (
          <X className="size-3 shrink-0 text-rose-400" />
        ) : (
          <Loader2 className="size-3 shrink-0 animate-spin text-sky-300" />
        )}
      </div>

      <div className="mt-1 flex items-center gap-1.5 text-[0.6rem] text-muted-foreground">
        <span
          className={cn(
            'inline-flex items-center gap-0.5 rounded px-1 py-px font-mono',
            isOrch ? 'bg-amber-400/15 text-amber-300' : 'bg-sky-400/12 text-sky-300'
          )}
          title={isOrch ? 'orchestrator tier' : 'worker tier'}
        >
          {isOrch ? <Zap className="size-2.5" /> : <Cpu className="size-2.5" />}
          <span className="max-w-[8rem] truncate">{agent.model || (isOrch ? 'orchestrator' : 'worker')}</span>
        </span>
        <span className="ml-auto tabular-nums">{formatTokens(agent.outTokens)} tok</span>
        <span className="tabular-nums opacity-70">{elapsed}</span>
      </div>

      {(active && agent.tail) || agent.summary ? (
        <div className="mt-1 flex items-start gap-1 text-[0.6rem] leading-snug text-muted-foreground/90">
          {active && agent.tail ? (
            <>
              {agent.tailKind === 'reasoning' && <Brain className="mt-px size-2.5 shrink-0 text-violet-300/80" />}
              <span className="line-clamp-2 italic">
                {agent.tailKind !== 'text' ? `${KIND_LABEL[agent.tailKind] ?? ''}: ` : ''}
                {agent.tail.slice(-160)}
              </span>
            </>
          ) : (
            <span className="line-clamp-2 text-muted-foreground/80">{agent.summary}</span>
          )}
        </div>
      ) : null}
    </div>
  )
}

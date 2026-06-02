import { atom } from 'nanostores'

import { openTheater } from './caduceus'

export type WorkflowAgentStatus = 'done' | 'failed' | 'queued' | 'running' | 'skipped' | 'streaming'
export type WorkflowDeltaKind = 'reasoning' | 'text' | 'tool'

export interface WorkflowAgent {
  id: string
  label: string
  phase: null | string
  model: null | string
  parentId: null | string
  status: WorkflowAgentStatus
  tail: string
  tailKind: WorkflowDeltaKind
  inTokens: number
  outTokens: number
  ms?: number
  cached?: boolean
  summary?: string
  startedAt: number
  updatedAt: number
}

export interface WorkflowPhase {
  title: string
  detail?: string
  index: number
}

export interface WorkflowVerify {
  id: string
  votes: { lens: string; verdict: string }[]
  result: string
  at: number
}

export interface WorkflowRun {
  runId: string
  name: string
  description: string
  phases: WorkflowPhase[]
  status: 'complete' | 'error' | 'running'
  agents: Record<string, WorkflowAgent>
  order: string[]
  narrator: { at: number; msg: string }[]
  verifies: WorkflowVerify[]
  budgetSpent: number
  budgetTotal: null | number
  concurrencyCap: number
  agentCount: number
  tokensOut: number
  startedAt: number
  updatedAt: number
  ms?: number
  resultSummary?: string
  error?: string
}

/** The current (or most recent) Loom run powering the Orchestration Theater. */
export const $workflowRun = atom<null | WorkflowRun>(null)

const TAIL_MAX = 280
const NARRATOR_MAX = 40
const VERIFY_MAX = 60
const ACTIVE: ReadonlySet<WorkflowAgentStatus> = new Set(['queued', 'running', 'streaming'])

const str = (v: unknown) => (typeof v === 'string' ? v : '')
const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : 0)

function asAgentStatus(v: unknown): WorkflowAgentStatus {
  return v === 'queued' || v === 'running' || v === 'streaming' || v === 'done' || v === 'failed' || v === 'skipped'
    ? v
    : 'running'
}

function clampTail(text: string): string {
  return text.length > TAIL_MAX ? `…${text.slice(text.length - TAIL_MAX)}` : text
}

function ensureAgent(run: WorkflowRun, id: string): WorkflowAgent {
  const existing = run.agents[id]
  if (existing) {
    return { ...existing }
  }
  run.order = [...run.order, id]
  return {
    id,
    inTokens: 0,
    label: id,
    model: null,
    outTokens: 0,
    parentId: null,
    phase: null,
    startedAt: Date.now(),
    status: 'queued',
    tail: '',
    tailKind: 'text',
    updatedAt: Date.now()
  }
}

function commit(run: WorkflowRun, agent: WorkflowAgent): WorkflowRun {
  agent.updatedAt = Date.now()
  return { ...run, agents: { ...run.agents, [agent.id]: agent }, updatedAt: Date.now() }
}

/** Reduce a `workflow.*` event into the live run graph. */
export function applyWorkflowEvent(type: string, payload: Record<string, unknown>): void {
  const p = payload ?? {}

  if (type === 'workflow.start') {
    const phases = Array.isArray(p.phases)
      ? p.phases.map((ph, i) => {
          const o = (ph ?? {}) as Record<string, unknown>
          return { detail: str(o.detail) || undefined, index: i, title: str(o.title) || `Phase ${i + 1}` }
        })
      : []
    $workflowRun.set({
      agentCount: 0,
      agents: {},
      budgetSpent: 0,
      budgetTotal: typeof p.budget_total === 'number' ? p.budget_total : null,
      concurrencyCap: num(p.concurrency_cap) || 1,
      description: str(p.description),
      error: undefined,
      name: str(p.name) || 'workflow',
      narrator: [],
      order: [],
      phases,
      resultSummary: undefined,
      runId: str(p.run_id) || str(p.runId),
      startedAt: Date.now(),
      status: 'running',
      tokensOut: 0,
      updatedAt: Date.now(),
      verifies: []
    })
    openTheater()
    return
  }

  const run = $workflowRun.get()
  if (!run) {
    return
  }

  switch (type) {
    case 'workflow.phase': {
      const title = str(p.phase)
      const index = num(p.index)
      const exists = run.phases.some(ph => ph.title === title)
      $workflowRun.set({
        ...run,
        phases: exists ? run.phases : [...run.phases, { index, title }],
        updatedAt: Date.now()
      })
      return
    }
    case 'workflow.agent.spawn': {
      const id = str(p.agent_id)
      if (!id) return
      const agent = ensureAgent(run, id)
      agent.label = str(p.label) || agent.label
      agent.phase = str(p.phase) || agent.phase
      agent.model = str(p.model) || agent.model
      agent.parentId = str(p.parent_id) || agent.parentId
      agent.status = 'queued'
      const next = commit(run, agent)
      next.agentCount = Math.max(next.agentCount, next.order.length)
      $workflowRun.set(next)
      return
    }
    case 'workflow.agent.status': {
      const id = str(p.agent_id)
      if (!run.agents[id]) return
      const agent = ensureAgent(run, id)
      agent.status = asAgentStatus(p.status)
      $workflowRun.set(commit(run, agent))
      return
    }
    case 'workflow.agent.delta': {
      const id = str(p.agent_id)
      const text = str(p.text)
      if (!id || !text) return
      const agent = ensureAgent(run, id)
      const kind = (p.kind === 'reasoning' || p.kind === 'tool' ? p.kind : 'text') as WorkflowDeltaKind
      agent.tail = clampTail((agent.tailKind === kind ? agent.tail : '') + text)
      agent.tailKind = kind
      if (ACTIVE.has(agent.status)) {
        agent.status = 'streaming'
      }
      $workflowRun.set(commit(run, agent))
      return
    }
    case 'workflow.agent.tokens': {
      const id = str(p.agent_id)
      if (!run.agents[id]) return
      const agent = ensureAgent(run, id)
      agent.inTokens = num(p.in)
      agent.outTokens = num(p.out)
      $workflowRun.set(commit(run, agent))
      return
    }
    case 'workflow.agent.done': {
      const id = str(p.agent_id)
      if (!id) return
      const agent = ensureAgent(run, id)
      agent.status = asAgentStatus(p.status) === 'failed' ? 'failed' : 'done'
      agent.summary = str(p.summary) || agent.summary
      agent.inTokens = num(p.in) || agent.inTokens
      agent.outTokens = num(p.out) || agent.outTokens
      agent.ms = num(p.ms) || agent.ms
      agent.cached = p.cached === true
      $workflowRun.set(commit(run, agent))
      return
    }
    case 'workflow.verify': {
      const votes = Array.isArray(p.votes)
        ? p.votes.map(v => {
            const o = (v ?? {}) as Record<string, unknown>
            return { lens: str(o.lens), verdict: str(o.verdict) }
          })
        : []
      $workflowRun.set({
        ...run,
        updatedAt: Date.now(),
        verifies: [...run.verifies, { at: Date.now(), id: str(p.finding_id), result: str(p.result), votes }].slice(
          -VERIFY_MAX
        )
      })
      return
    }
    case 'workflow.budget': {
      $workflowRun.set({
        ...run,
        budgetSpent: num(p.spent),
        budgetTotal: typeof p.total === 'number' ? p.total : run.budgetTotal,
        tokensOut: num(p.spent),
        updatedAt: Date.now()
      })
      return
    }
    case 'workflow.log': {
      const msg = str(p.message)
      if (!msg) return
      $workflowRun.set({
        ...run,
        narrator: [...run.narrator, { at: Date.now(), msg }].slice(-NARRATOR_MAX),
        updatedAt: Date.now()
      })
      return
    }
    case 'workflow.complete': {
      $workflowRun.set({
        ...run,
        agentCount: num(p.agents) || run.agentCount,
        ms: num(p.ms) || run.ms,
        resultSummary: str(p.result_summary),
        status: 'complete',
        tokensOut: num(p.out) || run.tokensOut,
        updatedAt: Date.now()
      })
      return
    }
    case 'workflow.error': {
      const aid = str(p.agent_id)
      if (aid && run.agents[aid]) {
        const agent = ensureAgent(run, aid)
        agent.status = 'failed'
        $workflowRun.set(commit(run, agent))
        return
      }
      $workflowRun.set({ ...run, error: str(p.message), status: 'error', updatedAt: Date.now() })
      return
    }
    default:
      return
  }
}

export function clearWorkflow(): void {
  $workflowRun.set(null)
}

export const workflowActiveCount = (run: null | WorkflowRun): number =>
  run ? run.order.filter(id => ACTIVE.has(run.agents[id]?.status ?? 'done')).length : 0

export const workflowIsLive = (run: null | WorkflowRun): boolean => !!run && run.status === 'running'

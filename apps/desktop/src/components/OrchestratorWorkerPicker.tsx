import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Check, Cpu, Zap } from '@/lib/icons'
import { cn } from '@/lib/utils'
import {
  $caduceus,
  $caduceusPickerOpen,
  type CaduceusTier,
  closeTierPicker,
  setCaduceusTiers
} from '@/store/caduceus'
import { $gateway } from '@/store/gateway'
import { $activeSessionId } from '@/store/session'
import type { ModelOptionProvider, ModelOptionsResponse } from '@/types/hermes'

import { getGlobalModelOptions } from '../hermes'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from './ui/command'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog'

type Slot = 'orchestrator' | 'worker'

function tierLabel(t: CaduceusTier): string {
  if (!t.model) {
    return ''
  }
  return t.provider ? `${t.provider}:${t.model}` : t.model
}

function ModelColumn({
  accent,
  current,
  icon,
  onPick,
  providers,
  search,
  soloOption,
  subtitle,
  title
}: {
  accent: string
  current: CaduceusTier
  icon: React.ReactNode
  onPick: (provider: string, model: string) => void
  providers: ModelOptionProvider[]
  search: string
  soloOption?: { label: string; onPick: () => void; active: boolean }
  subtitle: string
  title: string
}) {
  const q = search.trim().toLowerCase()
  const matches = (p: ModelOptionProvider, m: string) =>
    !q || m.toLowerCase().includes(q) || p.name.toLowerCase().includes(q) || p.slug.toLowerCase().includes(q)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className={cn('flex items-center gap-1.5 border-b border-(--ui-stroke-tertiary) px-3 py-2 text-xs font-semibold', accent)}>
        {icon}
        <span>{title}</span>
        <span className="ml-auto truncate font-mono text-[0.65rem] font-normal text-muted-foreground">
          {tierLabel(current) || subtitle}
        </span>
      </div>
      <CommandList className="max-h-[46vh] flex-1 overflow-y-auto">
        <CommandEmpty>No models match.</CommandEmpty>
        {soloOption && (
          <CommandGroup>
            <CommandItem
              className={cn('flex items-center gap-2', soloOption.active && 'bg-primary/15')}
              onSelect={soloOption.onPick}
              value="__solo__"
            >
              <span className="flex-1">{soloOption.label}</span>
              {soloOption.active && <Check className="size-3.5 text-primary" />}
            </CommandItem>
          </CommandGroup>
        )}
        {providers.map(provider => {
          const models = (provider.models ?? []).filter(m => matches(provider, m))
          if (!models.length) {
            return null
          }
          return (
            <CommandGroup heading={provider.name} key={provider.slug}>
              {models.map(model => {
                const isCurrent = model === current.model && (provider.slug === current.provider || !current.provider)
                return (
                  <CommandItem
                    className={cn('flex items-center gap-2 pl-4 font-mono text-xs', isCurrent && 'bg-primary/15')}
                    key={`${provider.slug}:${model}`}
                    onSelect={() => onPick(provider.slug, model)}
                    value={`${provider.slug}:${model}`}
                  >
                    <span className="min-w-0 flex-1 truncate">{model}</span>
                    {isCurrent && <Check className="size-3.5 shrink-0 text-primary" />}
                  </CommandItem>
                )
              })}
            </CommandGroup>
          )
        })}
      </CommandList>
    </div>
  )
}

/** Two-slot orchestrator/worker model picker for Caduceus mode. */
export function OrchestratorWorkerPicker() {
  const open = useStore($caduceusPickerOpen)
  const caduceus = useStore($caduceus)
  const gw = useStore($gateway)
  const sessionId = useStore($activeSessionId)
  const [search, setSearch] = useState('')

  const modelOptions = useQuery({
    enabled: open,
    queryFn: () =>
      gw && sessionId
        ? gw.request<ModelOptionsResponse>('model.options', { session_id: sessionId })
        : getGlobalModelOptions(),
    queryKey: ['model-options', 'caduceus', sessionId || 'global']
  })

  const providers = (modelOptions.data?.providers ?? []).filter(p => (p.models ?? []).length > 0)

  return (
    <Dialog onOpenChange={o => (o ? null : closeTierPicker())} open={open}>
      <DialogContent className="max-w-3xl gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b border-(--ui-stroke-tertiary) px-4 py-3">
          <DialogTitle className="flex items-center gap-2 text-sm">
            <Zap className="size-4 text-amber-300" /> Caduceus model tiers
          </DialogTitle>
          <DialogDescription className="text-xs">
            The orchestrator plans &amp; synthesizes; the worker runs every fan-out leaf. Leave the worker on
            “Solo” to run one model everywhere.
          </DialogDescription>
        </DialogHeader>
        <Command className="rounded-none" shouldFilter={false}>
          <CommandInput onValueChange={setSearch} placeholder="Filter models…" value={search} />
          <div className="flex min-h-0 divide-x divide-(--ui-stroke-tertiary)">
            <ModelColumn
              accent="text-amber-300"
              current={caduceus.orchestrator}
              icon={<Zap className="size-3.5" />}
              onPick={(provider, model) => void setCaduceusTiers(sessionId, { orchestrator: { model, provider } })}
              providers={providers}
              search={search}
              subtitle="session model"
              title="Orchestrator"
            />
            <ModelColumn
              accent="text-sky-300"
              current={caduceus.split ? caduceus.worker : { model: '', provider: '' }}
              icon={<Cpu className="size-3.5" />}
              onPick={(provider, model) => void setCaduceusTiers(sessionId, { worker: { model, provider } })}
              providers={providers}
              search={search}
              soloOption={{
                active: !caduceus.split,
                label: 'Solo — same as orchestrator',
                onPick: () => void setCaduceusTiers(sessionId, { worker: { model: '', provider: '' } })
              }}
              subtitle="solo"
              title="Worker"
            />
          </div>
        </Command>
      </DialogContent>
    </Dialog>
  )
}

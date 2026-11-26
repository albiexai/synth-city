import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { MODAL_SIZES } from '@/lib/constants'
import { formatServerTime } from '@/lib/serverTime'
import type { SupervisorBindingUpdate, SupervisorObservation } from '@/types/agentTeams'
import { fetchAuditEvents, type AuditEventRead } from '../factory/auditApi'
import { updateTeamSupervisor } from './api'
import { useOperatorTokenFlow } from './useOperatorTokenFlow'

const SOURCE = 'agent_team_supervisor.registration'
const PAGE_SIZE = 100
const REFUSALS: Record<string, string> = {
  supervisor_actor_missing: 'No external actor has this id.',
  supervisor_binding_changed: 'The binding changed while you edited. Review the current binding, then save again.',
  supervisor_team_missing: 'This team does not exist any more.',
}
const REASONS: Record<string, string> = { supervisor_registered: 'Binding saved', supervisor_removed: 'Supervisor removed' }
const words = (failure: unknown, fallback: string) => {
  const message = failure instanceof Error ? failure.message : fallback
  return REFUSALS[message] ?? message
}
const actorWords = (value: unknown) => (value === null || value === undefined ? 'none' : `actor ${String(value)}`)
const graceWords = (value: unknown) => (value === null || value === undefined ? 'default' : `${String(value)} seconds`)
// A binding is named by its version (the writes of the team) and the team's context key (a deleted team's id can be reused, its key cannot).
type BindingKey = { version: number; key: string }
const sameBinding = (a: BindingKey, b: BindingKey | undefined) => b !== undefined && a.version === b.version && a.key === b.key
const wholeFrom = (text: string, least: number) => (text.trim() !== '' && Number.isInteger(Number(text)) && Number(text) >= least ? Number(text) : null)

function changes(event: AuditEventRead) {
  const before = event.before_values ?? {}
  const after = event.after_values ?? {}
  const lines: string[] = []
  if (before.supervisor_actor_id !== after.supervisor_actor_id) lines.push(`Supervisor: ${actorWords(before.supervisor_actor_id)} → ${actorWords(after.supervisor_actor_id)}`)
  if (before.supervisor_grace_seconds !== after.supervisor_grace_seconds) lines.push(`Grace: ${graceWords(before.supervisor_grace_seconds)} → ${graceWords(after.supervisor_grace_seconds)}`)
  return lines
}

/**
 * Bind, change the grace of, and remove the supervisor of a team (#1298). The actor is chosen by its id: the server
 * checks it. The history is the audit record of each change. A field that the user has not touched follows the server.
 */
export function SupervisorBindingDialog({ presetId, presetName, observation, open, onOpenChange, refresh, onReturnFocus }: {
  presetId: number
  presetName: string
  observation: SupervisorObservation | null
  open: boolean
  onOpenChange: (open: boolean) => void
  refresh: () => Promise<void>
  onReturnFocus: () => void
}) {
  const { withOperatorToken, tokenDialog } = useOperatorTokenFlow('to read or change the supervisor of a team')
  const [history, setHistory] = useState<AuditEventRead[]>([])
  const [nextPage, setNextPage] = useState<number | null>(null)
  const [historyState, setHistoryState] = useState<'loading' | 'ready' | 'failed'>('loading')
  const [draft, setDraft] = useState<{ actor?: string; grace?: string; useDefault?: boolean; baseline?: BindingKey }>({})
  const [confirmKey, setConfirmKey] = useState<BindingKey | null>(null) // the binding a removal was confirmed for
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const generation = useRef(0) // a newer first page, or closing the dialog, makes older answers obsolete
  const pending = useRef(new Set<number>()) // one request for each page at a time
  const session = useRef(0) // each open and each close starts a new session; a save from an older one changes nothing
  const boundId = observation?.actor_id ?? null
  const actorText = draft.actor ?? (boundId === null ? '' : String(boundId))
  const graceText = draft.grace ?? (boundId === null ? '' : String(observation?.grace_seconds ?? ''))
  const useDefault = draft.useDefault ?? false
  // Writes need the binding version and the team context key of the server. Without them a change by another client, or
  // a team that reuses a deleted team's id, could not be detected.
  const version = observation?.binding_version
  const teamKey = observation?.team_context_key
  const current: BindingKey | undefined = typeof version === 'number' && typeof teamKey === 'string' && teamKey !== '' ? { version, key: teamKey } : undefined
  const canWrite = current !== undefined
  const reviewed = draft.baseline ?? current // the binding that the operator saw when the edit began
  const stale = canWrite && draft.baseline !== undefined && !sameBinding(draft.baseline, current)
  const locked = saving || !canWrite
  const confirmOpen = canWrite && confirmKey !== null && sameBinding(confirmKey, current)
  const confirmLost = confirmKey !== null && !sameBinding(confirmKey, current)
  const edit = (patch: { actor?: string; grace?: string; useDefault?: boolean }) =>
    setDraft((previous) => ({ ...previous, ...patch, baseline: previous.baseline ?? current }))

  const load = useCallback(async (page = 1) => {
    if (page === 1) {
      generation.current += 1
      pending.current.clear()
    } else if (pending.current.has(page)) {
      return
    }
    const mine = generation.current
    pending.current.add(page)
    try {
      const result = await withOperatorToken((token) => fetchAuditEvents({ teamId: presetId, eventKind: 'policy_change', page, pageSize: PAGE_SIZE, operatorToken: token }))
      if (mine !== generation.current) return
      const items = result.items.filter((item) => item.source === SOURCE)
      setHistory((current) => (page === 1 ? items : [...current, ...items.filter((item) => !current.some((seen) => seen.id === item.id))]))
      setNextPage(page * PAGE_SIZE < result.total ? page + 1 : null)
      setHistoryState('ready')
    } catch (failure) {
      if (mine !== generation.current) return
      if (page === 1) setHistoryState('failed')
      setError(`Could not read the history: ${words(failure, 'unknown error')}`)
    } finally {
      pending.current.delete(page)
    }
  }, [presetId, withOperatorToken])

  useEffect(() => {
    session.current += 1
  }, [open])

  useEffect(() => {
    if (!open) {
      generation.current += 1
      return
    }
    queueMicrotask(() => {
      setDraft({}); setConfirmKey(null); setError(null); setSaving(false); setHistory([]); setNextPage(null); setHistoryState('loading')
      void load()
    })
  }, [open, load])

  const send = async (update: SupervisorBindingUpdate) => {
    const mine = session.current
    setSaving(true)
    try {
      await withOperatorToken((token) => updateTeamSupervisor(presetId, update, token))
      await refresh()
      if (mine !== session.current) return
      setDraft({}); setConfirmKey(null)
      await load()
      toast.success('Supervisor binding saved')
    } catch (failure) {
      if (mine === session.current) {
        setError(words(failure, 'Failed to save the supervisor binding'))
        if (failure instanceof Error && failure.message === 'supervisor_binding_changed') {
          setDraft({}); setConfirmKey(null)
          void refresh().catch(() => undefined)
        }
      }
    } finally {
      if (mine === session.current) setSaving(false)
    }
  }

  const submit = () => {
    setError(null)
    if (reviewed === undefined || stale) return
    const actorId = wholeFrom(actorText, 1)
    if (actorId === null) {
      setError('Enter the actor id as a whole number, 1 or more.')
      return
    }
    const update: SupervisorBindingUpdate = { actor_id: actorId, expected_binding_version: reviewed.version, expected_team_context_key: reviewed.key }
    if (draft.useDefault) {
      update.grace_seconds = null
    } else if (draft.grace !== undefined) {
      const seconds = wholeFrom(draft.grace, 1)
      if (seconds === null) {
        setError('Enter the grace as whole seconds, 1 or more.')
        return
      }
      if (boundId === null || seconds !== observation?.grace_seconds) update.grace_seconds = seconds
    }
    if (actorId === boundId && !('grace_seconds' in update)) {
      setError('No value changed.')
      return
    }
    void send(update)
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={MODAL_SIZES.SM} onCloseAutoFocus={(event) => { event.preventDefault(); onReturnFocus() }}>
        <DialogHeader>
          <DialogTitle>Supervisor binding</DialogTitle>
          <DialogDescription>{presetName}. The Director key is needed to read the history and to save.</DialogDescription>
        </DialogHeader>
        {error && <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
        <p className="text-sm">
          {observation === null ? 'The supervisor binding could not be read.' : boundId === null ? 'No supervisor is bound.' : `Bound: ${observation.address?.display_name ?? observation.address?.address ?? 'actor'} (actor ${boundId})`}
        </p>
        {observation !== null && !canWrite && (
          <p className="text-sm text-muted-foreground">Changes are off: this server does not report a binding version and a team key, so a change by another client could not be detected. Update the server to edit the supervisor.</p>
        )}
        {stale && (
          <div role="status" className="grid gap-2 rounded-md border border-amber-500/60 p-3 text-sm">
            <p>The binding changed while you edited. Review the current binding.</p>
            <Button variant="outline" size="sm" onClick={() => { setDraft({}); setConfirmKey(null) }}>Review the current binding</Button>
          </div>
        )}
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="supervisor-actor">Supervisor actor id</Label>
            <Input id="supervisor-actor" inputMode="numeric" value={actorText} disabled={locked} onChange={(event) => edit({ actor: event.target.value })} />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="supervisor-grace">Grace (seconds)</Label>
            <Input id="supervisor-grace" inputMode="numeric" value={graceText} placeholder="default" disabled={useDefault || locked} onChange={(event) => edit({ grace: event.target.value })} />
            <div className="flex items-center gap-2 text-sm">
              <Checkbox id="supervisor-default" checked={useDefault} disabled={locked} onCheckedChange={(checked) => edit({ useDefault: checked === true })} />
              <Label htmlFor="supervisor-default" className="font-normal">Use the default grace</Label>
            </div>
          </div>
          <Button onClick={submit} disabled={locked || stale}>Save</Button>
          {boundId !== null && !confirmOpen && <Button variant="outline" onClick={() => setConfirmKey(current ?? null)} disabled={locked}>Remove the supervisor</Button>}
          {confirmLost && !confirmOpen && <p className="text-sm text-muted-foreground">The binding changed, so the removal was not confirmed.</p>}
          {confirmOpen && (
            <div className="grid gap-2 rounded-md border p-3 text-sm">
              <p>Remove this supervisor? Its grace setting is cleared.</p>
              <div className="flex gap-2">
                <Button variant="destructive" size="sm" onClick={() => { if (confirmKey !== null) void send({ actor_id: null, expected_binding_version: confirmKey.version, expected_team_context_key: confirmKey.key }) }} disabled={saving}>Remove</Button>
                <Button variant="outline" size="sm" onClick={() => setConfirmKey(null)} disabled={saving}>Keep the supervisor</Button>
              </div>
            </div>
          )}
        </div>
        <section aria-label="History" className="space-y-2">
          <h4 className="text-sm font-semibold">History</h4>
          {historyState === 'ready' && history.length === 0 && nextPage === null && <p className="text-sm text-muted-foreground">No changes recorded yet.</p>}
          {historyState === 'ready' && history.length === 0 && nextPage !== null && <p className="text-sm text-muted-foreground">No supervisor binding change in the events loaded so far. Load older history to look further.</p>}
          {historyState === 'failed' && <p className="text-sm text-muted-foreground">The history is not available.</p>}
          <ul className="space-y-2 text-sm">
            {history.map((item) => (
              <li key={item.id} className="rounded-md border p-2">
                <p className="text-xs text-muted-foreground">{formatServerTime(item.occurred_at)} · {item.actor_reference ?? item.actor_kind}</p>
                {changes(item).map((line) => <p key={line}>{line}</p>)}
                {item.sanitized_reason && <p className="break-words text-muted-foreground">{REASONS[item.sanitized_reason] ?? item.sanitized_reason}</p>}
              </li>
            ))}
          </ul>
          {historyState === 'ready' && nextPage !== null && <Button variant="outline" size="sm" onClick={() => void load(nextPage)}>Load older history</Button>}
        </section>
        {tokenDialog}
      </DialogContent>
    </Dialog>
  )
}

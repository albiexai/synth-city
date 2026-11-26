import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { MODAL_SIZES } from '@/lib/constants'
import { formatServerTime } from '@/lib/serverTime'
import type { ScopeSettingDefinition, ScopeSettingsHistoryEntry, TeamGithubScope, TeamGithubScopeUpdate } from '@/types/agentTeams'
import { fetchScopeSettings, updateTeamGithubScope } from './api'
import { useOperatorTokenFlow } from './useOperatorTokenFlow'

const NAMES: Record<string, string> = {
  review_hold_label: 'Review hold label',
  max_item_effort_minutes: 'Effort ceiling',
  initial_paths_required: 'First-plan paths required',
}
const show = (value: unknown) => (value === null || value === undefined ? 'none' : String(value))

/**
 * Edit the review hold label and the effort ceiling of one repository (#1297 part A). The ranges and the history
 * come from the server. A label is cleared with an explicit null: an empty label is invalid.
 */
export function ScopeLimitsDialog({ scope, open, onOpenChange, refresh, onReturnFocus }: {
  scope: TeamGithubScope
  open: boolean
  onOpenChange: (open: boolean) => void
  refresh: () => Promise<void>
  onReturnFocus: () => void
}) {
  const { withOperatorToken, tokenDialog } = useOperatorTokenFlow('to read or change repository limits')
  const [definitions, setDefinitions] = useState<Record<string, ScopeSettingDefinition>>({})
  const [history, setHistory] = useState<ScopeSettingsHistoryEntry[]>([])
  const [next, setNext] = useState<number | null>(null)
  const [historyState, setHistoryState] = useState<'loading' | 'ready' | 'failed'>('loading')
  // A field the user has not touched is undefined and follows the server, so a background refresh never erases a draft.
  const [draft, setDraft] = useState<{ label?: string; clear?: boolean; minutes?: string; requirePaths?: boolean; reason: string }>({ reason: '' })
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const generation = useRef(0) // a newer first load, or closing the dialog, makes older answers obsolete
  const pending = useRef(new Set<number>()) // one request for each cursor at a time
  const session = useRef(0) // each open and each close starts a new session; a save from an older session changes nothing
  const label = draft.label ?? scope.review_hold_label ?? ''
  const clear = draft.clear ?? scope.review_hold_label == null
  const minutes = draft.minutes ?? String(scope.max_item_effort_minutes ?? '')
  const pathsRequired = scope.initial_paths_required ?? false
  const requirePaths = draft.requirePaths ?? pathsRequired
  const reason = draft.reason

  const load = useCallback(async (beforeId?: number) => {
    const key = beforeId ?? 0
    if (beforeId === undefined) {
      generation.current += 1
      pending.current.clear()
    } else if (pending.current.has(key)) {
      return
    }
    const mine = generation.current
    pending.current.add(key)
    try {
      const page = await withOperatorToken((token) => fetchScopeSettings(scope.id, token, beforeId))
      if (mine !== generation.current) return
      setDefinitions(page.definitions)
      setHistory((current) => (beforeId === undefined ? page.history : [...current, ...page.history.filter((item) => !current.some((seen) => seen.event_id === item.event_id))]))
      setNext(page.next_before_id)
      setHistoryState('ready')
      setError(null)
    } catch (failure) {
      if (mine !== generation.current) return
      if (beforeId === undefined) setHistoryState('failed')
      setError(`Could not read the settings and history: ${failure instanceof Error ? failure.message : 'unknown error'}`)
    } finally {
      pending.current.delete(key)
    }
  }, [scope.id, withOperatorToken])

  useEffect(() => {
    session.current += 1
  }, [open])

  useEffect(() => {
    if (!open) {
      generation.current += 1
      return
    }
    queueMicrotask(() => {
      setDraft({ reason: '' }); setDefinitions({}); setHistory([]); setNext(null); setSaving(false); setHistoryState('loading')
      void load()
    })
  }, [open, load])

  const range = definitions.max_item_effort_minutes
  const min = range?.minimum, max = range?.maximum
  const submit = async () => {
    const update: TeamGithubScopeUpdate = {}
    const value = Number(minutes)
    setError(null)
    if (minutes.trim() === '' || !Number.isInteger(value) || min === undefined || max === undefined || value < min || value > max) {
      setError(`Enter a whole number of minutes from ${min} to ${max}.`)
      return
    }
    if (!clear && !label.trim()) {
      setError('Enter a label, or choose to clear it.')
      return
    }
    if (value !== scope.max_item_effort_minutes) update.max_item_effort_minutes = value
    if (requirePaths !== pathsRequired) update.initial_paths_required = requirePaths
    if (clear ? scope.review_hold_label != null : label.trim() !== scope.review_hold_label) update.review_hold_label = clear ? null : label.trim()
    if (Object.keys(update).length === 0) {
      setError('No value changed.')
      return
    }
    if (reason.trim()) update.reason = reason.trim()
    const mine = session.current
    setSaving(true)
    try {
      await withOperatorToken((token) => updateTeamGithubScope(scope.id, update, token))
      await refresh()
      if (mine !== session.current) return
      setDraft({ reason: '' })
      await load()
      toast.success('Repository limits saved')
    } catch (failure) {
      if (mine === session.current) setError(failure instanceof Error ? failure.message : 'Failed to save the limits')
    } finally {
      if (mine === session.current) setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={MODAL_SIZES.SM} onCloseAutoFocus={(event) => { event.preventDefault(); onReturnFocus() }}>
        <DialogHeader>
          <DialogTitle>Review hold and effort limit</DialogTitle>
          <DialogDescription>{scope.repo_owner}/{scope.repo_name}. The Director key is needed to read the history and to save.</DialogDescription>
        </DialogHeader>
        {error && <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
        <div className="grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="limits-label">Review hold label</Label>
            <Input id="limits-label" value={label} disabled={clear || saving} onChange={(event) => setDraft((current) => ({ ...current, label: event.target.value, clear: false }))} />
            <div className="flex items-center gap-2 text-sm">
              <Checkbox id="limits-clear" checked={clear} disabled={saving} onCheckedChange={(checked) => setDraft((current) => ({ ...current, clear: checked === true }))} />
              <Label htmlFor="limits-clear" className="font-normal">No hold label (clear it)</Label>
            </div>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="limits-minutes">Effort ceiling (minutes)</Label>
            <Input id="limits-minutes" inputMode="numeric" value={minutes} disabled={saving} onChange={(event) => setDraft((current) => ({ ...current, minutes: event.target.value }))} />
            {range && <p className="text-xs text-muted-foreground">Whole minutes, {min} to {max} minutes{range.default !== undefined && `, default ${String(range.default)}`}.</p>}
          </div>
          {definitions.initial_paths_required?.type === 'boolean' && (
            <div className="flex items-center gap-2 text-sm">
              <Checkbox id="limits-require-paths" checked={requirePaths} disabled={saving}
                onCheckedChange={(checked) => setDraft((current) => ({ ...current, requirePaths: checked === true }))} />
              <Label htmlFor="limits-require-paths">Require paths in the first plan</Label>
            </div>
          )}
          <div className="grid gap-2">
            <Label htmlFor="limits-reason">Reason (optional)</Label>
            <Input id="limits-reason" maxLength={1000} value={reason} disabled={saving} onChange={(event) => setDraft((current) => ({ ...current, reason: event.target.value }))} />
          </div>
          <Button onClick={() => void submit()} disabled={saving || !range}>Save</Button>
        </div>
        <section aria-label="History" className="space-y-2">
          <h4 className="text-sm font-semibold">History</h4>
          {historyState === 'ready' && history.length === 0 && <p className="text-sm text-muted-foreground">No changes recorded yet.</p>}
          {historyState === 'failed' && <p className="text-sm text-muted-foreground">The history is not available.</p>}
          <ul className="space-y-2 text-sm">
            {history.map((item) => (
              <li key={item.event_id} className="rounded-md border p-2">
                <p className="text-xs text-muted-foreground">{formatServerTime(item.occurred_at)} · {item.actor_reference ?? item.actor_kind}</p>
                {Object.entries(item.changes).map(([name, change]) => <p key={name}>{NAMES[name] ?? name}: {show(change.before)} → {show(change.after)}</p>)}
                {item.redacted_fields.map((name) => <p key={name}>{NAMES[name] ?? name}: changed, value hidden</p>)}
                {item.reason && <p className="break-words text-muted-foreground">{item.reason}</p>}
              </li>
            ))}
          </ul>
          {historyState === 'ready' && next !== null && <Button variant="outline" size="sm" onClick={() => void load(next)}>Load more history</Button>}
        </section>
        {tokenDialog}
      </DialogContent>
    </Dialog>
  )
}

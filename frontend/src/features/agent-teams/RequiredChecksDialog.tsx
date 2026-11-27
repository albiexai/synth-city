import { useEffect, useRef, useState } from 'react'
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { MODAL_SIZES } from '@/lib/constants'
import type { DeliveryRequiredCheck, TeamGithubScope } from '@/types/agentTeams'
import { DeliveryPolicyHistory } from './DeliveryPolicyHistory'
import { charCount, MAX_REASON, useDeliveryPolicy } from './useDeliveryPolicy'

const DEFAULT_APP = 'github-actions'
const MAX_CHECKS = 32 // the server's limits (FactoryDeliveryPolicy, DeliveryRequiredCheck)
const MAX_NAME = 200
const MAX_APP = 100
const APP_PATTERN = /^[a-z0-9-]+$/

type Row = { key: number; name: string; app: string }
const length = charCount
const same = (a: DeliveryRequiredCheck, b: DeliveryRequiredCheck) => a.name === b.name && a.app_slug === b.app_slug

/** Every rule that the server enforces on the list, so that a refusal is shown before the request is sent. */
function validate(rows: Row[], hosted: boolean) {
  const seen = new Set<string>()
  const byRow: Record<number, string[]> = {}
  for (const row of rows) {
    const problems: string[] = []
    const name = row.name.trim(), app = row.app.trim()
    if (!name) problems.push('Enter a check name.')
    else if (length(name) > MAX_NAME) problems.push(`A check name has at most ${MAX_NAME} characters.`)
    if (!app) problems.push('Enter an app.')
    else if (length(app) > MAX_APP) problems.push(`An app has at most ${MAX_APP} characters.`)
    else if (!APP_PATTERN.test(app)) problems.push('An app uses lowercase letters, digits and hyphens only.')
    const identity = `${name}\u0000${app}`
    if (name && app && seen.has(identity)) problems.push('This name and app are already in the list.')
    seen.add(identity)
    if (problems.length) byRow[row.key] = problems
  }
  const list: string[] = []
  if (rows.length > MAX_CHECKS) list.push(`A policy has at most ${MAX_CHECKS} required checks.`)
  if (rows.length === 0 && hosted) list.push('The broad checks run on hosted CI, so the list needs at least one check.')
  return { byRow, list, valid: Object.keys(byRow).length === 0 && list.length === 0 }
}

/**
 * Edit the required checks of one repository (#1345, part B1 of #1297). The server replaces the whole delivery
 * policy on a save, so the save sends the current policy with only the list changed. A stale revision is refused.
 */
export function RequiredChecksDialog({ scope, open, onOpenChange, refresh, onReturnFocus }: {
  scope: TeamGithubScope
  open: boolean
  onOpenChange: (open: boolean) => void
  refresh: () => Promise<void>
  onReturnFocus: () => void
}) {
  const { policy, revision, locked, obsolete, isStale, reason, setReason, reasonTooLong, events, historyState, error, setError, saving, save, readAgain, tokenDialog } =
    useDeliveryPolicy({ scope, open, refresh, tokenReason: 'to read or change the required checks' })
  // A list the user has not touched is undefined and follows the server, so a background refresh never erases a draft.
  const [draft, setDraft] = useState<{ base: number; rows: Row[] } | null>(null)
  const nextKey = useRef(1)

  const serverRows: Row[] = (policy?.required_checks ?? []).map((check, index) => ({ key: -(index + 1), name: check.name, app: check.app_slug }))
  const rows = draft?.rows ?? serverRows
  const stale = isStale(draft?.base)
  const check = validate(rows, policy?.broad_checks === 'hosted')
  const edit = (change: (current: Row[]) => Row[]) => setDraft((current) => ({ base: current?.base ?? revision ?? 0, rows: change(current?.rows ?? serverRows) }))
  const setField = (key: number, field: 'name' | 'app', value: string) => edit((current) => current.map((row) => (row.key === key ? { ...row, [field]: value } : row)))
  const move = (index: number, by: -1 | 1) => edit((current) => {
    const next = [...current]
    ;[next[index], next[index + by]] = [next[index + by], next[index]]
    return next
  })

  useEffect(() => {
    if (open) queueMicrotask(() => setDraft(null)) // a new opening starts without the old draft
  }, [open])

  const submit = async () => {
    if (!policy || revision === undefined || !draft) return
    setError(null)
    const normalized = rows.map((row) => ({ name: row.name.trim(), app_slug: row.app.trim() }))
    if (stale) { setError('The policy changed on the server after you started. Discard your changes to see the current list.'); return }
    if (!check.valid) { setError('Fix the problems in the list first.'); return }
    if (normalized.length === policy.required_checks.length && normalized.every((item, index) => same(item, policy.required_checks[index]))) {
      setError('No change to save.')
      return
    }
    await save({ base: draft.base, next: { ...policy, required_checks: normalized }, onSaved: () => setDraft(null), success: 'Required checks saved' })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={MODAL_SIZES.SM} onCloseAutoFocus={(event) => { event.preventDefault(); onReturnFocus() }}>
        <DialogHeader>
          <DialogTitle>Required checks</DialogTitle>
          <DialogDescription>
            {scope.repo_owner}/{scope.repo_name}. A pull request merges only when every check below succeeds on its exact head. The Director key is needed to read the history and to save.
          </DialogDescription>
        </DialogHeader>
        {error && <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
        {!policy && <p role="alert" className="text-sm text-destructive">This server did not send the delivery policy, so the checks cannot be edited.</p>}
        {stale && (
          <div role="alert" className="flex flex-wrap items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">
            <span>{draft?.base !== revision ? `The policy changed on the server (revision ${draft?.base} to ${revision}). ` : 'The server refused the save because the policy changed. '}Your changes are not saved.</span>
            <Button variant="outline" size="sm" onClick={() => setDraft(null)}>Discard my changes</Button>
          </div>
        )}
        {locked && (
          <div role="alert" className="flex flex-wrap items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">
            <span>The policy changed on the server (revision {obsolete} is out of date). The current policy is not available yet, so the list is read-only.</span>
            <Button variant="outline" size="sm" onClick={readAgain}>Read again</Button>
          </div>
        )}
        <div className="grid gap-4">
          <ul aria-label="Required checks" className="space-y-3">
            {rows.map((row, index) => {
              const problems = check.byRow[row.key] ?? []
              const errorId = `check-error-${row.key}`
              return (
                <li key={row.key} className="space-y-1">
                  <div className="grid gap-2 sm:grid-cols-[1fr_11rem_auto]">
                    <Input aria-label={`Check ${index + 1} name`} aria-describedby={problems.length ? errorId : undefined} aria-invalid={problems.length > 0} placeholder="Check name" value={row.name} disabled={saving || locked || !policy}
                      onChange={(event) => setField(row.key, 'name', event.target.value)} />
                    <Input aria-label={`Check ${index + 1} app`} aria-describedby={problems.length ? errorId : undefined} aria-invalid={problems.length > 0} placeholder={DEFAULT_APP} value={row.app} disabled={saving || locked || !policy}
                      onChange={(event) => setField(row.key, 'app', event.target.value)} />
                    <div className="flex gap-1">
                      <Button variant="outline" size="icon" aria-label={`Move check ${index + 1} up`} disabled={saving || locked || index === 0} onClick={() => move(index, -1)}><ArrowUp className="size-4" /></Button>
                      <Button variant="outline" size="icon" aria-label={`Move check ${index + 1} down`} disabled={saving || locked || index === rows.length - 1} onClick={() => move(index, 1)}><ArrowDown className="size-4" /></Button>
                      <Button variant="outline" size="icon" aria-label={`Remove check ${index + 1}`} disabled={saving || locked} onClick={() => edit((current) => current.filter((item) => item.key !== row.key))}><Trash2 className="size-4" /></Button>
                    </div>
                  </div>
                  {problems.length > 0 && <p id={errorId} className="text-xs text-destructive">{problems.join(' ')}</p>}
                </li>
              )
            })}
          </ul>
          {rows.length === 0 && <p className="text-sm text-muted-foreground">No required checks. A head with no check runs is not green.</p>}
          {check.list.map((message) => <p key={message} className="text-xs text-destructive">{message}</p>)}
          <div>
            <Button variant="outline" size="sm" disabled={saving || locked || !policy || rows.length >= MAX_CHECKS}
              onClick={() => edit((current) => [...current, { key: nextKey.current++, name: '', app: DEFAULT_APP }])}>
              <Plus className="size-4" /> Add a check
            </Button>
          </div>
          <div className="grid gap-2">
            <Label htmlFor="checks-reason">Reason</Label>
            <Input id="checks-reason" aria-invalid={reasonTooLong} value={reason} disabled={saving || locked || !policy} onChange={(event) => setReason(event.target.value)} />
            <p className={reasonTooLong ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>{reasonTooLong ? `The reason has at most ${MAX_REASON} characters.` : 'The reason is required. It is kept in the history with the new list.'}</p>
          </div>
          <Button onClick={() => void submit()} disabled={saving || !policy || !draft || stale}>Save</Button>
        </div>
        <DeliveryPolicyHistory events={events} state={historyState} />
        {tokenDialog}
      </DialogContent>
    </Dialog>
  )
}

import { useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { MODAL_SIZES } from '@/lib/constants'
import type { DeliveryPolicy, TeamGithubScope } from '@/types/agentTeams'
import { DeliveryPolicyHistory } from './DeliveryPolicyHistory'
import { type DeliveryRevisions, FIELD_LABELS, MAX_REASON, useDeliveryPolicy } from './useDeliveryPolicy'

type Timer = 'owner_idle_seconds' | 'owner_nudge_grace_seconds' | 'owner_observation_wait_seconds' | 'owner_observation_resume_seconds' | 'checkpoint_delay_report_seconds'
/**
 * The server's ranges (FactoryDeliveryPolicy in backend/app/models/schemas.py). The server has no endpoint for them,
 * so tests pin these numbers. A field that may be unset is sent as null when its box is empty.
 */
const TIMERS: { key: Timer; min: number; max: number; nullable: boolean; help: string }[] = [
  { key: 'owner_idle_seconds', min: 60, max: 86400, nullable: true, help: 'How long an owner can show no activity before the factory nudges it. Empty: the team or server default.' },
  { key: 'owner_nudge_grace_seconds', min: 30, max: 3600, nullable: true, help: 'The grace period after a nudge. Empty: the team or server default.' },
  { key: 'owner_observation_wait_seconds', min: 30, max: 1800, nullable: true, help: 'After a nudge, how long to wait when native evidence is unknown, before the item pauses for the operator. Needs native owner contact. Empty: no wait.' },
  { key: 'owner_observation_resume_seconds', min: 60, max: 3600, nullable: false, help: 'How long a paused item waits for the operator. An unchanged pause can be resumed before this deadline. At or after it, the resume is refused.' },
  { key: 'checkpoint_delay_report_seconds', min: 60, max: 86400, nullable: true, help: 'Builders are told to report a checkpoint publication delay after this time. Empty: builders get no such instruction.' },
]
const SWITCHES: { key: 'caller_inventory_required' | 'regression_proof_required'; help: string }[] = [
  { key: 'caller_inventory_required', help: 'Builders record and review the caller inventory before they edit shared consumers.' },
  { key: 'regression_proof_required', help: 'For each regression, a builder shows a failing assertion on the known defective source and a pass on the candidate.' },
]
// Each option has a short label for the box and an explanation that shows under the field, because a box cuts a long text.
type Option = readonly [value: string, label: string, help: string]
const BROAD: readonly Option[] = [
  ['local', 'Local', 'Builders run the agreed broad checks once before they publish a batch.'],
  ['hosted', 'Hosted', 'Builders run focused checks. The required hosted jobs give the final evidence.'],
]
const BASE_UPDATE: readonly Option[] = [
  ['disabled', 'Disabled', 'The operator integration-update command is refused. Use GitHub Update branch by hand.'],
  ['fast_forward', 'Fast forward', 'The command moves the workspace to the accepted base only when the history stays linear.'],
  ['merge', 'Merge', 'The command merges the accepted base into the workspace.'],
]
const CONTACT: readonly Option[] = [
  ['reports', 'Reports', 'The owner sends progress reports before the idle time ends.'],
  ['native', 'Native', 'Fresh activity in the owner session can renew owner contact.'],
]

type Values = { broad_checks: string; accepted_base_update: string; owner_contact: string; caller_inventory_required: boolean; regression_proof_required: boolean } & Record<Timer, string>
const toValues = (policy: DeliveryPolicy): Values => ({
  broad_checks: policy.broad_checks, accepted_base_update: policy.accepted_base_update, owner_contact: policy.owner_contact,
  caller_inventory_required: policy.caller_inventory_required, regression_proof_required: policy.regression_proof_required,
  owner_idle_seconds: String(policy.owner_idle_seconds ?? ''), owner_nudge_grace_seconds: String(policy.owner_nudge_grace_seconds ?? ''),
  owner_observation_wait_seconds: String(policy.owner_observation_wait_seconds ?? ''), owner_observation_resume_seconds: String(policy.owner_observation_resume_seconds),
  checkpoint_delay_report_seconds: String(policy.checkpoint_delay_report_seconds ?? ''),
})

/** Every rule that the server enforces on these fields, so that a refusal is shown before the request is sent. */
function validate(values: Values, checkCount: number) {
  const timers: Partial<Record<Timer, string>> = {}
  const parsed: Partial<Record<Timer, number | null>> = {}
  for (const { key, min, max, nullable } of TIMERS) {
    const text = values[key].trim()
    if (text === '') {
      if (nullable) parsed[key] = null
      else timers[key] = `Enter a whole number from ${min} to ${max}.`
    } else if (!/^\d+$/.test(text) || Number(text) < min || Number(text) > max) timers[key] = `Enter a whole number from ${min} to ${max}.`
    else parsed[key] = Number(text)
  }
  const cross: string[] = []
  if (values.owner_observation_wait_seconds.trim() !== '' && values.owner_contact !== 'native') cross.push('An observation wait needs native owner contact. Choose Native, or clear the wait.')
  if (values.broad_checks === 'hosted' && checkCount === 0) cross.push('Hosted broad checks need at least one required check. Add one in Required checks first, or choose Local.')
  return { timers, parsed, cross, valid: Object.keys(timers).length === 0 && cross.length === 0 }
}

function Choice({ id, name, value, options, disabled, onChange }: { id: string; name: string; value: string; options: readonly Option[]; disabled: boolean; onChange: (value: string) => void }) {
  return (
    <div className="grid gap-1">
      <Label htmlFor={id}>{name}</Label>
      <select id={id} aria-describedby={`${id}-help`} className="h-9 w-full min-w-0 rounded-md border bg-background px-2 text-sm" value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
        {options.map(([option, text]) => <option key={option} value={option}>{text}</option>)}
      </select>
      <p id={`${id}-help`} className="text-xs text-muted-foreground">{options.find(([option]) => option === value)?.[2]}</p>
    </div>
  )
}

/**
 * Edit the other delivery policy fields of one repository (#1346, part B2 of #1297). The server replaces the whole
 * policy on a save, so the save sends the current policy with only these fields changed. The required checks keep
 * their own editor. A stale revision is refused.
 */
export function DeliveryPolicyDialog({ scope, revisions, open, onOpenChange, refresh, onReturnFocus }: {
  scope: TeamGithubScope
  revisions: DeliveryRevisions
  open: boolean
  onOpenChange: (open: boolean) => void
  refresh: () => Promise<void>
  onReturnFocus: () => void
}) {
  const { policy, revision, locked, obsolete, isStale, reason, setReason, reasonTooLong, events, historyState, error, setError, saving, save, readAgain, tokenDialog } =
    useDeliveryPolicy({ scope, open, refresh, tokenReason: 'to read or change the delivery policy', revisions })
  // Fields that the user has not touched follow the server, so a background refresh never erases a draft.
  const [draft, setDraft] = useState<{ base: number; values: Values } | null>(null)
  useEffect(() => {
    if (open) queueMicrotask(() => setDraft(null)) // a new opening starts without the old draft
  }, [open])

  const values = draft?.values ?? (policy ? toValues(policy) : null)
  const stale = isStale(draft?.base)
  const disabled = saving || locked || !policy
  const check = values ? validate(values, policy?.required_checks.length ?? 0) : null
  const set = <K extends keyof Values>(key: K, value: Values[K]) => {
    if (!values) return
    setDraft({ base: draft?.base ?? revision ?? 0, values: { ...values, [key]: value } })
  }

  const submit = async () => {
    if (!policy || revision === undefined || !draft || !check) return
    setError(null)
    if (stale) { setError('The policy changed on the server after you started. Discard your changes to see the current policy.'); return }
    if (!check.valid) { setError('Fix the problems in the form first.'); return }
    const next = {
      ...policy, ...check.parsed,
      broad_checks: draft.values.broad_checks, accepted_base_update: draft.values.accepted_base_update, owner_contact: draft.values.owner_contact,
      caller_inventory_required: draft.values.caller_inventory_required, regression_proof_required: draft.values.regression_proof_required,
    } as DeliveryPolicy
    if ((Object.keys(FIELD_LABELS) as (keyof typeof FIELD_LABELS)[]).every((field) => next[field] === policy[field])) { setError('No change to save.'); return }
    await save({ base: draft.base, next, onSaved: () => setDraft(null), success: 'Delivery policy saved' })
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className={`${MODAL_SIZES.MD} overflow-y-auto grid-cols-[minmax(0,1fr)]`} onCloseAutoFocus={(event) => { event.preventDefault(); onReturnFocus() }}>
        <DialogHeader>
          <DialogTitle>Delivery policy</DialogTitle>
          <DialogDescription>
            {scope.repo_owner}/{scope.repo_name}. These choices apply to the next attempt. They grant no authority. The required checks have their own editor. The Director key is needed to read the history and to save.
          </DialogDescription>
        </DialogHeader>
        {error && <div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
        {!policy && <p role="alert" className="text-sm text-destructive">This server did not send the delivery policy, so it cannot be edited.</p>}
        {stale && (
          <div role="alert" className="flex flex-wrap items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">
            <span>{draft?.base !== revision ? `The policy changed on the server (revision ${draft?.base} to ${revision}). ` : 'The server refused the save because the policy changed. '}Your changes are not saved.</span>
            <Button variant="outline" size="sm" onClick={() => setDraft(null)}>Discard my changes</Button>
          </div>
        )}
        {locked && (
          <div role="alert" className="flex flex-wrap items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 p-3 text-sm">
            <span>The policy changed on the server (revision {obsolete} is out of date). The current policy is not available yet, so the form is read-only.</span>
            <Button variant="outline" size="sm" onClick={readAgain}>Read again</Button>
          </div>
        )}
        {values && check && (
          <div className="grid gap-4">
            <Choice id="policy-broad" name={FIELD_LABELS.broad_checks} value={values.broad_checks} options={BROAD} disabled={disabled} onChange={(value) => set('broad_checks', value)} />
            <Choice id="policy-base" name={FIELD_LABELS.accepted_base_update} value={values.accepted_base_update} options={BASE_UPDATE} disabled={disabled} onChange={(value) => set('accepted_base_update', value)} />
            <Choice id="policy-contact" name={FIELD_LABELS.owner_contact} value={values.owner_contact} options={CONTACT} disabled={disabled} onChange={(value) => set('owner_contact', value)} />
            {check.cross.map((message) => <p key={message} role="alert" className="text-xs text-destructive">{message}</p>)}
            {TIMERS.map(({ key, min, max, help }) => (
              <div key={key} className="grid gap-1">
                <Label htmlFor={`policy-${key}`}>{FIELD_LABELS[key]} (seconds, {min} to {max})</Label>
                <Input id={`policy-${key}`} inputMode="numeric" aria-invalid={Boolean(check.timers[key])} aria-describedby={`policy-${key}-help`} value={values[key]} disabled={disabled}
                  onChange={(event) => set(key, event.target.value)} />
                <p id={`policy-${key}-help`} className={check.timers[key] ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>{check.timers[key] ?? help}</p>
              </div>
            ))}
            {SWITCHES.map(({ key, help }) => (
              <div key={key} className="flex items-start gap-2 text-sm">
                <input id={`policy-${key}`} type="checkbox" className="mt-1" aria-describedby={`policy-${key}-help`} checked={values[key]} disabled={disabled} onChange={(event) => set(key, event.target.checked)} />
                <div>
                  <Label htmlFor={`policy-${key}`}>{FIELD_LABELS[key]}</Label>
                  <p id={`policy-${key}-help`} className="text-xs text-muted-foreground">{help}</p>
                </div>
              </div>
            ))}
            <div className="grid gap-2">
              <Label htmlFor="policy-reason">Reason</Label>
              <Input id="policy-reason" aria-invalid={reasonTooLong} value={reason} disabled={disabled} onChange={(event) => setReason(event.target.value)} />
              <p className={reasonTooLong ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>{reasonTooLong ? `The reason has at most ${MAX_REASON} characters.` : 'The reason is required. It is kept in the history with the new values.'}</p>
            </div>
            <Button onClick={() => void submit()} disabled={saving || !policy || !draft || stale}>Save</Button>
          </div>
        )}
        <DeliveryPolicyHistory events={events} state={historyState} />
        {tokenDialog}
      </DialogContent>
    </Dialog>
  )
}

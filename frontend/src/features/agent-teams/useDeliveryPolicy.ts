import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { ApiHttpError } from '@/lib/api'
import type { DeliveryPolicy, DeliveryPolicyEvent, TeamGithubScope } from '@/types/agentTeams'
import { fetchDeliveryPolicyHistory, updateTeamGithubDeliveryPolicy } from './api'
import { useOperatorTokenFlow } from './useOperatorTokenFlow'

export const MAX_REASON = 1000 // the server's limit for the reason of a policy change
// The server counts characters (Unicode code points), not UTF-16 units, so an emoji counts once.
export const charCount = (value: string) => Array.from(value).length

export type PolicyField = Exclude<keyof DeliveryPolicy, 'required_checks'>
/** The names that the history uses for the other fields. The editor shows the same names. */
export const FIELD_LABELS: Record<PolicyField, string> = {
  broad_checks: 'Broad checks',
  accepted_base_update: 'Accepted base update',
  owner_contact: 'Owner contact',
  owner_idle_seconds: 'Owner idle time',
  owner_nudge_grace_seconds: 'Owner nudge grace',
  owner_observation_wait_seconds: 'Owner observation wait',
  owner_observation_resume_seconds: 'Owner observation resume',
  checkpoint_delay_report_seconds: 'Checkpoint delay report',
  caller_inventory_required: 'Caller inventory required',
  regression_proof_required: 'Regression proof required',
}

/**
 * The revision logic that the delivery policy editors share (#1345, #1346). The server replaces the whole policy on
 * a save and refuses a stale `expected_revision`. The repository read can lag behind a save or a rejection, so the
 * hook compares every base revision with what it knows: the read, the last confirmed save and the last rejection.
 */
export function useDeliveryPolicy({ scope, open, refresh, tokenReason }: {
  scope: TeamGithubScope
  open: boolean
  refresh: () => Promise<void>
  tokenReason: string
}) {
  const { withOperatorToken, tokenDialog } = useOperatorTokenFlow(tokenReason)
  // The policy that the last confirmed save returned. It stands in until the repository read catches up with it.
  const [confirmed, setConfirmed] = useState<{ revision: number; policy: DeliveryPolicy } | null>(null)
  // The highest revision that a 409 proved obsolete. A read that still shows it, or an older one, is not current.
  const [obsolete, setObsolete] = useState(0)
  const confirmedNewer = confirmed !== null && (scope.delivery_policy_revision === undefined || confirmed.revision > scope.delivery_policy_revision)
  const policy = confirmedNewer ? confirmed.policy : scope.delivery_policy
  const revision = confirmedNewer ? confirmed.revision : scope.delivery_policy_revision
  const [reason, setReason] = useState('')
  const [events, setEvents] = useState<DeliveryPolicyEvent[]>([])
  const [historyState, setHistoryState] = useState<'loading' | 'ready' | 'failed'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const generation = useRef(0) // a newer read, or closing the dialog, makes older answers obsolete
  const session = useRef(0) // a save from an older open of the dialog changes nothing in the new one

  const locked = revision !== undefined && revision <= obsolete // the current policy has not been read yet
  /** A draft is stale when its base revision is not the current one, or the server already refused it. */
  const isStale = (base: number | undefined) => base !== undefined && (base !== revision || base <= obsolete)
  const reasonTooLong = charCount(reason.trim()) > MAX_REASON

  const loadHistory = useCallback(async () => {
    generation.current += 1
    const mine = generation.current
    try {
      const page = await withOperatorToken((token) => fetchDeliveryPolicyHistory(scope.id, token))
      if (mine !== generation.current) return
      setEvents(page.events)
      setHistoryState('ready')
    } catch (failure) {
      if (mine !== generation.current) return
      setHistoryState('failed')
      setError(`Could not read the history: ${failure instanceof Error ? failure.message : 'unknown error'}`)
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
      setReason(''); setEvents([]); setHistoryState('loading'); setError(null); setSaving(false) // the confirmed policy and the obsolete revision stay: the repository read may still lag
      void loadHistory()
    })
  }, [open, loadHistory])

  /** Send the whole policy. `onSaved` clears the caller's draft, and only for the dialog opening that started the save. */
  const save = async ({ base, next, onSaved, success }: { base: number; next: DeliveryPolicy; onSaved: () => void; success: string }) => {
    setError(null)
    if (!reason.trim()) { setError('Enter a reason for the change.'); return }
    if (reasonTooLong) { setError(`The reason has at most ${MAX_REASON} characters.`); return }
    const mine = session.current
    setSaving(true)
    try {
      const saved = await withOperatorToken((token) => updateTeamGithubDeliveryPolicy(scope.id, { expected_revision: base, policy: next, reason: reason.trim() }, token))
      // The confirmed policy and revision belong to the repository, so they count even when the dialog was reopened.
      // They stand in until the repository read shows them.
      setConfirmed((current) => (current && current.revision >= saved.revision ? current : { revision: saved.revision, policy: saved.policy }))
      if (mine !== session.current) { void refresh().catch(() => undefined); return }
      onSaved(); setReason('')
      await refresh()
      if (mine !== session.current) return
      await loadHistory()
      toast.success(success)
    } catch (failure) {
      const rejected = failure instanceof ApiHttpError && failure.status === 409
      if (rejected) setObsolete((current) => Math.max(current, base)) // a fact about the repository, not about this opening
      if (mine !== session.current) return
      if (rejected) {
        setError('The policy changed on the server, so nothing was saved. Discard your changes to see the current list.')
        void refresh().catch(() => undefined)
      } else {
        setError(failure instanceof Error ? failure.message : 'Failed to save the delivery policy')
      }
    } finally {
      if (mine === session.current) setSaving(false)
    }
  }

  const readAgain = () => { void refresh().catch(() => undefined) }
  return { policy, revision, locked, obsolete, isStale, reason, setReason, reasonTooLong, events, historyState, error, setError, saving, save, readAgain, tokenDialog }
}

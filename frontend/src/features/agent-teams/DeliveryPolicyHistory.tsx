import { formatServerTime } from '@/lib/serverTime'
import type { DeliveryPolicy, DeliveryPolicyEvent, DeliveryRequiredCheck } from '@/types/agentTeams'
import { FIELD_LABELS, type PolicyField } from './useDeliveryPolicy'

const same = (a: DeliveryRequiredCheck, b: DeliveryRequiredCheck) => a.name === b.name && a.app_slug === b.app_slug
const label = (check: DeliveryRequiredCheck) => `${check.name} (${check.app_slug})`

const SECONDS = new Set<PolicyField>(['owner_idle_seconds', 'owner_nudge_grace_seconds', 'owner_observation_wait_seconds', 'owner_observation_resume_seconds', 'checkpoint_delay_report_seconds'])

function show(field: PolicyField, value: DeliveryPolicy[PolicyField]): string {
  if (value === null) return 'not set'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return SECONDS.has(field) ? `${value} s` : String(value)
}

/** What one event changed, compared with the event before it. The oldest event has nothing before it, so it shows the whole policy. */
function describe(event: DeliveryPolicyEvent, previous: DeliveryPolicyEvent | undefined): string[] {
  const now = event.policy.required_checks
  if (!previous) return [`Required checks: ${now.length ? now.map(label).join(', ') : 'none'}`, ...(Object.keys(FIELD_LABELS) as PolicyField[]).map((field) => `${FIELD_LABELS[field]}: ${show(field, event.policy[field])}`)]
  const before = previous.policy.required_checks
  const lines = [
    ...now.filter((check) => !before.some((old) => same(old, check))).map((check) => `Added: ${label(check)}`),
    ...before.filter((old) => !now.some((check) => same(old, check))).map((check) => `Removed: ${label(check)}`),
  ]
  if (lines.length === 0 && now.length === before.length && now.some((check, index) => !same(check, before[index]))) lines.push('Order changed')
  for (const field of Object.keys(FIELD_LABELS) as PolicyField[]) {
    if (event.policy[field] !== previous.policy[field]) lines.push(`${FIELD_LABELS[field]}: ${show(field, previous.policy[field])} to ${show(field, event.policy[field])}`)
  }
  if (lines.length === 0) lines.push('No field changed')
  return lines
}

/** The recorded changes of one repository's delivery policy, newest first. The server keeps the latest 100. */
export function DeliveryPolicyHistory({ events, state }: { events: DeliveryPolicyEvent[]; state: 'loading' | 'ready' | 'failed' }) {
  return (
    <section aria-label="History" className="space-y-2">
      <h4 className="text-sm font-semibold">History</h4>
      {state === 'ready' && events.length === 0 && <p className="text-sm text-muted-foreground">No changes recorded yet.</p>}
      {state === 'failed' && <p className="text-sm text-muted-foreground">The history is not available.</p>}
      <ul className="space-y-2 text-sm">
        {events.map((event, index) => (
          <li key={event.id} className="rounded-md border p-2">
            <p className="text-xs text-muted-foreground">Revision {event.revision} · {formatServerTime(event.created_at)}{event.actor ? ` · ${event.actor}` : ''}</p>
            {event.work_item_id !== null && <p>Applied to attempt {event.work_item_id}</p>}
            {event.work_item_id === null && describe(event, events[index + 1]).map((line) => <p key={line} className="break-words">{line}</p>)}
            {event.reason && <p className="break-words text-muted-foreground">{event.reason}</p>}
          </li>
        ))}
      </ul>
      {state === 'ready' && events.length >= 100 && <p className="text-xs text-muted-foreground">The server keeps the latest 100 changes in this list.</p>}
    </section>
  )
}

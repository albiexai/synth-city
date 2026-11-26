import { formatServerTime, parseServerTime } from '@/lib/serverTime'
import type { GithubWorkItem } from '@/types/agentTeams'

export function hasLiveQueuedHandoff(item: GithubWorkItem) {
  return item.handoff_state === 'queued' && item.handoff_target_slot_id != null
    && !!item.handoff_expires_at && parseServerTime(item.handoff_expires_at).getTime() > Date.now()
}

export function QueuedHandoffDetails({ item, targetName }: { item: GithubWorkItem; targetName?: string }) {
  const elapsed = item.handoff_queued_at
    ? Date.now() - parseServerTime(item.handoff_queued_at).getTime()
    : NaN
  const age = Number.isFinite(elapsed) ? `${Math.max(0, Math.floor(elapsed / 60_000))} min ago` : 'Unavailable'
  return (
    <dl className="flex flex-wrap gap-x-5 gap-y-2 text-sm">
      <div className="min-w-0"><dt className="text-muted-foreground">Target</dt><dd className="wrap-anywhere">{targetName ?? `Slot ${item.handoff_target_slot_id}`}</dd></div>
      <div><dt className="text-muted-foreground">Queued</dt><dd>{age}</dd></div>
      {item.handoff_expires_at && (
        <div><dt className="text-muted-foreground">Expires</dt><dd>{formatServerTime(item.handoff_expires_at, { dateStyle: 'medium', timeStyle: 'short' })}</dd></div>
      )}
    </dl>
  )
}

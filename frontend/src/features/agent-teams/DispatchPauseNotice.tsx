import type { AgentTeamPreset } from '@/types/agentTeams'

export type DispatchPauseState = Pick<AgentTeamPreset, 'dispatch_paused' | 'dispatch_pause_reason' | 'dispatch_paused_by'>

export function DispatchPauseNotice({ pause }: { pause: DispatchPauseState }) {
  if (!pause.dispatch_paused) return null
  return (
    <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
      <strong className="block">Dispatch paused</strong>
      <div className="[overflow-wrap:anywhere]">
        {pause.dispatch_pause_reason}
        {pause.dispatch_paused_by === 'operator' && ' · Set by Operator'}
      </div>
    </div>
  )
}

import type { TeamSummaryAttention, TeamSummaryCoverageReason, TeamSummaryHumanAction, UnansweredRequestReason } from '@/types/agentTeams'

// Plain words for the team summary, shared by the team card and the Overview.
export const ATTENTION_TEXT: Record<TeamSummaryAttention['code'], string> = {
  attempt_stopped: 'Attempt stopped',
  recovery_decision: 'Recovery decision needed',
  human_review: 'Human review needed',
  owner_stalled: 'Owner stalled',
  milestone_routing_conflict: 'Multiple teams claim the same milestone for this repository',
  team_idle: 'Team idle',
  observation_pause: 'Observation paused',
  hold: 'Team on HOLD',
  recovery_only: 'Team in recovery-only mode',
  hold_unavailable: 'HOLD state unknown',
  unanswered_request: 'Unanswered request',
  unanswered_request_incomplete: 'Unanswered-request view incomplete',
}

// Plain words for the fixed reason a view of unanswered requests is not complete (#752).
export const COVERAGE_REASON_TEXT: Record<UnansweredRequestReason, string> = {
  candidate_limit: 'more candidates than the view reads',
  native_unknown: "a member's activity is unknown or not readable",
  stall_timeout: 'the unanswered-request observation timed out',
  inputs_changing: 'member observations kept changing during the read',
  fence_changed: 'an addressed member’s binding changed during the read',
  ambiguous_legacy_request: 'an older nested request cannot be matched to a reply',
  reply_scan_limit: 'the reply check reached its limit',
  entry_limit: 'more unanswered requests than are listed',
}

// Any other value shows a fixed fallback, never text from the server.
export function coverageReasonText(reason: string | null | undefined) {
  return COVERAGE_REASON_TEXT[reason as UnansweredRequestReason] ?? 'the view could not be proven'
}

export function unansweredRequestText(item: TeamSummaryAttention) {
  const minutes = Math.floor(Math.max(0, item.unanswered_seconds ?? 0) / 60)
  return `${item.recipient ?? 'A member'} has not answered ${item.requester ?? 'a team member'}'s request for ${minutes} min`
}

export function incompleteRequestText(item: TeamSummaryAttention) {
  const more = (item.omitted_count ?? 0) > 0 ? ` · ${item.omitted_count} more not listed` : ''
  return `${coverageReasonText(item.coverage_reason)}${more}`
}

const TEAM_COVERAGE_TEXT: Record<TeamSummaryCoverageReason['code'], string> = {
  work_item_limit: 'The work item read reached its limit.',
  scope_limit: 'The repository read reached its limit.',
  approval_limit: 'The approval read reached its limit.',
  native_unknown: 'Member activity is unknown.',
  dispatch_incomplete: 'Dispatch observations are incomplete.',
  pause_limit: 'The observation pause read reached its limit.',
  human_actions_incomplete: 'Human action observations are incomplete.',
  human_action_invalid: 'A human action could not be read.',
  human_action_limit: 'The human action read reached its limit.',
  progress_missing: 'Work progress is unavailable.',
  observation_timeout: 'An observation timed out.',
  observation_failed: 'An observation could not be read.',
  fence_changed: 'A member binding changed during the read.',
  source_changed: 'Team or work state changed during the read.',
  window_expired: 'An observation expired before the summary was ready.',
}

const TEAM_ACTIVITY_TEXT: Partial<Record<NonNullable<TeamSummaryCoverageReason['activity_reason']>, string>> = {
  restart_needed_for_observation: 'Restart in resume mode through Synth City to observe this session.',
  no_native_event: 'Waiting for a supported native turn event.',
  completion_unconfirmed: 'Waiting for confirmation that the turn ended.',
  no_current_binding: 'The workspace owner has no current activity binding.',
  native_log_unavailable: 'The native activity log is unavailable.',
  native_log_unreadable: 'The native activity log could not be read.',
  ambiguous_binding: 'More than one activity binding could apply.',
  duplicate_native_identity: 'More than one slot uses the same native session.',
}

const TEAM_COVERAGE_SOURCE: Record<NonNullable<TeamSummaryCoverageReason['source']>, string> = {
  native: 'Member activity',
  dispatch: 'Dispatch',
  pauses: 'Observation pauses',
  human_actions: 'Human actions',
  progress: 'Work progress',
}

export function teamCoverageReasonText(reason: TeamSummaryCoverageReason, slotNames?: Record<number, string>) {
  const slot = reason.slot_id ? slotNames?.[reason.slot_id] ?? `Slot ${reason.slot_id}` : null
  const item = reason.work_item_id ? `Work item ${reason.work_item_id}` : null
  const source = reason.source ? TEAM_COVERAGE_SOURCE[reason.source] : undefined
  const detail = reason.activity_reason ? TEAM_ACTIVITY_TEXT[reason.activity_reason]
    : source && reason.code === 'observation_timeout' ? `${source} read timed out.`
    : source && reason.code === 'observation_failed' ? `${source} could not be read.`
    : undefined
  return [slot ?? item, detail ?? TEAM_COVERAGE_TEXT[reason.code] ?? 'An observation is incomplete.']
    .filter(Boolean).join(' · ')
}

export const ACTION_TEXT: Record<TeamSummaryHumanAction['kind'], string> = {
  review_pr: 'Review the PR',
  merge_pr: 'Merge the PR',
  inspect_attempt: 'Inspect the attempt',
  inspect_checkpoint: 'Inspect the checkpoint',
  milestone_acceptance: 'Accept the milestone',
  pilot_decision: 'Make the pilot decision',
  scope_clarification: 'Clarify the scope',
  provide_evidence: 'Provide evidence',
}

const escalationReasonLabels: Record<string, string> = {
  plan_blocked: 'Plan blocked',
  launch_outcome_unknown: 'Session launch outcome unknown',
  approval_rounds_exhausted: 'Approval rounds exhausted',
  leader_offline: 'Leader offline',
  owner_offline: 'Owner offline',
  brief_unread: 'Owner has not read the brief',
  leader_ack_timeout: 'Leader acknowledgement timed out',
  owner_idle_timeout: 'Owner idle timeout',
  owner_observation_unavailable: 'Owner observation unavailable',
  integration_update_conflict: 'Integration update needs coordination',
  retry_count_exhausted: 'Verification retries exhausted',
  continuation_revision_exhausted: 'Continuation revision exhausted',
  continuation_budget_exhausted: 'Continuation budget exhausted',
  continuation_invalid_state: 'Continuation state invalid',
  continuation_pr_identity_invalid: 'Continuation PR identity invalid',
  dispatch_label_removed: 'Dispatch label removed',
  abandoned_by_operator: 'Abandoned by the Director',
  prepared_owner_unavailable: 'Prepared owner unavailable',
  pr_closed_unmerged: 'PR closed without merge',
}

export function escalationReasonLabel(reason?: string | null) {
  if (!reason) return 'Unknown reason'
  return escalationReasonLabels[reason] ?? reason.replaceAll('_', ' ')
}

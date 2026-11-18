import type { TeamSummaryAttention, TeamSummaryHumanAction, UnansweredRequestReason } from '@/types/agentTeams'

// Plain words for the team summary, shared by the team card and the Overview.
export const ATTENTION_TEXT: Record<TeamSummaryAttention['code'], string> = {
  attempt_stopped: 'Attempt stopped',
  recovery_decision: 'Recovery decision needed',
  human_review: 'Human review needed',
  owner_stalled: 'Owner stalled',
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

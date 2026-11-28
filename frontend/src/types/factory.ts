// Generated from accepted P01 schema1; provenance: tests/fixtures/factory/PROVENANCE.md.
import type { AgentStatus } from './agentStatus';
import type { RequiredCIUnreadableSignal } from './agentTeams';
export type FactoryErrorDetail = {
  code:
    | "invalid_filter"
    | "invalid_cursor"
    | "resource_not_found"
    | "projection_failed";
  message: string;
};

export type FactoryErrorResponse = {
  detail: FactoryErrorDetail;
};

export type AutomationSummary = {
  configured_scopes: number;
  enabled_scopes: number;
  intake_blocked_scopes: number;
  intake_eligible_scopes: number;
  intake_unknown_scopes: number;
  never_polled_scopes: number;
  paused_scopes: number;
  runtime: RuntimeObservation;
  stale_scopes: number;
};

export type FactoryCounts = {
  active: number;
  attention: number;
  finished: number;
  queued: number;
  review: number;
  total: number;
  unknown: number;
};

export type FactoryFilters = {
  provider: string | null;
  scope_id: number | null;
  team_id: number | null;
};

export type RuntimeObservation = {
  mode: "normal" | "recovery_only" | "unknown";
  observed_at: string | null;
  reason_code: string | null;
  scheduler_state: "running" | "stopped" | "unknown";
};

export type OverviewResponse = {
  automation: AutomationSummary;
  counts: FactoryCounts;
  filters: FactoryFilters;
  generated_at: string;
  schema_version: 1;
};

export type GithubIdentity = {
  name: string;
  owner: string;
};

export type IntakeProjection = {
  reason_code: string;
  state: "eligible" | "blocked" | "unknown";
  summary: string;
};

export type OverlapObservation = {
  other_scope_ids: number[];
  state: "none" | "warning" | "unknown";
};

export type PollObservation = {
  freshness: "fresh" | "stale" | "never_polled" | "suspended" | "unknown";
  interval_seconds: number;
  last_polled_at: string | null;
};

export type RepositoryProjection = {
  configured_enabled: boolean;
  github: GithubIdentity;
  intake: IntakeProjection;
  overlap: OverlapObservation;
  poll: PollObservation;
  scope_enabled: boolean;
  scope_id: number;
  team: TeamReference;
  team_automation_enabled: boolean;
};

export type TeamReference = {
  id: number;
  name: string;
};

export type RepositoryDetailResponse = {
  generated_at: string;
  repository: RepositoryProjection;
  schema_version: 1;
};

export type RepositoryListResponse = {
  filters: FactoryFilters;
  generated_at: string;
  has_more: boolean;
  next_cursor: string | null;
  repositories: RepositoryProjection[];
  schema_version: 1;
  total: number;
};

export type ApproverReference = {
  member_id: number | null;
  slot_id: number | null;
  source: "first_enabled_slot" | "explicit_assignment" | "unknown";
};

export type BridgeTarget = {
  member_id: number;
  session_id: number;
  slot_id: number;
  team_id: number;
};

export type FactoryAction = {
  block_code: string | null;
  name:
    | "retry"
    | "resume_attempt"
    | "escalate_attempt"
    | "cancel_continuation_request"
    | "cancel_active_revision"
    | "release_recovery_checkpoint";
  reason: string;
  required_actor: "leader" | "owner" | "operator" | "unknown";
  state: "eligible" | "blocked" | "unknown";
};

export type LaunchPlanHint = {
  slot_id: number;
  team_id: number;
};

export type LinkHints = {
  bridge_target: BridgeTarget | null;
  launch_plan: LaunchPlanHint | null;
  mail: MailHint | null;
};

export type MailHint = {
  member_id: number;
  slot_id: number;
  team_id: number;
};

export type OwnerReference = {
  configured_provider: string | null;
  member_id: number | null;
  name: string;
  provider_label: string | null;
  slot_id: number;
};

export type SafeWorkItem = {
  difficulty_provenance?: {
    difficulty: "low" | "medium" | "high";
    source: "leader" | "director" | "supervisor" | "github";
    actor_reference: string | null;
  };
  active_scope_revision: number;
  active_scope_status: string | null;
  approval_round_count: number;
  attempt_phase: string;
  created_at: string;
  diagnostic_retry_count: number;
  dispatch_status: string;
  github_updated_at: string;
  id: number;
  issue_number: number;
  issue_title: string;
  issue_type: string;
  last_verified_sha: string | null;
  checks_unavailable_since?: string | null;
  checks_unavailable_reason?: RequiredCIUnreadableSignal['reason'] | null;
  required_ci_unreadable?: RequiredCIUnreadableSignal | null;
  pending_approval_kind: string | null;
  pending_approval_status: string | null;
  pr_number: number | null;
  retry_count: number;
  scope_id: number;
  scope_default_effort_limit_minutes?: number | null;
  plan_estimate_minutes?: number | null;
  plan_limit_minutes?: number | null;
  plan_refusal?: PlanRefusal | null;
  updated_at: string;
};

export type PlanRefusal = {
  code: "plan_estimate_exceeds_limit";
  estimate_minutes: number;
  limit_minutes: number;
  split_suggestion: string;
};

export type SessionAssociation = {
  /** Display facts for the member (#1073). The strict state below stays what it is; these only add evidence. */
  agent_status?: AgentStatus | null;
  bridge_target: BridgeTarget | null;
  observed_provider: string | null;
  state: "bound" | "offline" | "ambiguous" | "unknown";
};

export type WaitingActor = {
  actor: "owner" | "leader" | "operator" | "human" | "unknown";
  reason_code: string;
  summary: string;
};

export type WorkPolicy = {
  continuation_enabled: boolean;
  max_approval_rounds: number;
  max_verification_retries: number;
  merge_policy: string;
};

export type WorkProjection = {
  actions: FactoryAction[];
  approver: ApproverReference | null;
  approver_session: SessionAssociation;
  category:
    "queued" | "active" | "review" | "attention" | "finished" | "unknown";
  item: SafeWorkItem;
  links: LinkHints;
  owner: OwnerReference | null;
  owner_queue_position?: number;
  policy: WorkPolicy;
  repository: WorkRepository;
  session: SessionAssociation;
  team: TeamReference;
  waiting: WaitingActor | null;
  workspace: WorkspaceAssociation | null;
};

export type WorkRepository = {
  display_name: string;
  github: GithubIdentity;
  scope_id: number;
};

export type WorkspaceAssociation = {
  id: number;
  state: "leased" | "released" | "unknown";
};

export type WorkDetailResponse = {
  generated_at: string;
  schema_version: 1;
  work_item: WorkProjection;
  initial_path_comparison?: {
    head_sha: string;
    base_sha: string;
    in_scope: string[];
    approved_companions: string[];
    outside_scope: string[];
    note: string | null;
  };
};

export type WorkFilters = {
  owner_slot_id?: number;
  category:
    | "all"
    | "open"
    | "queued"
    | "active"
    | "review"
    | "attention"
    | "finished"
    | "unknown";
  provider: string | null;
  scope_id: number | null;
  team_id: number | null;
};

export type WaitingIssue = {
  issue_number: number;
  milestone: string;
  observed_at: string;
  // A wait without a repository stamp has no issue link.
  repository: WorkRepository | null;
};

export type DeferredIssue = {
  deferred_at: string;
  issue_number: number;
  issue_title: string;
  previous_branch: string | null;
  repository: WorkRepository;
  target_milestone: string | null;
  team: TeamReference;
  work_item_id: number;
};

export type MilestoneStatesResponse = {
  deferred: DeferredIssue[];
  deferred_truncated: boolean;
  filters: WorkFilters;
  generated_at: string;
  schema_version: 1;
  waiting: WaitingIssue[];
  waiting_truncated: boolean;
};

export type WorkListResponse = {
  counts: FactoryCounts;
  filters: WorkFilters;
  generated_at: string;
  has_more: boolean;
  items: WorkProjection[];
  next_cursor: string | null;
  schema_version: 1;
  total: number;
};

// #1252: the campaigns list (GET /factory/campaigns). A count that is not recorded is null with a reason, never 0.
export type CampaignStatus = "active" | "finishing" | "finished";
// ``coverage``, ``unknown_count`` and ``reasons`` are the snapshot's own: a known value can still be partial.
export type CampaignMeasure = {
  value: number | null;
  reason: string | null;
  coverage?: string | null;
  unknown_count?: number | null;
  reasons?: string[];
};
export type CampaignRepositoryCount = { repo: string; issues: number | null; reason: string | null };
export type CampaignRow = {
  id: number;
  team: TeamReference | null;
  selector: string;
  status: CampaignStatus;
  started_at: string;
  finished_at: string | null;
  merged: CampaignMeasure;
  in_scope: CampaignMeasure;
  repositories: CampaignRepositoryCount[];
  repositories_unknown_reason: string | null;
  // ``as_of`` is the cutoff that the counts describe. ``frozen_at`` is when a frozen version was stored.
  source: { kind: "frozen" | "live"; as_of: string; frozen_at?: string | null } | null;
};
// #1256: GET /factory/campaigns/{id}/health. A number that is not recorded is null with a reason, never 0.
export type HealthCoverage = "full" | "partial" | "unavailable";
export type HealthItemTimeBand = {
  band: string;
  items: number;
  timed: number;
  median_seconds: number | null;
  p85_seconds: number | null;
  small_sample: boolean;
  coverage: string;
  unknown_reasons: string[];
};
export type HealthItemTime = {
  items: number;
  timed: number;
  coverage: string;
  unknown_reasons: string[];
  bands: HealthItemTimeBand[];
};
export type HealthCost = {
  value_usd: number | null;
  coverage: HealthCoverage;
  members_total: number | null;
  members_known: number | null;
  unknown_members: { member: string; reason: string }[];
  prices_as_of: string | null;
  reasons: string[];
};
export type HealthBottleneckStage = {
  key: string;
  group: "waiting" | "process";
  stage: string;
  actor: string;
  credited_seconds: number;
  share: number;
  items: number;
  median_seconds: number;
  small_sample: boolean;
  coverage: HealthCoverage;
};
export type HealthBottleneckWindow = {
  window: "whole_campaign" | "last_four_hours";
  label: string;
  started_at: string;
  as_of: string;
  coverage: HealthCoverage;
  unknown_reasons: string[];
  items: number | null;
  credited_seconds: number | null;
  state: "ranked" | "below_thresholds" | "unknown";
  stages: HealthBottleneckStage[];
  largest_observed: HealthBottleneckStage | null;
};
export type CampaignHealthResponse = {
  schema_version: 1;
  campaign_id: number;
  source: "live" | "frozen" | "unavailable";
  as_of: string | null;
  item_time: HealthItemTime | null;
  item_time_reason: string | null;
  cost: HealthCost;
  bottleneck: HealthBottleneckWindow | null;
  bottleneck_reason: string | null;
};
export type CampaignListResponse = {
  schema_version: 1;
  generated_at: string;
  campaigns: CampaignRow[];
  total: number;
  counts: { live: number; finished: number };
  hidden_unknown_repositories: number;
  finished_before_recording: number;
  has_more: boolean;
  next_cursor: string | null;
};

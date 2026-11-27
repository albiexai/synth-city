import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { ApiHttpError, apiClient } from "@/lib/api";
import {
  createAgentTeamPreset,
  createTeamGithubScope,
  fetchAgentTeamPresets,
  fetchConfigurationObservation,
  fetchTeamGithubScopes,
  launchAgentTeam,
  planAgentTeamLaunch,
  updateAgentTeamLeader,
  updateAgentTeamPreset,
  updateTeamGithubScope,
} from "@/features/agent-teams/api";
import { clearOperatorToken, getOperatorToken, setOperatorToken } from "@/features/agent-teams/operatorAuth";
import { OperatorTokenDialog } from "@/features/agent-teams/AutonomyPanel";
import { useOptionalProjectContext } from "@/contexts/ProjectContext";
import type { AgentTeamPreset, AgentTeamPresetInput, AgentTeamLaunchPlan, AgentTeamSlotInput, TeamGithubMergePolicy, TeamGithubScope } from "@/types/agentTeams";
import { CharterField } from "./CharterField";
import { charterTemplateValues, findUnresolved, type CharterRole } from "./charterTemplates";
import { AUTO_MERGE_OWNER_REQUIREMENT, AUTO_MERGE_VERIFIER_REQUIREMENT, autoMergeSetupBlocker, VERIFIER_ROLE } from "./autoMergeReadiness";

// A slot key maps to its route and, since #1309, its charter text. The Leader and verifier keys hold only a charter.
type SlotRoute = { areaLabels: string; expertise: string; charter?: string };
// Leader and verifier charters sit under keys that no worker name can equal: names are single trimmed lines,
// so they never hold a newline. A worker named "Verifier" cannot take the verifier's text.
const LEADER_KEY = "\nleader";
const VERIFIER_KEY = "\nverifier";
type Check = { status: "ready" | "blocked" | "unknown"; code: string; remedy: string };
type Preflight = { status: "ready" | "blocked" | "unknown"; observed_at: string; checked_at?: string; checks: Record<string, Check>; configuration_presence?: Record<string, boolean>; host_guidance?: string[] };
type Draft = {
  owner: string; repo: string; path: string; dispatch: string; design: string; baseRef: string;
  authMode: "token" | "github_app"; teamMode: "existing" | "new"; teamId: number | "";
  teamName: string; leaderName: string; workerNames: string; provider: string;
  mergePolicy: TeamGithubMergePolicy;
};
type Recovery = {
  kind: "team" | "scope";
  candidates: Array<{ label: string; id: number }>;
  requestedDraft?: Draft;
  requestedRouting?: Record<string, SlotRoute>;
  // Complete requested record identity: the record the save asked for, and
  // whether an older record was confirmed and preserved as explicit partial
  // state after the intent changed.
  requestedRecord?: { kind: "team" | "scope"; id: number | null; confirmed: boolean };
  requestedIntentKey?: symbol | string | number;
} | null;
type ScopeOverlap = { key: string; first: TeamGithubScope; second: TeamGithubScope; active: boolean };
type UncertainLaunch = { slotIds: number[]; planHash: string } | null;
const initial: Draft = {
  owner: "", repo: "", path: "", dispatch: "albiex-synth-city-ready", design: "albiex-synth-city-design", baseRef: "origin/HEAD",
  authMode: "token", teamMode: "new", teamId: "", teamName: "", leaderName: "Leader", workerNames: "Worker",
  provider: "codex-cli", mergePolicy: "human",
};
const labelClass = "block text-sm font-medium";
const inputClass = "mt-1 w-full rounded-md border bg-background px-3 py-2";
const panelClass = "space-y-4 rounded-lg border bg-card p-4";
const overlapWarning = "This can dispatch the same issue more than once";
const DEFAULT_AUTO_MERGE_CAP = 5;

// Owner and name from a GitHub URL, an SSH URL or "owner/name".
function parseRepository(text: string): { owner: string; repo: string } | null {
  const match = /^(?:https?:\/\/(?:www\.)?github\.com\/|git@github\.com:|ssh:\/\/git@github\.com\/|github\.com\/)?([A-Za-z0-9_.-]+)\/([A-Za-z0-9_.-]+?)(?:\.git)?\/?$/.exec(text.trim());
  return match ? { owner: match[1], repo: match[2] } : null;
}

const OTHER = "__other";

function normalized(value: Draft) {
  return JSON.stringify({
    owner: value.owner.trim().toLowerCase(), repo: value.repo.trim().toLowerCase(), path: value.path.trim(),
    dispatch: value.dispatch.trim(), design: value.design.trim(), baseRef: value.baseRef.trim(), authMode: value.authMode,
  });
}

function scopePreflightInput(scope: TeamGithubScope) {
  return {
    repo_owner: scope.repo_owner,
    repo_name: scope.repo_name,
    repo_path: scope.repo_path,
    dispatch_label: scope.dispatch_label,
    design_label: scope.design_label,
    dispatch_auth_mode: scope.github_auth_mode === "app" ? "github_app" : "token",
    base_ref: scope.base_ref,
  };
}

function scopeMatchesDraft(scope: TeamGithubScope, draft: Draft) {
  return scope.repo_owner.toLowerCase() === draft.owner.trim().toLowerCase() &&
    scope.repo_name.toLowerCase() === draft.repo.trim().toLowerCase() &&
    scope.repo_path === draft.path.trim() &&
    scope.dispatch_label === draft.dispatch.trim() &&
    scope.design_label === draft.design.trim() &&
    scope.base_ref === draft.baseRef.trim() &&
    scope.github_auth_mode === (draft.authMode === "github_app" ? "app" : "ambient") &&
    (scope.merge_policy ?? "human") === draft.mergePolicy &&
    (draft.mergePolicy === "human" || scope.max_auto_merges_per_day === DEFAULT_AUTO_MERGE_CAP);
}

function buildActivationSnapshot(scopes: TeamGithubScope[], teams: AgentTeamPreset[], team: AgentTeamPreset) {
  return JSON.stringify({
    teamId: team.id,
    teamAutonomy: team.autonomy_enabled,
    teams: teams.map((item) => [item.id, item.autonomy_enabled, item.leader_slot_id,
      item.slots.map((slot) => [slot.id, slot.enabled, slot.provider, slot.repo_path, slot.role, slot.area_labels ?? [], slot.expertise ?? null])
        .sort((a, b) => Number(a[0]) - Number(b[0]))])
      .sort((a, b) => Number(a[0]) - Number(b[0])),
    scopes: scopes.map((item) => [
      item.id, item.preset_id, item.enabled, item.repo_owner.toLowerCase(), item.repo_name.toLowerCase(),
      item.repo_path, item.base_ref, item.dispatch_label, item.design_label, item.github_auth_mode,
      item.github_auth_configured, item.github_poll_token_configured,
      item.merge_policy, item.max_auto_merges_per_day,
    ]).sort((a, b) => Number(a[0]) - Number(b[0])),
  });
}

function findActivationOverlaps(
  targets: TeamGithubScope[], scopes: TeamGithubScope[], teams: AgentTeamPreset[],
): ScopeOverlap[] {
  const targetIds = new Set(targets.map((item) => item.id));
  if (!targetIds.size) return [];
  const activeTeamIds = new Set(teams.filter((item) => item.autonomy_enabled).map((item) => item.id));
  const future = new Map<number, TeamGithubScope>();
  for (const item of scopes) if (item.enabled && activeTeamIds.has(item.preset_id)) future.set(item.id, item);
  for (const item of targets) future.set(item.id, item);
  const candidates = [...future.values()].sort((a, b) => a.id - b.id);
  const overlaps: ScopeOverlap[] = [];
  for (let i = 0; i < candidates.length; i += 1) {
    for (let j = i + 1; j < candidates.length; j += 1) {
      const first = candidates[i];
      const second = candidates[j];
      if (!targetIds.has(first.id) && !targetIds.has(second.id)) continue;
      if (first.repo_owner.toLowerCase() !== second.repo_owner.toLowerCase() ||
          first.repo_name.toLowerCase() !== second.repo_name.toLowerCase() ||
          first.dispatch_label !== second.dispatch_label) continue;
      // An active collision exists now. A prospective overlap appears after this action.
      const active = first.enabled && second.enabled &&
        activeTeamIds.has(first.preset_id) && activeTeamIds.has(second.preset_id);
      overlaps.push({ key: `${first.id}:${second.id}`, first, second, active });
    }
  }
  return overlaps;
}

function overlapDescription(overlap: ScopeOverlap) {
  const { first, second } = overlap;
  return `${first.repo_owner}/${first.repo_name} label ${first.dispatch_label}: team ${first.preset_id}/scope ${first.id} and team ${second.preset_id}/scope ${second.id}`;
}

function parseAreaLabels(text: string) {
  return text.split(",").map((item) => item.trim()).filter(Boolean);
}

// A charter is sent only when it has text, so a setup without charters sends the same payload as before.
function charterOf(routing: Record<string, SlotRoute>, name: string) {
  const text = routing[name]?.charter;
  return text && text.trim() ? { charter: text } : {};
}

function draftTeamSlots(draft: Draft, routing: Record<string, SlotRoute>): AgentTeamSlotInput[] {
  const base = { provider: draft.provider, repo_path: draft.path.trim(), enabled: true };
  const workers = draft.workerNames.split("\n").map((name) => name.trim()).filter(Boolean);
  const slots: AgentTeamSlotInput[] = [
    { ...base, display_name: draft.leaderName.trim(), role: "Leader", area_labels: [], expertise: null, ...charterOf(routing, LEADER_KEY) },
    ...workers.map((name) => {
      const route = routing[name];
      return {
        ...base, display_name: name, role: "Worker",
        area_labels: parseAreaLabels(route?.areaLabels ?? ""),
        expertise: route?.expertise.trim() || null, ...charterOf(routing, name),
      };
    }),
  ];
  if (draft.mergePolicy === "auto") {
    const taken = new Set(slots.map((slot) => slot.display_name.toLowerCase()));
    let name = "Verifier";
    for (let suffix = 2; taken.has(name.toLowerCase()); suffix += 1) name = `Verifier ${suffix}`;
    slots.push({ ...base, display_name: name, role: VERIFIER_ROLE, area_labels: [], expertise: null, ...charterOf(routing, VERIFIER_KEY) });
  }
  return slots;
}

function teamMatchesDraft(
  team: AgentTeamPreset, draft: Draft, routing: Record<string, SlotRoute>,
) {
  const [expectedLeader, ...expectedSlots] = draftTeamSlots(draft, routing);
  const names = [expectedLeader, ...expectedSlots].map((slot) => slot.display_name);
  if (team.name !== draft.teamName.trim() || team.autonomy_enabled || team.slots.length !== names.length ||
      new Set(names).size !== names.length) return false;
  const leader = team.slots.find((slot) => slot.display_name === expectedLeader.display_name);
  // A pending Leader assignment is resumable; a conflicting assignment is not.
  const leaderAssigned = leader != null &&
    (team.leader_slot_id == null || team.leader_slot_id === leader.id);
  if (!leader || !leaderAssigned || !leader.enabled || leader.provider !== draft.provider ||
      leader.repo_path !== draft.path.trim() || (leader.role ?? "").toLowerCase() !== "leader" ||
      (leader.charter ?? "").trim() !== (expectedLeader.charter ?? "").trim()) return false;
  return expectedSlots.every((expected) => {
    const slot = team.slots.find((item) => item.display_name === expected.display_name);
    return Boolean(slot && slot.enabled && slot.provider === draft.provider && slot.repo_path === draft.path.trim() &&
      (slot.role ?? "").toLowerCase() === expected.role?.toLowerCase() && (slot.expertise ?? "") === (expected.expertise ?? "") &&
      JSON.stringify(slot.area_labels ?? []) === JSON.stringify(expected.area_labels) &&
      (slot.charter ?? "").trim() === (expected.charter ?? "").trim());
  });
}

class SaveIntentChangedError extends Error {}

function isDefiniteCreateNonWrite(cause: unknown) {
  // Scoped to create requests only. The create-route transaction contract is
  // proven by test_create_route_400_after_partial_work_persists_nothing: a 400
  // validation refusal, 401 authentication refusal, or 422 schema refusal never
  // commits a write, even after partial in-session work. Transport and server
  // errors stay uncertain and keep the no-duplicate-create latch.
  return cause instanceof ApiHttpError &&
    (cause.status === 400 || cause.status === 401 || cause.status === 422);
}

export function RepositorySetupPage() {
  // "Add a repository" from a team page names the team: ?team_id=N.
  const [searchParams] = useSearchParams();
  const requestedTeam = Number(searchParams.get("team_id")) || null;
  const [draft, setDraft] = useState<Draft>(initial);
  const [step, setStep] = useState(0);
  const [preflight, setPreflight] = useState<{ key: string; value: Preflight } | null>(null);
  const [presets, setPresets] = useState<AgentTeamPreset[]>([]);
  const [scopes, setScopes] = useState<TeamGithubScope[]>([]);
  const [team, setTeam] = useState<AgentTeamPreset | null>(null);
  const [scope, setScope] = useState<TeamGithubScope | null>(null);
  const [recovery, setRecovery] = useState<Recovery>(null);
  const [launchPlan, setLaunchPlan] = useState<AgentTeamLaunchPlan | null>(null);
  const [launchUncertain, setLaunchUncertain] = useState(false);
  const [uncertainLaunch, setUncertainLaunch] = useState<UncertainLaunch>(null);
  const [launchReconciliationReady, setLaunchReconciliationReady] = useState(false);
  const [launchSlots, setLaunchSlots] = useState<number[]>([]);
  const [includeLeader, setIncludeLeader] = useState(false);
  const [activationSnapshot, setActivationSnapshot] = useState<string | null>(null);
  const [activationScopes, setActivationScopes] = useState<TeamGithubScope[]>([]);
  const [scopeOverlaps, setScopeOverlaps] = useState<ScopeOverlap[]>([]);
  const [teamOverlaps, setTeamOverlaps] = useState<ScopeOverlap[]>([]);
  const [scopeOverlapAcks, setScopeOverlapAcks] = useState<string[]>([]);
  const [teamOverlapAcks, setTeamOverlapAcks] = useState<string[]>([]);
  const [activationReadiness, setActivationReadiness] = useState<string[]>([]);
  const [teamActivationAck, setTeamActivationAck] = useState(false);
  const [slotRouting, setSlotRouting] = useState<Record<string, SlotRoute>>({});
  const [tokenDialogOpen, setTokenDialogOpen] = useState(false);
  const [tokenInput, setTokenInput] = useState("");
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [tokenReason, setTokenReason] = useState("to change watched repositories");
  // The person cancelled the key prompt: the form stays, with a way to enter it.
  const [keyNeeded, setKeyNeeded] = useState<"check access" | "save" | null>(null);
  const [repoText, setRepoText] = useState("");
  const [pathChoice, setPathChoice] = useState("");
  const [manual, setManual] = useState(false);
  const projects = useOptionalProjectContext()?.projects ?? [];
  const tokenPromiseRef = useRef<Promise<string | null> | null>(null);
  const tokenResolverRef = useRef<((value: string | null) => void) | null>(null);
  const [confirmSave, setConfirmSave] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const token = getOperatorToken();
  const inputKey = useMemo(() => normalized(draft), [draft]);
  const currentPreflight = preflight?.key === inputKey ? preflight.value : null;
  const currentKey = useRef(inputKey);
  currentKey.current = inputKey;
  const saveIntentKey = JSON.stringify({
    ...JSON.parse(normalized(draft)),
    teamMode: draft.teamMode, teamId: draft.teamId, teamName: draft.teamName,
    leaderName: draft.leaderName, workerNames: draft.workerNames, provider: draft.provider,
    mergePolicy: draft.mergePolicy, slotRouting,
  });
  // A templated charter must be resolved before the team is created. Only new teams carry charters.
  const unresolvedCharters = draft.teamMode === "new"
    ? draftTeamSlots(draft, slotRouting).flatMap((slot) => {
      const names = findUnresolved(slot.charter ?? "");
      return names.length ? [{ member: slot.display_name, names }] : [];
    })
    : [];
  // A value is filled in only when the current access check verified it.
  const charterValues = (workerName?: string) => charterTemplateValues({
    baseRef: currentPreflight?.checks.base_branch?.status === "ready" ? draft.baseRef : undefined,
    dispatchLabel: currentPreflight?.checks.dispatch_label?.status === "ready" ? draft.dispatch : undefined,
    areaLabels: workerName ? parseAreaLabels(slotRouting[workerName]?.areaLabels ?? "") : undefined,
    // A saved scope states its own plan limit. A scope that is not saved yet keeps the documented default.
    maxItemEffortMinutes: scope?.max_item_effort_minutes,
    maxPlanEstimateMinutes: scope?.max_plan_estimate_minutes,
  });
  // The key is a worker name, LEADER_KEY or VERIFIER_KEY.
  const charterField = (key: string, role: CharterRole) => <CharterField
    role={role} values={charterValues(role === "builder" ? key : undefined)}
    value={slotRouting[key]?.charter ?? ""}
    onChange={(charter) => { setConfirmSave(false); setSlotRouting((current) => ({ ...current, [key]: { areaLabels: current[key]?.areaLabels ?? "", expertise: current[key]?.expertise ?? "", charter } })); }} />;
  const currentSaveIntent = useRef(saveIntentKey);
  currentSaveIntent.current = saveIntentKey;
  const liveLaunchSlots = useRef<number[]>([]);
  liveLaunchSlots.current = launchSlots;

  async function readTeamActivationBlockers(currentScope: TeamGithubScope, operatorToken: string): Promise<string[]> {
    // Server-derived authenticated bindings. Hook sessions and spawn-only
    // plans are not readiness. The route names stale, ambiguous,
    // unauthenticated and wrong-member bindings as blockers.
    const readiness = await apiClient<{
      status: string;
      blockers: Array<{ code: string; message: string }>;
    }>(`agent-teams/github-scopes/${currentScope.id}/activation-readiness`, {
      headers: { "X-Synth-City-Operator-Token": operatorToken },
    });
    return readiness.blockers.map((blocker) => `${blocker.message} (${blocker.code})`);
  }

  function applyActivationReview(fresh: { presets: AgentTeamPreset[]; scopes: TeamGithubScope[] }, freshTeam: AgentTeamPreset, freshScope: TeamGithubScope) {
    const scopeTargets = freshTeam.autonomy_enabled || freshScope.enabled ? (freshScope.enabled ? [] : [freshScope]) : [];
    const teamTargets = freshTeam.autonomy_enabled ? [] : fresh.scopes.filter((item) => item.preset_id === freshTeam.id && item.enabled);
    const scopeConflicts = findActivationOverlaps(scopeTargets, fresh.scopes, fresh.presets);
    const teamConflicts = findActivationOverlaps(teamTargets, fresh.scopes, fresh.presets);
    const visibleIds = new Set<number>([freshScope.id, ...teamTargets.map((item) => item.id)]);
    for (const conflict of [...scopeConflicts, ...teamConflicts]) {
      visibleIds.add(conflict.first.id);
      visibleIds.add(conflict.second.id);
    }
    setActivationScopes(fresh.scopes.filter((item) => visibleIds.has(item.id)));
    setScopeOverlaps(scopeConflicts);
    setTeamOverlaps(teamConflicts);
    setScopeOverlapAcks([]);
    setTeamOverlapAcks([]);
    setActivationSnapshot(buildActivationSnapshot(fresh.scopes, fresh.presets, freshTeam));
  }

  function requestOperatorToken(error: string | null = null, reason?: string): Promise<string | null> {
    const stored = getOperatorToken();
    if (stored) return Promise.resolve(stored);
    if (tokenPromiseRef.current) return tokenPromiseRef.current;
    setTokenReason(reason ?? "to change watched repositories");
    setTokenError(error);
    setTokenDialogOpen(true);
    const pending = new Promise<string | null>((resolve) => { tokenResolverRef.current = resolve; });
    tokenPromiseRef.current = pending;
    return pending;
  }

  function settleOperatorToken(value: string | null) {
    if (value) setOperatorToken(value);
    tokenResolverRef.current?.(value);
    tokenResolverRef.current = null;
    tokenPromiseRef.current = null;
    setTokenDialogOpen(false);
    setTokenInput("");
    setTokenError(null);
  }

  async function withOperatorToken<Result>(action: (value: string, attempt: number) => Promise<Result>, reason?: string): Promise<Result> {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const value = await requestOperatorToken(attempt ? "The Director key was rejected. Enter a valid key to retry." : null, reason);
      // Typed pre-send credential cancellation: the prompt cancel sends
      // nothing. Recovery clears only when every attempted create was a
      // proven non-write; uncertain writes keep the latch.
      if (!value) throw new SaveIntentChangedError("The Director key is required for this action.");
      try { return await action(value, attempt); }
      catch (cause) {
        if (!(cause instanceof ApiHttpError) || cause.status !== 401 || attempt > 0) throw cause;
        clearOperatorToken();
      }
    }
    throw new Error("The Director key was rejected.");
  }

  async function refreshConfiguration() {
    // One bounded bulk observation through the shared protected credential
    // helper. No per-preset request fan-out. The route keeps operator
    // authentication (R1).
    const observation = await withOperatorToken((operatorToken) =>
      fetchConfigurationObservation(operatorToken));
    setPresets(observation.presets);
    setScopes(observation.scopes);
    return { presets: observation.presets, scopes: observation.scopes, complete: observation.complete };
  }

  // The same public reads the Teams page makes. The key is asked only to check access or save.
  async function loadPublicConfiguration() {
    const list = await fetchAgentTeamPresets();
    const lists = await Promise.all(list.presets.map((preset) => fetchTeamGithubScopes(preset.id)));
    setPresets(list.presets);
    setScopes(lists.flatMap((item) => item.scopes));
    if (requestedTeam && list.presets.some((item) => item.id === requestedTeam)) {
      setDraft((current) => ({ ...current, teamMode: "existing", teamId: requestedTeam }));
    }
  }

  useEffect(() => {
    void loadPublicConfiguration().catch((cause: unknown) => setError(cause instanceof Error ? cause.message : "Could not load teams."));
    // This effect is the mount-only initial load of the public reads.
  }, []);

  useEffect(() => () => {
    tokenResolverRef.current?.(null);
    tokenResolverRef.current = null;
    tokenPromiseRef.current = null;
  }, []);

  const knownRepositories = useMemo(
    () => [...new Set(scopes.map((item) => `${item.repo_owner}/${item.repo_name}`))].sort(),
    [scopes],
  );

  // Local projects, then the checkouts that the loaded teams already use.
  const checkoutChoices = useMemo(() => {
    const seen = new Set<string>();
    const choices: { path: string; label: string }[] = [];
    const add = (path: string, name: string) => {
      if (!path || seen.has(path)) return;
      seen.add(path);
      choices.push({ path, label: `${name} (${path})` });
    };
    for (const item of projects) add(item.path, item.name);
    for (const preset of presets) for (const slot of preset.slots) add(slot.repo_path, slot.repo_path.split("/").filter(Boolean).pop() ?? slot.repo_path);
    return choices;
  }, [projects, presets]);

  const update = (patch: Partial<Draft>) => {
    currentKey.current = normalized({ ...draft, ...patch });
    setDraft((current) => ({ ...current, ...patch }));
    setLaunchPlan(null);
    setActivationSnapshot(null);
    setScopeOverlaps([]);
    setTeamOverlaps([]);
    setScopeOverlapAcks([]);
    setTeamOverlapAcks([]);
    setActivationReadiness([]);
    setConfirmSave(false);
  };
  const matchingOverlap = useMemo(() => scopes.filter((item) =>
    item.enabled && item.repo_owner.toLowerCase() === draft.owner.trim().toLowerCase() &&
    item.repo_name.toLowerCase() === draft.repo.trim().toLowerCase() && item.dispatch_label === draft.dispatch.trim()
  ), [scopes, draft.owner, draft.repo, draft.dispatch]);

  async function checkPrerequisites() {
    setBusy(true); setError(""); setPreflight(null); setActivationSnapshot(null); setScopeOverlaps([]); setTeamOverlaps([]); setScopeOverlapAcks([]); setTeamOverlapAcks([]); setActivationReadiness([]); setTeamActivationAck(false);
    const checkedKey = inputKey;
    try {
      const value = await withOperatorToken((operatorToken) => apiClient<Preflight>("factory/setup-preflight", {
        method: "POST", headers: { "X-Synth-City-Operator-Token": operatorToken },
        body: JSON.stringify({ repo_owner: draft.owner.trim(), repo_name: draft.repo.trim(), repo_path: draft.path.trim(), dispatch_label: draft.dispatch.trim(), design_label: draft.design.trim(), dispatch_auth_mode: draft.authMode, base_ref: draft.baseRef.trim() }),
      }), "to check access to this repository");
      if (currentKey.current !== checkedKey) return;
      setKeyNeeded(null);
      setPreflight({ key: checkedKey, value });
    } catch (cause) {
      if (cause instanceof SaveIntentChangedError) { setKeyNeeded("check access"); return; }
      if (currentKey.current === checkedKey) setError(cause instanceof ApiHttpError ? cause.message : cause instanceof Error ? cause.message : "The check failed.");
    } finally { setBusy(false); }
  }

  async function reconcileUnknown(kind: "team" | "scope", teamId?: number, requested?: { draft: Draft; routing: Record<string, SlotRoute> }, requestedRecord?: { kind: "team" | "scope"; id: number | null; confirmed: boolean }) {
    setBusy(true); setError("");
    // The complete current recovery intent and requested record identity are
    // captured before any credential or read await.
    const intent = { draft: requested?.draft ?? { ...draft }, routing: requested?.routing ?? { ...slotRouting } };
    const capturedSaveIntent = currentSaveIntent.current;
    const requestedIntentKey = capturedSaveIntent;
    try {
      const fresh = await refreshConfiguration();
      // Recheck the captured intent after the credential and read awaits,
      // before any recovery or navigation change.
      const intentChangedNow = currentSaveIntent.current !== capturedSaveIntent;
      const record = intentChangedNow && requestedRecord
        ? { ...requestedRecord, confirmed: true }
        : requestedRecord;
      if (kind === "team") {
        const names = draftTeamSlots(intent.draft, intent.routing).map((slot) => slot.display_name);
        const candidates = fresh.presets.filter((item) => item.name === draft.teamName.trim() &&
          item.slots.length === names.length && names.every((name) => item.slots.some((slot) => slot.display_name === name)));
        setRecovery({ kind, candidates: candidates.map((item) => ({ label: `${item.name} (#${item.id})`, id: item.id })), requestedDraft: intent.draft, requestedRouting: intent.routing, requestedRecord: record, requestedIntentKey });
      } else {
        const candidates = fresh.scopes.filter((item) => item.preset_id === (teamId ?? team?.id) && scopeMatchesDraft(item, intent.draft));
        setRecovery({ kind, candidates: candidates.map((item) => ({ label: `Repository record #${item.id} (${item.enabled ? "enabled" : "disabled"})`, id: item.id })), requestedDraft: intent.draft, requestedRouting: intent.routing, requestedRecord: record, requestedIntentKey });
      }
      setError("The create response was uncertain. Review fresh matching records and select one, or stop. Do not repeat the create request.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not reconcile the uncertain create result."); }
    finally { setBusy(false); }
  }

  async function saveSetup() {
    if (recovery || !confirmSave || (draft.teamMode === "existing" && !draft.teamId)) return;
    const requestedIntent = currentSaveIntent.current;
    const intentChanged = () => currentSaveIntent.current !== requestedIntent;
    const saveToken = await requestOperatorToken(null, "to save this setup");
    if (!saveToken) { setKeyNeeded("save"); return; }
    setKeyNeeded(null);
    if (intentChanged()) {
      // A refusal before the save body must display safely and return normally.
      setError("Inputs changed during the save. Review the saved records before continuing.");
      return;
    }
    setBusy(true); setError("");
    try {
      const fresh = await refreshConfiguration();
      if (intentChanged()) {
        // A stale save must not create records for changed inputs.
        throw new Error("Inputs changed during the save. Review the saved records before continuing.");
      }
      let selected: AgentTeamPreset | null;
      if (draft.teamMode === "existing") {
        selected = fresh.presets.find((item) => item.id === draft.teamId) ?? null;
      } else if (team) {
        const confirmed = fresh.presets.find((item) => item.id === team.id) ?? null;
        if (!confirmed) throw new Error("The previously created team is no longer present. Refresh and review the records before saving again.");
        if (!teamMatchesDraft(confirmed, draft, slotRouting)) {
          setTeam(confirmed);
          throw new Error("The draft changed after the team was created. The saved team keeps its confirmed values. Make a separately reviewed edit, or start this setup again with a new team.");
        }
        selected = confirmed;
      } else {
        selected = null;
      }
      let anyUncertainCreateAttempt = false;
      if (draft.teamMode === "new" && !selected) {
        const slots = draftTeamSlots(draft, slotRouting);
        if (draft.mergePolicy === "auto" && !slots.some((slot) => slot.role === "Worker")) {
          throw new Error(AUTO_MERGE_OWNER_REQUIREMENT);
        }
        const allNames = slots.map((slot) => slot.display_name);
        if (allNames.some((name) => !name) || new Set(allNames).size !== allNames.length) {
          throw new Error("Enter unique, non-empty names for the Leader and each worker.");
        }
        const input: AgentTeamPresetInput = {
          name: draft.teamName.trim(), autonomy_enabled: false, slots,
        };
        setRecovery({ kind: "team", candidates: [], requestedDraft: { ...draft }, requestedRouting: { ...slotRouting } });
        try {
          selected = await withOperatorToken((operatorToken) => {
            if (intentChanged()) {
              // Checked immediately before the create callback, including any
              // credential-retry invocation of the same callback. This is a
              // pre-send cancellation, not an uncertain transport outcome.
              throw new SaveIntentChangedError("Inputs changed during the save. Review the saved records before continuing.");
            }
            return Promise.resolve(createAgentTeamPreset(input, operatorToken)).catch((cause: unknown) => {
              if (!(cause instanceof SaveIntentChangedError) && !isDefiniteCreateNonWrite(cause)) {
                anyUncertainCreateAttempt = true;
              }
              throw cause;
            });
          });
          if (!anyUncertainCreateAttempt) {
            setRecovery(null);
          }
        } catch (cause) {
          if (cause instanceof SaveIntentChangedError) {
            // Pre-send cancellation: nothing was written. The request latch is
            // cleared only when every attempted create is a proven non-write.
            if (!anyUncertainCreateAttempt) {
              setRecovery(null);
            }
            throw cause;
          }
          if (isDefiniteCreateNonWrite(cause)) {
            // A rejected non-write clears the latch only when no earlier attempt
            // remains uncertain.
            if (!anyUncertainCreateAttempt) {
              setRecovery(null);
            }
            throw cause;
          }
          await reconcileUnknown("team", undefined, { draft: { ...draft }, routing: { ...slotRouting } },
            { kind: "team", id: null, confirmed: false });
          throw cause;
        }
        setTeam(selected);
      }
      if (!selected) throw new Error("Select an existing team or create a new team.");
      if (intentChanged()) {
        // A stale save must not write further records for changed inputs.
        throw new Error("Inputs changed during the save. Review the saved records before continuing.");
      }
      if (draft.teamMode === "new") {
        const leaderSlot = selected.slots.find((slot) => slot.display_name === draft.leaderName.trim());
        if (!leaderSlot) throw new Error("The saved team has no matching Leader slot. Review the team before setup continues.");
        if (selected.leader_slot_id !== leaderSlot.id) {
          selected = await updateAgentTeamLeader(selected.id, { leader_slot_id: leaderSlot.id, expected_leader_slot_id: selected.leader_slot_id ?? null, expected_updated_at: selected.updated_at, reason: "Set the explicit Leader during guided repository setup." }, saveToken);
          if (intentChanged()) {
            // The Leader change is a persisted known record. Do not continue
            // with stale writes or overwrite newer selection or recovery state.
            throw new Error("Inputs changed during the save. The Leader record is saved. Review the saved records before continuing.");
          }
        }
      } else if (!selected.leader_slot_id) {
        throw new Error("The existing team has no explicit Leader assignment. Keep its settings unchanged and resolve its authority separately.");
      }
      setTeam(selected);
      const selectedTeam = selected;
      const sameScope = fresh.scopes.find((item) => item.preset_id === selectedTeam.id && item.repo_owner.toLowerCase() === draft.owner.trim().toLowerCase() &&
        item.repo_name.toLowerCase() === draft.repo.trim().toLowerCase());
      let savedScope = sameScope ?? null;
      if (savedScope && !scopeMatchesDraft(savedScope, draft)) {
        setScope(savedScope);
        setStep(5);
        throw new Error("This team already has a scope for the repository with different saved settings. The existing scope remains unchanged. Review it in Teams before making a separate edit.");
      }
      if (!savedScope) {
        if (draft.mergePolicy === "auto") {
          const blocker = autoMergeSetupBlocker(selectedTeam);
          if (blocker) throw new Error(blocker);
        }
        if (intentChanged()) {
          // A stale save must not issue a scope create for changed inputs.
          throw new Error("Inputs changed during the save. Review the saved records before continuing.");
        }
        setRecovery({ kind: "scope", candidates: [], requestedDraft: { ...draft }, requestedRouting: { ...slotRouting } });
        try {
          savedScope = await withOperatorToken(async (operatorToken, attempt) => {
            if (draft.mergePolicy === "auto" && attempt > 0 && !intentChanged()) {
              // The replacement credential may have waited while the roster changed.
              const observation = await fetchConfigurationObservation(operatorToken).catch(() => {
                throw new SaveIntentChangedError("Could not refresh the team before retrying the scope save. Review the team and save again.");
              });
              const retryTeam = observation.presets.find((item) => item.id === selectedTeam.id);
              if (!retryTeam || retryTeam.leader_slot_id !== selectedTeam.leader_slot_id) {
                throw new SaveIntentChangedError("The selected team or Leader changed before the retry. Review the team and save again.");
              }
              const blocker = autoMergeSetupBlocker(retryTeam);
              if (blocker) throw new SaveIntentChangedError(blocker);
            }
            if (intentChanged()) {
              // Checked immediately before the scope create callback, including
              // any credential-retry invocation of the same callback. This is a
              // pre-send cancellation, not an uncertain transport outcome.
              throw new SaveIntentChangedError("Inputs changed during the save. Review the saved records before continuing.");
            }
            return Promise.resolve(createTeamGithubScope(selectedTeam.id, {
              repo_owner: draft.owner.trim(), repo_name: draft.repo.trim(), repo_path: draft.path.trim(),
              dispatch_label: draft.dispatch.trim(), design_label: draft.design.trim(), merge_policy: draft.mergePolicy,
              max_approval_rounds: 3, max_concurrent_dispatched: 1, max_verification_retries: 1,
              max_auto_merges_per_day: draft.mergePolicy === "auto" ? DEFAULT_AUTO_MERGE_CAP : 0,
              base_ref: draft.baseRef.trim(), github_auth_mode: draft.authMode === "github_app" ? "app" : "ambient",
              builds_out_of_tree: false, max_build_parallelism: 1, enabled: false,
            }, operatorToken)).catch((cause: unknown) => {
              if (!(cause instanceof SaveIntentChangedError) && !isDefiniteCreateNonWrite(cause)) {
                anyUncertainCreateAttempt = true;
              }
              throw cause;
            });
          });
          if (!anyUncertainCreateAttempt) {
            setRecovery(null);
          }
        } catch (cause) {
          if (cause instanceof SaveIntentChangedError) {
            // Pre-send cancellation: nothing was written. The request latch is
            // cleared only when every attempted create is a proven non-write.
            if (!anyUncertainCreateAttempt) {
              setRecovery(null);
            }
            throw cause;
          }
          if (isDefiniteCreateNonWrite(cause)) {
            // A rejected non-write clears the latch only when no earlier attempt
            // remains uncertain.
            if (!anyUncertainCreateAttempt) {
              setRecovery(null);
            }
            throw cause;
          }
          await reconcileUnknown("scope", selected.id, { draft: { ...draft }, routing: { ...slotRouting } },
            { kind: "scope", id: null, confirmed: false });
          throw cause;
        }
      }
      if (intentChanged()) {
        // A stale save must not persist further state for changed inputs.
        throw new Error("Inputs changed during the save. Review the saved records before continuing.");
      }
      setScope(savedScope);
      await refreshConfiguration();
      if (intentChanged()) {
        // A stale save must not navigate to the saved step for changed inputs.
        throw new Error("Inputs changed during the save. Review the saved records before continuing.");
      }
      setStep(5);
    } catch (cause) {
      setError((existing) => existing || (cause instanceof Error ? cause.message : "Setup could not be saved. The draft remains available here."));
    } finally { setBusy(false); }
  }

  async function resolveCandidate(id: number) {
    setBusy(true); setError("");
    // Capture the complete current save intent and the requested record
    // identity before any credential or configuration await.
    const capturedSaveIntent = currentSaveIntent.current;
    const capturedRecovery = recovery;
    const requestedIntent = {
      draft: capturedRecovery?.requestedDraft ?? { ...draft },
      routing: capturedRecovery?.requestedRouting ?? { ...slotRouting },
    };
    try {
      const fresh = await refreshConfiguration();
      // Recheck the live intent and the requested record identity after the
      // credential and configuration awaits, before any recovery or
      // navigation change.
      if (currentSaveIntent.current !== capturedSaveIntent ||
          (capturedRecovery?.requestedIntentKey !== undefined &&
           currentSaveIntent.current !== capturedRecovery.requestedIntentKey)) {
        // The confirmed partial record is preserved explicitly in the
        // recovery record; the uncertain-create safeguards stay latched.
        if (capturedRecovery) {
          setRecovery({ ...capturedRecovery,
            requestedRecord: { kind: capturedRecovery.kind, id, confirmed: true } });
        }
        throw new Error("The selected record no longer matches this draft or the recorded create request. The selected record is preserved. Keep setup blocked and make a separately reviewed edit or an explicit reviewed selection.");
      }
      if (capturedRecovery?.requestedRecord && capturedRecovery.requestedRecord.id !== null &&
          capturedRecovery.requestedRecord.id !== id) {
        if (capturedRecovery) {
          setRecovery({ ...capturedRecovery,
            requestedRecord: { ...capturedRecovery.requestedRecord, confirmed: true } });
        }
        throw new Error("The requested record identity changed during the save. The confirmed record is preserved. Review the saved records before continuing.");
      }
      if (capturedRecovery?.kind === "team") {
        const recovered = fresh.presets.find((item) => item.id === id);
        if (!recovered) throw new Error("The selected team is no longer present. Refresh and review the records again.");
        const requestedDraft = requestedIntent.draft;
        const requestedRouting = requestedIntent.routing;
        if (!teamMatchesDraft(recovered, requestedDraft, requestedRouting) ||
            !teamMatchesDraft(recovered, draft, slotRouting)) {
          throw new Error("The selected team no longer matches this draft or the recorded create request. Keep setup blocked and make a separately reviewed edit or an explicit reviewed selection.");
        }
        setTeam(recovered); setRecovery(null); setError("Team recovered and bound to its confirmed ID. Review the saved team, then save the repository scope as a separate step.");
      } else {
        const recovered = fresh.scopes.find((item) => item.id === id);
        if (!recovered) throw new Error("The selected scope is no longer present. Refresh and review the records again.");
        if (recovered.preset_id !== team?.id || !scopeMatchesDraft(recovered, draft)) throw new Error("The selected scope no longer matches this team and draft. Keep setup blocked and review the records again.");
        setScope(recovered); setRecovery(null); setStep(5);
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not recover the selected record."); }
    finally { setBusy(false); }
  }

  async function reviewActivation() {
    if (!scope || !team || currentPreflight?.status !== "ready") { setError("Run a current ready access and label check before activation."); return; }
    setBusy(true); setError("");
    try {
      const fresh = await refreshConfiguration();
      const freshTeam = fresh.presets.find((item) => item.id === team.id);
      const freshScope = fresh.scopes.find((item) => item.id === scope.id);
      if (!freshTeam) throw new Error("The selected team is no longer available.");
      if (!freshScope || freshScope.preset_id !== freshTeam.id) throw new Error("The saved scope no longer belongs to the selected team.");
      if (!scopeMatchesDraft(freshScope, draft)) throw new Error("The selected scope changed or no longer matches this draft. Review the saved scope before activation.");
      setTeam(freshTeam);
      setScope(freshScope);
      applyActivationReview(fresh, freshTeam, freshScope);
      setTeamActivationAck(false);
      const reviewBlockers = await withOperatorToken((operatorToken) => readTeamActivationBlockers(freshScope, operatorToken));
      if (!fresh.complete) {
        reviewBlockers.push("The configuration check is incomplete. Collisions may be missing. (configuration_observation_incomplete)");
      }
      setActivationReadiness(reviewBlockers);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not refresh activation checks."); }
    finally { setBusy(false); }
  }

  async function activateScope() {
    if (!scope || !team || !token || !activationSnapshot || currentPreflight?.status !== "ready") return;
    setBusy(true); setError("");
    try {
      const checkedKey = inputKey;
      const check = await apiClient<Preflight>("factory/setup-preflight", {
        method: "POST", headers: { "X-Synth-City-Operator-Token": token },
        body: JSON.stringify(scopePreflightInput(scope)),
      });
      if (checkedKey !== currentKey.current || check.status !== "ready") { setPreflight({ key: checkedKey, value: check }); setActivationSnapshot(null); setError("Inputs changed or current checks are not ready. Review the new result before activation."); return; }
      const initial = await refreshConfiguration();
      const initialTeam = initial.presets.find((item) => item.id === team.id);
      const initialScope = initial.scopes.find((item) => item.id === scope.id);
      if (!initialTeam || !initialScope) throw new Error("The selected team or scope is no longer available.");
      if (initialScope.preset_id !== initialTeam.id || !scopeMatchesDraft(initialScope, draft)) throw new Error("The selected saved scope changed. Review it again before activation.");
      if (initialScope.enabled) {
        setTeam(initialTeam); setScope(initialScope); setActivationSnapshot(null);
        setError("Fresh reads confirm that this scope is already enabled. No activation request was repeated.");
        return;
      }
      // Readiness runs before the final comparison so the configuration and
      // overlap recheck follows the last slow await (R2).
      const blockers = await withOperatorToken((operatorToken) => readTeamActivationBlockers(scope, operatorToken));
      if (!initial.complete) {
        blockers.push("The configuration check is incomplete. Collisions may be missing. (configuration_observation_incomplete)");
      }
      // Re-read configuration after the GitHub checks and readiness wait, then
      // compare the reviewed set and invalidate stale acknowledgements.
      const fresh = await refreshConfiguration();
      if (!fresh.complete) {
        // A final incomplete observation blocks the write even when the
        // truncated snapshot matches the reviewed set.
        blockers.push("The final configuration check is incomplete. Collisions may be missing. (configuration_observation_incomplete)");
      }
      setActivationReadiness(blockers);
      if (blockers.length) throw new Error("Shared readiness requirements are not met. Resolve the listed blockers before activation.");
      const refreshedTeam = fresh.presets.find((item) => item.id === team.id);
      const refreshedScope = fresh.scopes.find((item) => item.id === scope.id);
      if (!refreshedTeam || !refreshedScope || refreshedScope.preset_id !== refreshedTeam.id
          || !scopeMatchesDraft(refreshedScope, draft)) {
        throw new Error("The selected saved scope changed after the readiness wait. Review it again before activation.");
      }
      const snapshot = buildActivationSnapshot(fresh.scopes, fresh.presets, refreshedTeam);
      if (snapshot !== activationSnapshot) {
        applyActivationReview(fresh, refreshedTeam, refreshedScope);
        setTeamActivationAck(false);
        throw new Error("The reviewed configuration changed after the checks. Review the refreshed state and confirm the acknowledgements again.");
      }
      if (scopeOverlaps.some((item) => !scopeOverlapAcks.includes(item.key))) {
        throw new Error("Acknowledge each listed overlap, active collisions and prospective overlaps, before activation.");
      }
      if (!refreshedTeam.autonomy_enabled) throw new Error("Enable team automation as a separate reviewed action before enabling this scope.");
      const updatedScope = await updateTeamGithubScope(refreshedScope.id, { enabled: true }, token);
      setScope(updatedScope); await refreshConfiguration();
      setActivationSnapshot(null); setError("Scope activated after current access, label, readiness, and overlap checks.");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Activation could not be completed."); }
    finally { setBusy(false); }
  }

  async function reviewLaunch() {
    if (!team || !token || launchSlots.length === 0) return;
    const requestedSlots = [...launchSlots];
    const requestedKey = currentKey.current;
    setBusy(true); setError("");
    try {
      const planned = await planAgentTeamLaunch(team.id, { slot_ids: requestedSlots, reuse_existing: true }, token);
      // A late plan response for stale inputs or a changed selection is
      // discarded (R2 family).
      const live = liveLaunchSlots.current;
      const selectionChanged = requestedSlots.length !== live.length
        || requestedSlots.some((slotId) => !live.includes(slotId));
      if (currentKey.current !== requestedKey || selectionChanged) {
        setError("Inputs changed while the launch plan was built. Review a new launch plan.");
        return;
      }
      setLaunchPlan(planned);
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not build a current launch plan."); }
    finally { setBusy(false); }
  }

  async function activateTeam() {
    if (!scope || !team || !token || !activationSnapshot || team.autonomy_enabled || currentPreflight?.status !== "ready") return;
    setBusy(true); setError("");
    try {
      const fresh = await refreshConfiguration();
      const freshTeam = fresh.presets.find((item) => item.id === team.id);
      const freshScope = fresh.scopes.find((item) => item.id === scope.id);
      if (!freshTeam || !freshScope || freshScope.preset_id !== freshTeam.id || !scopeMatchesDraft(freshScope, draft)) throw new Error("The selected team or saved scope changed. Review activation again.");
      if (freshTeam.autonomy_enabled) { setTeam(freshTeam); setActivationSnapshot(null); setError("Fresh reads show that team automation is already enabled."); return; }
      const siblings = fresh.scopes.filter((item) => item.preset_id === freshTeam.id && item.enabled);
      const activationTargets = [...new Map([freshScope, ...siblings].map((item) => [item.id, item])).values()];
      if (!fresh.complete) {
        // The initial observation gate blocks before any remote work.
        throw new Error("The initial configuration check is incomplete. Collisions may be missing. No remote checks were made. (configuration_observation_incomplete)");
      }
      // Remote observations run first. Configuration is re-read after these HTTP checks.
      // Bounded remote work: at most 16 targets in batches of 4. Oversize or
      // incomplete observations block readiness before further remote calls.
      const observations: Array<{ scopeId: number; result: Preflight }> = [];
      let boundedWork = false;
      if (activationTargets.length > 16) {
        boundedWork = true;
      } else {
        for (let start = 0; start < activationTargets.length; start += 4) {
          const batch = activationTargets.slice(start, start + 4);
          const results = await Promise.all(batch.map(async (item) => ({
            scopeId: item.id,
            result: await apiClient<Preflight>("factory/setup-preflight", {
              method: "POST", headers: { "X-Synth-City-Operator-Token": token },
              body: JSON.stringify(scopePreflightInput(item)),
            }),
          })));
          observations.push(...results);
          if (results.some(({ result }) => result.status !== "ready")) {
            break;
          }
        }
      }
      const incomplete = observations.filter(({ result }) => result.status !== "ready");
      if (boundedWork) {
        throw new Error("Team activation is blocked. The sibling set exceeds the bounded activation check. Observed collisions are kept; no further remote checks were made.");
      }
      if (incomplete.length) {
        throw new Error(`Team activation remains blocked. Recheck scope ${incomplete.map(({ scopeId, result }) => `${scopeId} (${result.status} at ${result.observed_at}: ${Object.values(result.checks).filter((check) => check.status !== "ready").map((check) => `${check.code}: ${check.remedy}`).join("; ")})`).join(", ")}.`);
      }
      // Readiness runs before the final comparison so the configuration and
      // overlap recheck follows the last slow await (R2).
      const blockers = await withOperatorToken((operatorToken) => readTeamActivationBlockers(scope, operatorToken));
      const reviewed = await refreshConfiguration();
      if (!reviewed.complete) {
        blockers.push("The final configuration check is incomplete. Collisions may be missing. (configuration_observation_incomplete)");
      }
      setActivationReadiness(blockers);
      if (blockers.length) throw new Error("Shared readiness requirements are not met. Resolve the listed blockers before activation.");
      const reviewedTeam = reviewed.presets.find((item) => item.id === team.id);
      const reviewedScope = reviewed.scopes.find((item) => item.id === scope.id);
      if (!reviewedTeam || !reviewedScope || reviewedScope.preset_id !== reviewedTeam.id || !scopeMatchesDraft(reviewedScope, draft)) throw new Error("The selected team or saved scope changed after the checks. Review activation again.");
      const snapshot = buildActivationSnapshot(reviewed.scopes, reviewed.presets, reviewedTeam);
      if (snapshot !== activationSnapshot) {
        applyActivationReview(reviewed, reviewedTeam, reviewedScope);
        setTeamActivationAck(false);
        throw new Error("The reviewed configuration changed after the checks. Review the refreshed state and confirm the acknowledgements again.");
      }
      if (teamOverlaps.some((item) => !teamOverlapAcks.includes(item.key))) {
        throw new Error("Acknowledge each listed overlap, active collisions and prospective overlaps, before enabling team automation.");
      }
      if (!teamActivationAck) throw new Error("Confirm team activation and review every enabled sibling scope first.");
      const updatedTeam = await updateAgentTeamPreset(reviewedTeam.id, { autonomy_enabled: true }, token);
      setTeam(updatedTeam); setActivationSnapshot(null); setTeamActivationAck(false);
      setError("Team automation enabled. Review scope activation separately before enabling this scope.");
      await refreshConfiguration();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Team activation could not be completed."); }
    finally { setBusy(false); }
  }

  async function launchSelected() {
    if (!team || !launchPlan || !token) return;
    setBusy(true); setError("");
    try {
      const result = await launchAgentTeam(team.id, { slot_ids: launchSlots, reuse_existing: true, confirm_plan_hash: launchPlan.plan_hash }, token);
      setError(`Launch completed with status: ${result.status}. Review its result before a separate activation decision.`);
      setLaunchPlan(null);
      setLaunchUncertain(false);
      setUncertainLaunch(null);
      setLaunchReconciliationReady(false);
    } catch (cause) {
      if (cause instanceof ApiHttpError && cause.status === 409 && cause.code === "plan_conflict"
          && cause.provenNonWrite === true) {
        // Proven pre-write plan conflict: nothing launched. A fresh reviewed
        // plan is required; there is no uncertain outcome to reconcile.
        setLaunchPlan(null); setLaunchUncertain(false); setUncertainLaunch(null);
        setLaunchReconciliationReady(false);
        setError("Launch plan changed or needs confirmation. Review a new launch plan before launching.");
        return;
      }
      setError(`Launch outcome is uncertain. Do not launch again until the Director reconciles the team sessions. ${cause instanceof Error ? cause.message : "No safe result was returned."}`);
      setUncertainLaunch({ slotIds: [...launchSlots], planHash: launchPlan.plan_hash });
      setLaunchPlan(null); setLaunchUncertain(true);
    }
    finally { setBusy(false); }
  }

  async function reconcileLaunch() {
    if (!team || !token || !uncertainLaunch || uncertainLaunch.slotIds.length === 0) return;
    setBusy(true); setError(""); setLaunchReconciliationReady(false);
    try {
      const current = await planAgentTeamLaunch(team.id, { slot_ids: uncertainLaunch.slotIds, reuse_existing: true }, token);
      setLaunchPlan(current);
      const selectedItems = current.items.filter((item) => uncertainLaunch.slotIds.includes(item.slot_id));
      const confirmed = selectedItems.length === uncertainLaunch.slotIds.length && selectedItems.every((item) => item.action === "reuse" && Boolean(item.matching_session));
      setLaunchReconciliationReady(confirmed);
      setError(confirmed
        ? `Fresh planning found an existing session for every originally requested slot. Original plan ${uncertainLaunch.planHash}; review the plan, then confirm this reconciliation.`
        : `Fresh planning did not confirm an existing session for every originally requested slot from plan ${uncertainLaunch.planHash}. Keep launch blocked and ask the Director to reconcile the remaining slots.`);
    } catch (cause) {
      setError(`Launch outcome remains uncertain. ${cause instanceof Error ? cause.message : "Fresh session planning failed."}`);
    } finally { setBusy(false); }
  }

  return <section className="mx-auto max-w-3xl space-y-5">
    <div><h2 className="text-2xl font-semibold">Set up a repository</h2><p className="text-muted-foreground">Add a repository in a few steps: pick it, choose a team, then review and save.</p></div>
    <ol className="flex flex-wrap gap-2 text-sm" aria-label="Setup steps">{([["Repository", "required"], ["Team and roles", "required"], ["Routing", "optional"], ["Policy", "optional"], ["Review", "required"], ["Saved setup", ""]] as const).map(([name, mark], index) => <li key={name} aria-current={step === index ? "step" : undefined} className={`rounded border px-3 py-1 ${step === index ? "bg-accent font-semibold" : ""}`}>{index + 1}. {name}{mark && <span className="ml-2 text-xs font-normal text-muted-foreground">{mark}</span>}</li>)}</ol>
    <div role="progressbar" aria-label="Setup progress" aria-valuemin={1} aria-valuemax={6} aria-valuenow={step + 1} className="h-1 overflow-hidden rounded bg-muted"><div className="h-full bg-primary" style={{ width: `${((step + 1) / 6) * 100}%` }} /></div>
    {error && <p role="alert" className="rounded border border-destructive p-3 text-sm">{error}</p>}
    {(keyNeeded || !token) && <p role="status" className="rounded border p-3 text-sm">{keyNeeded ? `The Director key is needed to ${keyNeeded}` : "Synth City asks for the Director key only when you check access or save"} · <button type="button" className="underline" onClick={() => void requestOperatorToken(null, keyNeeded === "save" ? "to save this setup" : "to check access to this repository").then((value) => { if (value) setKeyNeeded(null); })}>Enter key</button></p>}
    {recovery && <div className={panelClass}><h3 className="font-semibold">Reconcile uncertain create</h3><p>Fresh reads found {recovery.candidates.length} matching record(s). Select one record to continue. Do not repeat the create request.</p>{recovery.candidates.length === 0 && <p role="status">No unique record is ready for selection. Keep this setup blocked and contact an operator.</p>}{recovery.candidates.map((item) => <Button key={item.id} variant="outline" disabled={busy} onClick={() => void resolveCandidate(item.id)}>Use {item.label}</Button>)}</div>}
    {step === 0 && <div className={panelClass}>
      <label className={labelClass}>GitHub repository<input className={inputClass} placeholder="https://github.com/owner/name or owner/name" value={repoText} onChange={(e) => { const text = e.target.value; setRepoText(text); const parsed = parseRepository(text); update(parsed ? { owner: parsed.owner, repo: parsed.repo } : { owner: "", repo: "" }); }} /></label>
      {knownRepositories.length > 0 && <div className="flex flex-wrap items-center gap-2 text-sm" role="group" aria-label="Repositories Synth City already watches"><span className="text-muted-foreground">Already watched:</span>{knownRepositories.map((item) => <Button key={item} type="button" variant="outline" size="sm" onClick={() => { setRepoText(item); const parsed = parseRepository(item); if (parsed) update({ owner: parsed.owner, repo: parsed.repo }); }}>{item}</Button>)}</div>}
      <label className={labelClass}>Local checkout<select className={inputClass} value={pathChoice} onChange={(e) => { const value = e.target.value; setPathChoice(value); if (value === OTHER) { setManual(true); update({ path: "" }); } else if (value) update({ path: value }); }}><option value="">Choose a local project…</option>{checkoutChoices.map((item) => <option key={item.path} value={item.path}>{item.label}</option>)}<option value={OTHER}>Other path…</option></select></label>
      <label className={labelClass}>Branch<select className={inputClass} value={draft.baseRef === "origin/HEAD" ? "origin/HEAD" : OTHER} onChange={(e) => { if (e.target.value === OTHER) { setManual(true); update({ baseRef: "origin/" }); } else update({ baseRef: "origin/HEAD" }); }}><option value="origin/HEAD">Repository default branch (origin/HEAD)</option><option value={OTHER}>Another branch…</option></select></label>
      <details open={manual} onToggle={(e) => setManual((e.currentTarget as HTMLDetailsElement).open)} className="rounded border p-3"><summary className="cursor-pointer text-sm font-medium">Enter the details by hand</summary>
        <div className="mt-3 space-y-4">
          <label className={labelClass}>Repository owner<input className={inputClass} value={draft.owner} onChange={(e) => update({ owner: e.target.value })} /></label>
          <label className={labelClass}>Repository name<input className={inputClass} value={draft.repo} onChange={(e) => update({ repo: e.target.value })} /></label>
          <label className={labelClass}>Primary checkout path<input className={inputClass} value={draft.path} onChange={(e) => update({ path: e.target.value })} /></label>
          <label className={labelClass}>Base branch<input className={inputClass} value={draft.baseRef} onChange={(e) => update({ baseRef: e.target.value })} /><span className="text-xs text-muted-foreground">The default is origin/HEAD, the repository's default branch. For another branch, write origin/&lt;name&gt;.</span></label>
        </div>
      </details>
      <label className={labelClass}>Dispatch label<input className={inputClass} value={draft.dispatch} onChange={(e) => update({ dispatch: e.target.value })} /></label>
      <label className={labelClass}>Design label<input className={inputClass} value={draft.design} onChange={(e) => update({ design: e.target.value })} /></label>
      <label className={labelClass}>Dispatch authentication<select className={inputClass} value={draft.authMode} onChange={(e) => update({ authMode: e.target.value as Draft["authMode"] })}><option value="token">GitHub token</option><option value="github_app">GitHub App</option></select></label>
      <p className="text-sm">Synth City reads the checkout and checks that it can see the repository and its labels. Nothing changes. The Director key is asked for only when you check access or save.</p>
      <Button disabled={busy || !draft.owner || !draft.repo || !draft.path || !draft.dispatch || !draft.design || !draft.baseRef} onClick={() => void checkPrerequisites()}>{busy ? "Checking…" : "Check access and labels"}</Button>
      {preflight && <div className="rounded border p-3" aria-live="polite"><p>{currentPreflight ? "Current result" : "Stale result"}: {preflight.value.status}; observed {new Date(preflight.value.observed_at ?? preflight.value.checked_at ?? "").toLocaleString()}.</p>{Object.entries(preflight.value.checks).map(([name, check]) => <p key={name}>{name}: {check.status} ({check.code}). Remedy: {check.remedy}</p>)}</div>}
      <Button variant="outline" disabled={busy || !draft.owner || !draft.repo || !draft.path || !draft.dispatch || !draft.design || !draft.baseRef} onClick={() => setStep(1)}>Continue to configuration</Button>
    </div>}
    {step === 1 && <div className={panelClass}><label className={labelClass}>Team setup<select className={inputClass} value={draft.teamMode} onChange={(e) => update({ teamMode: e.target.value as Draft["teamMode"] })}><option value="new">Create an inactive team</option><option value="existing">Use an existing team</option></select></label>
      {draft.teamMode === "existing" ? <label className={labelClass}>Team<select className={inputClass} value={draft.teamId} onChange={(e) => update({ teamId: Number(e.target.value) || "" })}><option value="">Select a team</option>{presets.map((item) => <option key={item.id} value={item.id}>{item.name} ({item.autonomy_enabled ? "active" : "paused"})</option>)}</select></label> : <><label className={labelClass}>Team name<input className={inputClass} value={draft.teamName} onChange={(e) => update({ teamName: e.target.value })} /></label><label className={labelClass}>Explicit Leader slot<input className={inputClass} value={draft.leaderName} onChange={(e) => update({ leaderName: e.target.value })} /></label><label className={labelClass}>Worker slots, one name per line<textarea className={inputClass} value={draft.workerNames} onChange={(e) => update({ workerNames: e.target.value })} /></label><label className={labelClass}>Worker and Leader provider<select className={inputClass} value={draft.provider} onChange={(e) => update({ provider: e.target.value })}>{["claude-code", "codex-cli", "copilot-cli", "opencode-cli", "pi-cli", "muse-cli", "antigravity-cli"].map((item) => <option key={item}>{item}</option>)}</select></label></>}
      <p className="text-sm">New teams remain inactive. Existing roster, Leader, policy, and activation stay unchanged.</p><Button disabled={draft.teamMode === "existing" && !draft.teamId} onClick={() => setStep(2)}>Continue</Button></div>}
    {step === 2 && <div className={panelClass}><p>Repository dispatch and design labels apply to the scope. Worker routes are separate for each slot. The Leader has no worker route.</p>{draft.teamMode === "new" ? <><fieldset className="space-y-2 rounded border p-3"><legend className="font-medium">{draft.leaderName.trim() || "Leader"}</legend>{charterField(LEADER_KEY, "leader")}</fieldset>{draft.workerNames.split("\n").map((rawName) => rawName.trim()).filter(Boolean).map((name) => <fieldset key={name} className="space-y-2 rounded border p-3"><legend className="font-medium">{name}</legend><label className={labelClass}>Area labels, comma separated<input className={inputClass} value={slotRouting[name]?.areaLabels ?? ""} onChange={(e) => { setConfirmSave(false); setSlotRouting((current) => ({ ...current, [name]: { ...current[name], areaLabels: e.target.value, expertise: current[name]?.expertise ?? "" } })); }} /></label><label className={labelClass}>Expertise<input className={inputClass} value={slotRouting[name]?.expertise ?? ""} onChange={(e) => { setConfirmSave(false); setSlotRouting((current) => ({ ...current, [name]: { ...current[name], areaLabels: current[name]?.areaLabels ?? "", expertise: e.target.value } })); }} /></label>{charterField(name, "builder")}</fieldset>)}</> : <p>Existing team routing stays unchanged.</p>}<p>Potential enabled overlaps: {matchingOverlap.map((item) => `team ${item.preset_id}/scope ${item.id}`).join(", ") || "none found in loaded data"}.</p><div className="flex flex-wrap gap-2"><Button onClick={() => setStep(3)}>Continue</Button><Button variant="outline" onClick={() => setStep(4)}>Skip optional steps</Button></div></div>}
    {step === 3 && <div className={panelClass}>
      <h3 className="font-semibold">Policy</h3>
      <label className={labelClass}>Merge policy
        <select className={inputClass} value={draft.mergePolicy} onChange={(event) => update({ mergePolicy: event.target.value as TeamGithubMergePolicy })}>
          <option value="human">Human review and merge</option>
          <option value="auto">Automatic merge</option>
        </select>
      </label>
      <p>{AUTO_MERGE_VERIFIER_REQUIREMENT} The verifier reviews the exact PR commit. Required checks must pass before merging.</p>
      {draft.mergePolicy === "auto" && <p>{AUTO_MERGE_OWNER_REQUIREMENT}</p>}
      {draft.mergePolicy === "auto" && draft.teamMode === "new" && <fieldset className="space-y-2 rounded border p-3"><legend className="font-medium">{draftTeamSlots(draft, slotRouting).find((slot) => slot.role === VERIFIER_ROLE)?.display_name}</legend>{charterField(VERIFIER_KEY, "reviewer")}</fieldset>}
      <p>New scopes allow one active work item and one verification retry. Setup saves the scope disabled.</p>
      <Button onClick={() => setStep(4)}>Review setup</Button>
    </div>}
    {step === 4 && <div className={panelClass}><h3 className="font-semibold">Review</h3><p>Repository: {draft.owner}/{draft.repo}</p><p>Checkout: {draft.path}</p><p>Base: {draft.baseRef}</p>
      {draft.mergePolicy === "auto" ? <>
        <p>Automatic merge, up to {DEFAULT_AUTO_MERGE_CAP} per day.</p>
        <p>{draft.teamMode === "new"
          ? `${draftTeamSlots(draft, slotRouting).find((slot) => slot.role === VERIFIER_ROLE)?.display_name} will be added as a separate verifier slot.`
          : AUTO_MERGE_VERIFIER_REQUIREMENT}</p>
      </> : <p>Human review and merge. {AUTO_MERGE_VERIFIER_REQUIREMENT}</p>}
      <p>Labels: {draft.dispatch} / {draft.design}</p><p>Authentication: {draft.authMode}; credentials are not stored in this draft.</p><p>Preflight: {currentPreflight?.status ?? "not checked or stale"}. Saving remains disabled and does not need ready checks.</p>{currentPreflight && <p className="text-sm">Configuration presence, boolean only: {Object.entries(currentPreflight.configuration_presence ?? {}).map(([name, present]) => `${name}: ${present ? "present" : "missing"}`).join(", ") || "not reported"}.</p>}{(currentPreflight?.host_guidance ?? []).length > 0 && <div className="text-sm"><p className="font-medium">Host procedure</p><ol className="list-decimal pl-5">{(currentPreflight?.host_guidance ?? []).map((line) => <li key={line}>{line}</li>)}</ol></div>}<p className="text-sm">Runtime recovery-only restrictions are separate from credential readiness. Activation needs every required check ready in a fresh observation.</p><p>Team: {draft.teamMode === "existing" ? presets.find((item) => item.id === draft.teamId)?.name ?? "Select a team" : draft.teamName || "New team"}; Leader: {draft.teamMode === "new" ? draft.leaderName : "preserved"}; worker slots: {draft.teamMode === "new" ? draft.workerNames.split("\n").filter((name) => name.trim()).length : "preserved"}.</p><p>Potential overlap: {matchingOverlap.map((item) => `team ${item.preset_id}/scope ${item.id}`).join(", ") || "none found"}.</p>{unresolvedCharters.length > 0 && <p role="status" className="rounded border border-amber-500/60 p-2 text-sm">Save is blocked. Replace every placeholder, or clear the charter, in the Routing or Policy step: {unresolvedCharters.map((item) => `${item.member} (${item.names.map((name) => `{{${name}}}`).join(", ")})`).join("; ")}.</p>}<label className="flex gap-2 text-sm"><input type="checkbox" checked={confirmSave} onChange={(e) => setConfirmSave(e.target.checked)} />Save configuration only. Keep launch, team activation, scope activation, and merge decisions separate.</label><div className="flex flex-wrap gap-2"><Button disabled={busy || Boolean(recovery) || !confirmSave || unresolvedCharters.length > 0 || (draft.teamMode === "existing" && !draft.teamId)} onClick={() => void saveSetup()}>{busy ? "Saving…" : "Save configuration"}</Button><Link className="self-center underline" to="/teams">Manage teams</Link></div></div>}
    {step === 5 && team && scope && <div className={panelClass}><h3 className="font-semibold">Saved configuration</h3><p>Team {team.name} (#{team.id}) is {team.autonomy_enabled ? "active" : "paused"}. Repository record #{scope.id} is {scope.enabled ? "enabled" : "disabled"}.</p><p>Saving did not launch workers or activate this scope.</p>
      <section className="space-y-2 rounded border p-3" aria-live="polite"><h4 className="font-semibold">Saved-stage preflight</h4>
        {preflight ? (<>
          <p className="text-sm">{currentPreflight ? "Current result" : "Stale result"}; observed {new Date(preflight.value.observed_at ?? preflight.value.checked_at ?? "").toLocaleString()}; status {preflight.value.status}.</p>
          {Object.entries(preflight.value.checks).map(([name, check]) => <p key={name} className="text-sm">{name}: {check.status} ({check.code}). Remedy: {check.remedy}</p>)}
          {Object.values(preflight.value.checks).some((check) => check.status !== "ready") && (
            <p className="text-sm">Gaps remain in the checks above. Complete each remedy, then repeat the check before activation.</p>
          )}
        </>) : (<p className="text-sm">No preflight result is saved for this stage. Run the access and label check to record one.</p>)}
      </section><p>Confirmed values bound to team #{team.id} and scope #{scope.id}: provider {team.slots.find((slot) => slot.id === team.leader_slot_id)?.provider ?? "unknown"}; Leader {team.slots.find((slot) => slot.id === team.leader_slot_id)?.display_name ?? "unassigned"}; slots {team.slots.length}. A cached fallback is not current evidence. Make a separately reviewed edit to change these saved values.</p>
      <section className="space-y-2 border-t pt-3"><h4 className="font-semibold">Separate worker launch</h4>{team.slots.filter((slot) => slot.id === team.leader_slot_id && slot.enabled).map((slot) => <label key={slot.id} className="flex gap-2 text-sm"><input type="checkbox" checked={includeLeader} onChange={(event) => { if (launchUncertain) return; setIncludeLeader(event.target.checked); setLaunchPlan(null); setLaunchReconciliationReady(false); setLaunchSlots((current) => event.target.checked ? [...new Set([...current, slot.id])] : current.filter((id) => id !== slot.id)); }} />Include Leader {slot.display_name} in this reviewed launch plan</label>)}{team.slots.filter((slot) => slot.enabled && slot.id !== team.leader_slot_id).map((slot) => <label key={slot.id} className="flex gap-2 text-sm"><input type="checkbox" checked={launchSlots.includes(slot.id)} onChange={(event) => { if (launchUncertain) return; setLaunchPlan(null); setLaunchReconciliationReady(false); setLaunchSlots((current) => event.target.checked ? [...new Set([...current, slot.id])] : current.filter((id) => id !== slot.id)); }} />{slot.display_name} ({slot.provider})</label>)}<Button disabled={busy || launchUncertain || launchSlots.length === 0} onClick={() => void reviewLaunch()}>Review current launch plan</Button>{launchUncertain && <><p role="status">Launch outcome needs operator reconciliation. This flow will not repeat the launch request.</p><Button disabled={busy || !uncertainLaunch} onClick={() => void reconcileLaunch()}>Reconcile current sessions</Button>{launchReconciliationReady && <Button disabled={busy} onClick={() => { setLaunchUncertain(false); setUncertainLaunch(null); setLaunchReconciliationReady(false); setLaunchPlan(null); setError("Current sessions were reconciled. Review a new launch plan before any further launch action."); }}>Confirm reconciled sessions</Button>}</>}{launchPlan && <div className="rounded border p-3"><p>Plan {launchPlan.can_launch ? "ready" : "blocked"}: spawn {launchPlan.spawn_count}, reuse {launchPlan.reuse_count}, blocked {launchPlan.blocked_count}.</p>{launchPlan.items.map((item) => <p key={item.slot_id}>{item.slot_name}: {item.action}{item.reasons.length ? ` (${item.reasons.join(", ")})` : ""}</p>)}<Button disabled={busy || launchUncertain || !launchPlan.can_launch} onClick={() => void launchSelected()}>Launch reviewed slots</Button></div>}</section>
      <section className="space-y-2 border-t pt-3"><h4 className="font-semibold">Separate activation</h4><p>Run a current ready access and label check before activation. Overlap detection is advisory. It is not an atomic cross-scope ownership guarantee.</p><Button disabled={busy} onClick={() => void checkPrerequisites()}>Refresh access and label check</Button><Button disabled={busy || currentPreflight?.status !== "ready"} onClick={() => void reviewActivation()}>Review current activation and overlap</Button>{activationSnapshot && <div className="space-y-2 rounded border p-3"><p>Scopes affected or overlapping: {activationScopes.map((item) => `team ${item.preset_id}/scope ${item.id} (${item.repo_owner}/${item.repo_name}, ${item.dispatch_label})`).join(", ") || "none"}.</p>
      {activationReadiness.length > 0 && <div className="rounded border p-2" role="status"><p className="font-medium">Shared readiness blockers</p>{activationReadiness.map((item) => <p key={item} className="text-sm">{item}</p>)}</div>}
      {scopeOverlaps.length > 0 && <div className="space-y-1"><p className="font-medium">Scope activation overlaps</p>{scopeOverlaps.map((overlap) => <label key={overlap.key} className="flex gap-2 text-sm"><input type="checkbox" checked={scopeOverlapAcks.includes(overlap.key)} onChange={(e) => setScopeOverlapAcks((current) => e.target.checked ? [...current, overlap.key] : current.filter((key) => key !== overlap.key))} /><span>{overlap.active ? "Active collision" : "Prospective overlap"}: {overlapDescription(overlap)}. {overlapWarning}.</span></label>)}</div>}
      {teamOverlaps.length > 0 && <div className="space-y-1"><p className="font-medium">Team activation overlaps</p>{teamOverlaps.map((overlap) => <label key={overlap.key} className="flex gap-2 text-sm"><input type="checkbox" checked={teamOverlapAcks.includes(overlap.key)} onChange={(e) => setTeamOverlapAcks((current) => e.target.checked ? [...current, overlap.key] : current.filter((key) => key !== overlap.key))} /><span>{overlap.active ? "Active collision" : "Prospective overlap"}: {overlapDescription(overlap)}. {overlapWarning}.</span></label>)}</div>}
      {!team.autonomy_enabled && <><label className="flex gap-2 text-sm"><input type="checkbox" checked={teamActivationAck} onChange={(e) => setTeamActivationAck(e.target.checked)} />Enable team automation. This also resumes each listed enabled sibling scope.</label><Button disabled={busy || !teamActivationAck || teamOverlaps.some((item) => !teamOverlapAcks.includes(item.key)) || activationReadiness.length > 0} onClick={() => void activateTeam()}>Enable team automation</Button></>}<Button disabled={busy || !team.autonomy_enabled || scopeOverlaps.some((item) => !scopeOverlapAcks.includes(item.key)) || activationReadiness.length > 0} onClick={() => void activateScope()}>Enable this scope</Button></div>}</section>
    </div>}
    {step > 0 && step < 5 && <Button variant="outline" onClick={() => setStep((value) => Math.max(0, value - 1))}>Back</Button>}
    <OperatorTokenDialog
      open={tokenDialogOpen}
      value={tokenInput}
      error={tokenError}
      reason={tokenReason}
      onValueChange={setTokenInput}
      onSubmit={() => settleOperatorToken(tokenInput)}
      onCancel={() => settleOperatorToken(null)}
    />
  </section>;
}

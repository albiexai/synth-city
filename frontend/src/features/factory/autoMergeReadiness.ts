import type { AgentTeamPreset } from "@/types/agentTeams";

export const VERIFIER_ROLE = "verifier";
export const AUTO_MERGE_VERIFIER_REQUIREMENT = "Automatic merge requires an enabled verifier slot separate from the Leader and the PR owner.";
export const AUTO_MERGE_OWNER_REQUIREMENT = "Automatic merge needs an enabled slot for the PR owner, separate from the Leader and verifier.";

export function hasIndependentVerifier(team: AgentTeamPreset) {
  return team.slots.some((slot) => slot.enabled && slot.id !== team.leader_slot_id &&
    // Match the role accepted by the protected merge transaction.
    (slot.role ?? "").replace(/^ +| +$/g, "").toLowerCase() === VERIFIER_ROLE);
}

export function autoMergeSetupBlocker(team: AgentTeamPreset) {
  if (!team.slots.some((slot) => slot.id === team.leader_slot_id && slot.enabled)) {
    return "Automatic merge requires an enabled Leader.";
  }
  if (!hasIndependentVerifier(team)) return AUTO_MERGE_VERIFIER_REQUIREMENT;

  // Dispatch excludes the Leader and coordinator roles from ownership.
  const available = team.slots.filter((slot) => slot.enabled && slot.id !== team.leader_slot_id &&
    (slot.role ?? "").replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, "").toLowerCase() !== "coordinator");
  return available.length < 2 ? AUTO_MERGE_OWNER_REQUIREMENT : null;
}

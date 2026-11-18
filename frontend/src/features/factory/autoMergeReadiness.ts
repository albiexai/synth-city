import type { AgentTeamPreset } from "@/types/agentTeams";

export const VERIFIER_ROLE = "verifier";
export const AUTO_MERGE_VERIFIER_REQUIREMENT = "Automatic merge requires an enabled verifier slot separate from the Leader and the PR owner.";

export function hasIndependentVerifier(team: AgentTeamPreset) {
  return team.slots.some((slot) => slot.enabled && slot.id !== team.leader_slot_id &&
    // Match the role accepted by the protected merge transaction.
    (slot.role ?? "").replace(/^ +| +$/g, "").toLowerCase() === VERIFIER_ROLE);
}

import type { AgentTeamPreset } from "@/types/agentTeams";

export interface TeamFilters {
  provider: string | null;
  scopeTeamIds: ReadonlySet<number> | null;
}

// A team card stays when a roster slot runs the provider and, for a repository
// filter, when that repository belongs to the team.
export function teamVisible(preset: AgentTeamPreset, filters: TeamFilters): boolean {
  if (filters.provider && !preset.slots.some((slot) => slot.provider === filters.provider)) {
    return false;
  }
  return filters.scopeTeamIds === null || filters.scopeTeamIds.has(preset.id);
}

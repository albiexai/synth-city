import { useSearchParams } from "react-router-dom";
import type { AgentTeamPresetListResponse } from "@/types/agentTeams";
import type { RepositoryListResponse, RepositoryProjection } from "@/types/factory";
import { useObservation } from "./reads";

export interface FilterOption {
  value: string;
  label: string;
}

const OPTIONS_ENDPOINT = "factory/repositories?limit=100";
const FILTER_KEYS = ["team_id", "scope_id"] as const;
const POSITIVE_INTEGER = /^[1-9]\d{0,9}$/;

function repositoryLabel(row: RepositoryProjection, rows: RepositoryProjection[]) {
  const name = `${row.github.owner}/${row.github.name}`;
  const same = rows.filter(
    (other) => `${other.github.owner}/${other.github.name}` === name,
  );
  if (same.length < 2) return name;
  const withTeam = `${name} · ${row.team.name}`;
  const twins = rows.filter(
    (other) =>
      `${other.github.owner}/${other.github.name} · ${other.team.name}` ===
      withTeam,
  );
  return twins.length < 2 ? withTeam : `${withTeam} · scope ${row.scope_id}`;
}

// Team and repository names for the filter selects, and the URL parameters
// with every value that names no known team or repository removed. A value
// that cannot be checked yet is held back, so no bad value reaches the API.
export function useFactoryFilters(work = false) {
  const [raw] = useSearchParams();
  const options = useObservation<RepositoryListResponse>(OPTIONS_ENDPOINT);
  const roster = useObservation<AgentTeamPresetListResponse>(work ? "agent-teams/presets" : null);
  const rows = Array.isArray(options.data?.repositories)
    ? options.data.repositories
    : null;
  const loading = !options.data && !options.error;
  // Beyond the first page the list cannot prove that a value is unknown.
  const complete = rows !== null && !options.data?.has_more;

  const teams: FilterOption[] = [];
  const repositories: FilterOption[] = [];
  for (const row of rows ?? []) {
    if (!teams.some((team) => team.value === String(row.team.id)))
      teams.push({ value: String(row.team.id), label: row.team.name });
    repositories.push({
      value: String(row.scope_id),
      label: repositoryLabel(row, rows ?? []),
    });
  }
  teams.sort((a, b) => a.label.localeCompare(b.label));
  repositories.sort((a, b) => a.label.localeCompare(b.label));

  const known = { team_id: teams, scope_id: repositories };
  const params = new URLSearchParams(raw);
  const unknown: ("team_id" | "scope_id" | "owner_slot_id")[] = [];
  let ready = true;
  for (const key of FILTER_KEYS) {
    const value = raw.get(key);
    if (value === null || value === "") continue;
    if (!POSITIVE_INTEGER.test(value)) {
      params.delete(key);
      unknown.push(key);
    } else if (complete) {
      if (!known[key].some((option) => option.value === value)) {
        params.delete(key);
        unknown.push(key);
      }
    } else if (loading) {
      ready = false;
    }
  }
  const ownerTeam = params.get("team_id") ??
    rows?.find((row) => String(row.scope_id) === params.get("scope_id"))?.team.id.toString();
  const presets = roster.data?.presets;
  const owners = (presets ?? []).flatMap((preset) =>
    ownerTeam && String(preset.id) !== ownerTeam ? [] : preset.slots.map((slot) => ({
      value: String(slot.id),
      label: [slot.display_name, slot.role, !ownerTeam && (presets?.length ?? 0) > 1 && preset.name]
        .filter(Boolean).join(" · "),
    })),
  ).sort((a, b) => a.label.localeCompare(b.label));
  const selectedOwner = work ? raw.get("owner_slot_id") : null;
  if (selectedOwner) {
    if (!POSITIVE_INTEGER.test(selectedOwner) || (presets && !owners.some((owner) => owner.value === selectedOwner))) {
      params.delete("owner_slot_id");
      unknown.push("owner_slot_id");
    } else if (!roster.data && !roster.error) {
      ready = false;
    }
  }
  return { params, unknown, ready, teams, repositories, owners };
}

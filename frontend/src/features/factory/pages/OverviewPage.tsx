import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { TeamSummaryCard } from "@/features/agent-teams/TeamSummaryCard";
import type { AgentTeamPresetListResponse, AgentTeamSummary } from "@/types/agentTeams";
import type { OverviewResponse, RepositoryListResponse, WorkListResponse } from "@/types/factory";
import { ActiveWork } from "../components/overview/ActiveWork";
import { CampaignProgress } from "../components/overview/CampaignProgress";
import { NeedsYou } from "../components/overview/NeedsYou";
import { OperationsFindings } from "../components/OperationsFindings";
import { filterNeedsYouRows, needsYouRows } from "../components/overview/needsYou";
import { StatusLine } from "../components/overview/StatusLine";
import { StatusTiles } from "../components/overview/StatusTiles";
import { ReadStatus } from "../components/ReadStatus";
import { RelativeTime } from "../components/RelativeTime";
import { anchor, destination, panel } from "../components/styles";
import { Time } from "../components/Time";
import { factoryQuery, workLink } from "../filterHelpers";
import { FactoryFilters } from "../filters";
import { useFactoryFilters } from "../filterOptions";
import { useObservation, useObservations } from "../reads";
import { teamVisible } from "./overviewFilters";

export function OverviewPage() {
  const { params, ready } = useFactoryFilters();
  const query = factoryQuery(params);
  const state = useObservation<OverviewResponse>(
    ready ? `factory/overview${query ? `?${query}` : ""}` : null,
  );
  const filtered = Boolean(query);
  // The same reads as the team cards below, so they add no request.
  const teamId = params.get("team_id");
  const scopeId = params.get("scope_id");
  const provider = params.get("provider");
  const presetList = useObservation<AgentTeamPresetListResponse>("agent-teams/presets");
  const presets = (presetList.data?.presets ?? []).filter(
    (preset) => !teamId || String(preset.id) === teamId,
  );
  const { states, refresh: refreshSummaries } = useObservations<AgentTeamSummary>(
    presets.map((preset) => `agent-teams/presets/${preset.id}/summary`),
  );
  const summaries = states.map((item) => item.data);
  const scopeRepos = useObservation<RepositoryListResponse>(
    ready && scopeId ? `factory/repositories?scope_id=${scopeId}` : null,
  );
  const providerWork = useObservation<WorkListResponse>(
    ready && provider ? `factory/work-items?provider=${encodeURIComponent(provider)}&limit=100` : null,
  );
  const scopeTeamIds = scopeId
    ? new Set((scopeRepos.data?.repositories ?? []).map((repo) => repo.team.id))
    : null;
  const visible = presets
    .map((preset, index) => ({ preset, summary: summaries[index] }))
    .filter(({ preset }) => teamVisible(preset, { provider, scopeTeamIds }));
  const visiblePresets = visible.map((entry) => entry.preset);
  const visibleSummaries = visible.map((entry) => entry.summary);
  const rows = filterNeedsYouRows(needsYouRows(visiblePresets, visibleSummaries), {
    scopeId: scopeId ? Number(scopeId) : null,
    provider: provider
      ? {
          workItemIds: new Set((providerWork.data?.items ?? []).map((item) => item.item.id)),
          complete: providerWork.data !== null && !providerWork.data.has_more,
        }
      : null,
  });
  const summariesLoading =
    presetList.data === null || states.some((item) => item.data === null && item.error === null);
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-baseline gap-3">
          <h2 className="text-2xl font-semibold">Overview</h2>
          {state.data && (
            <span className="text-sm text-muted-foreground">
              Updated <RelativeTime value={state.data.generated_at} />
            </span>
          )}
        </div>
        <Button
          variant="outline"
          onClick={() => void Promise.all([state.refresh(), presetList.refresh(), refreshSummaries()])}
        >
          Refresh
        </Button>
      </div>
      <FactoryFilters />
      <ReadStatus state={state} />
      {/* Read only after the overview itself loaded, so a failing overview stays one request. */}
      {state.data && <OperationsFindings />}
      {state.data && (
        <>
          <StatusLine data={state.data} teams={presetList.data ? presets.length : null} />
          <NeedsYou rows={rows} loading={summariesLoading} />
          <CampaignProgress presets={visiblePresets} summaries={visibleSummaries} />
          <StatusTiles counts={state.data.counts} params={params} />
          <section aria-label="Teams" className="space-y-2">
            <h3 className="text-lg font-semibold">Teams</h3>
            <ReadStatus state={presetList} />
            {presetList.data && visiblePresets.length === 0 && <p>No teams to show.</p>}
            <div className="grid gap-4 lg:grid-cols-2">
              {visiblePresets.map((preset) => (
                <TeamSummaryCard
                  key={preset.id}
                  presetId={preset.id}
                  name={preset.name}
                  running={preset.autonomy_enabled}
                  showChecked={false}
                  compact
                  slotNames={Object.fromEntries(preset.slots.map((slot) => [slot.id, slot.display_name]))}
                />
              ))}
            </div>
          </section>
          <ActiveWork query={query} viewAll={workLink(params, "open")} />
          <details className={`${panel} space-y-2 py-3`}>
            <summary className="cursor-pointer font-semibold">Automation details</summary>
            <p>
              Runtime {state.data.automation.runtime.mode.replaceAll("_", " ")}{" "}
              · scheduler {state.data.automation.runtime.scheduler_state}
            </p>
            <Time
              value={state.data.automation.runtime.observed_at}
              label="Runtime checked"
            />
            {state.data.automation.runtime.mode === "recovery_only" && (
              <p>
                Recovery-only mode blocks normal intake for all
                repositories that are switched on. Details of the recovery
                target stay in Director recovery.
              </p>
            )}
            {state.data.automation.runtime.scheduler_state === "stopped" && (
              <p>Scheduler stopped: normal intake is blocked.</p>
            )}
            {state.data.automation.runtime.mode === "unknown" && (
              <p>
                Runtime evidence is unknown; configuration alone does not
                establish intake health.
              </p>
            )}
            <p>
              {state.data.automation.configured_scopes} repositories set up ·{" "}
              {state.data.automation.enabled_scopes} switched on ·{" "}
              {state.data.automation.paused_scopes} paused
            </p>
            <p>
              Intake: {state.data.automation.intake_eligible_scopes} ready ·{" "}
              {state.data.automation.intake_blocked_scopes} blocked ·{" "}
              {state.data.automation.intake_unknown_scopes} not known
            </p>
            <p>
              Repositories ready for intake: {state.data.automation.stale_scopes} with
              an old GitHub check · {state.data.automation.never_polled_scopes} never
              checked. Intake still depends on access, readiness and capacity.
            </p>
          </details>
          {state.data.automation.configured_scopes === 0 && (
            <div className={panel}>
              <h3 className="font-semibold">
                {filtered
                  ? "No scopes match these filters"
                  : "No watched repositories yet"}
              </h3>
              <div className="flex flex-wrap gap-4">
                {filtered ? (
                  <Link className={anchor} to="/">
                    Clear filters
                  </Link>
                ) : (
                  <Link className={destination} to="/repositories/new">
                    Add a repository
                  </Link>
                )}
                <Link className={destination} to="/agent-bridge">
                  Open live sessions
                </Link>
                <Link className={destination} to="/harnesses">
                  Open Harnesses and configuration
                </Link>
              </div>
              <p>
                Native configuration remains available without setting up a
                factory.
              </p>
            </div>
          )}
          <section className="flex flex-wrap items-baseline justify-between gap-3">
            <h3 className="text-lg font-semibold">Repositories</h3>
            <p>
              {state.data.automation.configured_scopes} watched ·{" "}
              {state.data.automation.enabled_scopes} on ·{" "}
              {state.data.automation.intake_blocked_scopes} need attention
            </p>
            <Link className={anchor} to="/repositories">
              View all ›
            </Link>
          </section>
        </>
      )}
    </section>
  );
}

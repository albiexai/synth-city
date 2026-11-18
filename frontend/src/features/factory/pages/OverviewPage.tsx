import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { TeamSummaries } from "@/features/agent-teams/TeamSummaryCard";
import type { AgentTeamPresetListResponse, AgentTeamSummary } from "@/types/agentTeams";
import type { OverviewResponse, RepositoryListResponse } from "@/types/factory";
import { ActiveWork } from "../components/overview/ActiveWork";
import { CampaignProgress } from "../components/overview/CampaignProgress";
import { NeedsYou } from "../components/overview/NeedsYou";
import { OperationsFindings } from "../components/OperationsFindings";
import { needsYouRows } from "../components/overview/needsYou";
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

export function OverviewPage() {
  const { params, ready } = useFactoryFilters();
  const query = factoryQuery(params);
  const state = useObservation<OverviewResponse>(
    ready ? `factory/overview${query ? `?${query}` : ""}` : null,
  );
  const filtered = Boolean(query);
  // The same reads as the team cards below, so they add no request.
  const teamId = params.get("team_id");
  const presetList = useObservation<AgentTeamPresetListResponse>("agent-teams/presets");
  const presets = (presetList.data?.presets ?? []).filter(
    (preset) => !teamId || String(preset.id) === teamId,
  );
  // Repository and Assigned-harness filters (#1141, #1142). With either one set, Needs you, Progress and the team
  // cards all read the same separately keyed, filtered summary of each matching team (one request for each team).
  // Without them the page makes exactly the unfiltered reads it always made.
  const needsQuery = new URLSearchParams();
  for (const key of ["scope_id", "provider"]) {
    const value = params.get(key);
    if (value) needsQuery.set(key, value);
  }
  const needsFilter = needsQuery.toString();
  // Which teams the filter keeps, without a new wire field: a harness filter needs a slot of that harness, a
  // repository filter needs the team that owns it (the shared repository list). A list that cannot prove the
  // owner (not loaded, or longer than one page) keeps every team rather than guess.
  const repositories = useObservation<RepositoryListResponse>("factory/repositories?limit=100");
  const scopeFilter = params.get("scope_id");
  const providerFilter = params.get("provider");
  const owningTeam =
    scopeFilter && repositories.data && !repositories.data.has_more
      ? repositories.data.repositories.find((row) => String(row.scope_id) === scopeFilter)?.team.id
      : undefined;
  const matching = needsFilter
    ? presets.filter(
        (preset) =>
          (!providerFilter || preset.slots.some((slot) => slot.provider === providerFilter)) &&
          (owningTeam === undefined || preset.id === owningTeam),
      )
    : presets;
  const { states, refresh: refreshSummaries } = useObservations<AgentTeamSummary>(
    needsFilter ? [] : presets.map((preset) => `agent-teams/presets/${preset.id}/summary`),
  );
  const { states: needsStates, refresh: refreshNeeds } = useObservations<AgentTeamSummary>(
    needsFilter ? matching.map((preset) => `agent-teams/presets/${preset.id}/summary?${needsFilter}`) : [],
  );
  const needsReads = needsFilter ? needsStates : states;
  const summaries = needsReads.map((item) => item.data);
  const rows = needsYouRows(matching, summaries);
  const summariesLoading =
    presetList.data === null || needsReads.some((item) => item.data === null && item.error === null);
  // A failed read is not an empty list: say so instead of "Nothing needs you right now."
  // A failed refresh keeps the earlier data and sets the error, so the error alone decides.
  const needsFailed = needsFilter !== "" && needsStates.some((item) => item.error !== null);
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
          onClick={() => void Promise.all([state.refresh(), presetList.refresh(), refreshSummaries(), refreshNeeds()])}
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
          {needsFailed && (
            <p role="alert" className="rounded-lg border border-destructive px-4 py-2.5 text-sm">
              Could not read what needs you for these filters. Refresh to try again.
            </p>
          )}
          {!(needsFailed && rows.length === 0) && <NeedsYou rows={rows} loading={summariesLoading} />}
          <CampaignProgress presets={matching} summaries={summaries} />
          <StatusTiles counts={state.data.counts} params={params} />
          <TeamSummaries
            teamId={teamId}
            onOverview
            filterQuery={needsFilter}
            onlyIds={needsFilter ? matching.map((preset) => preset.id) : null}
          />
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

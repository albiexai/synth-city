import { Link } from "react-router-dom";
import type {
  CampaignHealthResponse,
  HealthBottleneckStage,
  HealthCost,
  HealthItemTimeBand,
} from "@/types/factory";
import { formatDuration } from "../../auditSummary";
import type { ReadState } from "../../reads";
import { RelativeTime } from "../RelativeTime";
import { anchor } from "../styles";

/** Where "Live retrospective" goes. The Retrospectives list shows the team's live campaign today. The live page of
 * #1253 changes this one function to /insights/retrospectives/{campaign id}, which the page then gets as a prop. */
function campaignLiveLink(teamId: number) {
  return `/insights/retrospectives?team_id=${teamId}&status=live`;
}

// What a reason code means to a person. Unknown is never shown as zero.
const REASONS: Record<string, string> = {
  cost_not_recorded: "the cost is not recorded yet",
  cost_section_invalid: "the recorded cost could not be read",
  no_snapshot: "no snapshot recorded",
  snapshot_without_item_time: "the snapshot has no item time",
  snapshot_without_bottlenecks: "the snapshot has no ranking",
  no_attempts_in_window: "no work in the window",
  actor_unavailable: "the actor is not known",
  harness_not_read: "its harness is not read yet",
  before_recording: "ran before recording began",
};
const reasonText = (reason: string) => REASONS[reason] ?? reason.replaceAll("_", " ");
const reasonsText = (reasons: string[]) => (reasons.length ? reasons.map(reasonText).join(", ") : "not recorded");

function Unknown({ reasons }: { reasons: string[] }) {
  return (
    <span className="text-muted-foreground">
      <em>unknown</em> · {reasonsText(reasons)}
    </span>
  );
}

function band(row: HealthItemTimeBand) {
  const n = `n = ${row.timed}`;
  if (row.median_seconds === null || row.timed === 0) {
    return (
      <li key={row.band} className="wrap-anywhere">
        <span className="text-muted-foreground">
          {row.band} lines: <em>unknown</em> · {reasonsText(row.unknown_reasons)}
        </span>
      </li>
    );
  }
  return (
    <li key={row.band} className="wrap-anywhere">
      <strong className="text-base">{formatDuration(row.median_seconds)}</strong>{" "}
      <span className="text-xs text-muted-foreground">median</span>
      <span className="block text-xs text-muted-foreground">
        {row.band} lines · p85 {row.p85_seconds === null ? "unknown" : formatDuration(row.p85_seconds)} · {n}
        {row.small_sample && " · small sample"}
        {row.coverage !== "full" && ` · ${row.coverage}`}
      </span>
    </li>
  );
}

function ItemTime({ data }: { data: CampaignHealthResponse }) {
  const item = data.item_time;
  if (!item) return <Unknown reasons={data.item_time_reason ? [data.item_time_reason] : []} />;
  const bands = item.bands.filter((row) => row.timed > 0 || row.items > 0);
  if (item.timed === 0 || bands.length === 0) {
    return <Unknown reasons={item.unknown_reasons.length ? item.unknown_reasons : ["no_timed_items"]} />;
  }
  return (
    <>
      <ul className="space-y-1">{bands.map(band)}</ul>
      {item.coverage !== "full" && (
        <span className="block text-xs text-muted-foreground">
          {item.coverage}
          {item.unknown_reasons.length > 0 && ` · ${reasonsText(item.unknown_reasons)}`}
        </span>
      )}
    </>
  );
}

const money = (value: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: value >= 100 ? 0 : 2,
  }).format(value);

function Cost({ cost }: { cost: HealthCost }) {
  if (cost.value_usd === null) return <Unknown reasons={cost.reasons} />;
  const covers =
    cost.members_total !== null && cost.members_known !== null
      ? `covers ${cost.members_known} of ${cost.members_total} members`
      : null;
  return (
    <>
      <strong className="text-base">≈ {money(cost.value_usd)}</strong>{" "}
      {cost.coverage !== "full" && (
        <span className="rounded border px-1.5 text-xs font-medium">{cost.coverage}</span>
      )}
      {(covers || cost.prices_as_of) && (
        <span className="block text-xs text-muted-foreground">
          {[covers, cost.prices_as_of && `prices as of ${cost.prices_as_of}`].filter(Boolean).join(" · ")}
        </span>
      )}
      {cost.unknown_members.map((member) => (
        <span key={member.member} className="block text-xs text-muted-foreground wrap-anywhere">
          {member.member} unknown: {reasonText(member.reason)}
        </span>
      ))}
    </>
  );
}

/** "Waiting for the leader" or "Required check: e2e": the stage with the actor that had to act. */
function stageLabel(stage: HealthBottleneckStage) {
  if (stage.group === "waiting") return `Waiting for ${stage.actor}`;
  const name = stage.stage.replaceAll("_", " ");
  return `${name.charAt(0).toUpperCase()}${name.slice(1)}: ${stage.actor}`;
}

function Bottleneck({ data }: { data: CampaignHealthResponse }) {
  const window = data.bottleneck;
  if (!window) return <Unknown reasons={data.bottleneck_reason ? [data.bottleneck_reason] : []} />;
  const top = window.state === "ranked" ? window.stages[0] : null;
  const shown = top ?? window.largest_observed;
  if (window.state === "unknown" && !shown) return <Unknown reasons={window.unknown_reasons} />;
  return (
    <>
      {top ? (
        <strong className="text-base wrap-anywhere">{stageLabel(top)}</strong>
      ) : (
        <span className="wrap-anywhere">
          <strong className="text-base">None above the thresholds</strong>
          {shown && <span className="block text-xs text-muted-foreground">Largest: {stageLabel(shown)}</span>}
        </span>
      )}
      {shown && (
        <span className="block text-xs text-muted-foreground">
          {formatDuration(shown.credited_seconds)} · {Math.round(shown.share * 100)}% of waiting and process time ·{" "}
          {shown.items} {shown.items === 1 ? "item" : "items"}
          {shown.small_sample && " · small sample"}
        </span>
      )}
      <span className="block text-xs text-muted-foreground">
        {window.label}
        {window.coverage !== "full" && ` · ${window.coverage}`}
        {window.unknown_reasons.length > 0 && ` · ${reasonsText(window.unknown_reasons)}`}
      </span>
    </>
  );
}

/** #1256: the campaign health block of the Overview Progress card. It only reads and links. It never applies anything. */
export function CampaignHealth({
  teamId,
  campaignId,
  state,
}: {
  teamId: number;
  campaignId: number;
  state: ReadState<CampaignHealthResponse>;
}) {
  const data = state.data;
  return (
    <div className="space-y-2 border-t border-dashed pt-3 text-sm" role="group" aria-label="Campaign health" data-campaign-id={campaignId}>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h5 className="font-semibold">Campaign health</h5>
        <Link className={anchor} to={campaignLiveLink(teamId)}>
          Live retrospective ›
        </Link>
      </div>
      {!data && !state.error && <p className="text-muted-foreground">Reading the campaign&apos;s health…</p>}
      {!data && state.error && (
        <p role="note" className="text-muted-foreground">
          The campaign&apos;s health could not be read. <em>unknown</em>, not zero.
        </p>
      )}
      {data && (
        <dl className="grid gap-3 sm:grid-cols-3">
          <div className="space-y-1">
            <dt className="text-muted-foreground">Item time, dispatch to merge</dt>
            <dd>
              <ItemTime data={data} />
            </dd>
          </div>
          <div className="space-y-1">
            <dt className="text-muted-foreground">Cost so far (API-equivalent)</dt>
            <dd>
              <Cost cost={data.cost} />
            </dd>
          </div>
          <div className="space-y-1">
            <dt className="text-muted-foreground">Top bottleneck, last 4 hours</dt>
            <dd>
              <Bottleneck data={data} />
            </dd>
          </div>
        </dl>
      )}
      {data?.as_of && (
        <p className="text-xs text-muted-foreground">
          {data.source === "frozen" ? "Frozen version" : "Calculated"} up to <RelativeTime value={data.as_of} />
          {state.error && " · the last read failed, so this may be old"}
        </p>
      )}
    </div>
  );
}

import { Button } from "@/components/ui/button";
import type { DeferredIssue, MilestoneStatesResponse, WaitingIssue } from "@/types/factory";
import { useObservation } from "../reads";
import { GithubLink } from "./GithubLink";
import { ReadStatus } from "./ReadStatus";
import { RelativeTime } from "./RelativeTime";
import { panel } from "./styles";

const row = "flex flex-wrap items-baseline gap-x-2 gap-y-1 text-sm";

function Issue({ item }: { item: Pick<DeferredIssue, "issue_number" | "repository"> }) {
  return (
    <GithubLink identity={item.repository.github} kind="issues" id={item.issue_number}>
      {item.repository.display_name}#{item.issue_number}
    </GithubLink>
  );
}

function Waiting({ item }: { item: WaitingIssue }) {
  const { repository } = item;
  return (
    <li className={row}>
      {repository ? (
        <Issue item={{ issue_number: item.issue_number, repository }} />
      ) : (
        <>
          <span>#{item.issue_number}</span>
          <span className="text-muted-foreground">repository not recorded</span>
        </>
      )}
      <span>
        Waiting: no team for milestone <strong className="font-medium">{item.milestone}</strong>
      </span>
      <RelativeTime value={item.observed_at} />
    </li>
  );
}

function Deferred({ item, showTeam }: { item: DeferredIssue; showTeam: boolean }) {
  return (
    <li className={row}>
      <Issue item={item} />
      <span className="min-w-0 wrap-break-word">{item.issue_title}</span>
      <span>
        {item.target_milestone ? (
          <>
            Deferred to milestone <strong className="font-medium">{item.target_milestone}</strong>
          </>
        ) : (
          "Deferred (milestone not recorded)"
        )}
      </span>
      {showTeam && <span className="text-muted-foreground">from {item.team.name}</span>}
      <RelativeTime value={item.deferred_at} />
    </li>
  );
}

// Issues whose milestone moved to another team, and ready issues that no team has the milestone for (#1337).
// The view is read-only and shows nothing while there is nothing to show.
export function MilestoneStates({ teamId, scopeId }: { teamId?: number; scopeId?: number }) {
  const query = new URLSearchParams();
  if (teamId) query.set("team_id", String(teamId));
  if (scopeId) query.set("scope_id", String(scopeId));
  const text = query.toString();
  const state = useObservation<MilestoneStatesResponse>(`factory/milestone-states${text ? `?${text}` : ""}`);
  const data = state.data;
  const waiting = Array.isArray(data?.waiting) ? data.waiting : [];
  const deferred = Array.isArray(data?.deferred) ? data.deferred : [];
  if (!state.error && !waiting.length && !deferred.length) return null;
  return (
    <section aria-labelledby="milestone-states-heading" className={panel} data-testid="milestone-states">
      <h3 id="milestone-states-heading" className="text-base font-semibold">
        Milestone moves
      </h3>
      <ReadStatus state={state} />
      {state.error && state.stopped && (
        <Button type="button" variant="outline" onClick={() => void state.refresh()}>
          Try again
        </Button>
      )}
      {deferred.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-sm font-medium">Deferred by a milestone move</h4>
          <ul className="space-y-2">
            {deferred.map((item) => (
              <Deferred key={item.work_item_id} item={item} showTeam={!teamId} />
            ))}
          </ul>
          {data?.deferred_truncated && (
            <p className="text-sm text-muted-foreground">More deferred issues exist than this list shows.</p>
          )}
        </div>
      )}
      {waiting.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-sm font-medium">Waiting for a team</h4>
          <ul className="space-y-2">
            {waiting.map((item, index) => (
              <Waiting
                key={item.repository ? `${item.repository.github.owner}/${item.repository.github.name}#${item.issue_number}` : `unrecorded-${index}`}
                item={item}
              />
            ))}
          </ul>
          {data?.waiting_truncated && (
            <p className="text-sm text-muted-foreground">More waiting issues exist than this list shows.</p>
          )}
        </div>
      )}
    </section>
  );
}

import { useObservation } from "../reads";
import { RelativeTime } from "./RelativeTime";

// #941: stuck states and silent background failures the factory found in itself. Read-only and public-safe;
// it shows only while a finding is open, so a healthy factory adds nothing to the page.
export interface OperationsFinding {
  id: number;
  kind: string;
  subject: string;
  detail: string;
  status: string;
  occurrences: number;
  first_seen_at: string;
  last_seen_at: string;
}
export interface OperationsFindingsResponse {
  health: string;
  reasons: string[];
  open_findings: number | null;
  findings: OperationsFinding[];
}

const REASON_WORDS: Record<string, string> = {
  verifying_after_merge: "Still verifying after the pull request merged",
  item_skipped: "Skipped by verification, poll after poll",
  admission_missing: "A ready issue has no work item",
  poll_rolled_back: "A poll failed and its writes were discarded",
  findings_unreadable: "The findings could not be read",
};

export function OperationsFindings() {
  const state = useObservation<OperationsFindingsResponse>("health/findings");
  const open = (state.data?.findings ?? []).filter((finding) => finding.status === "open");
  if (open.length === 0) return null;
  return (
    <section
      aria-labelledby="operations-findings-heading"
      className="rounded-lg border-2 border-amber-500/60 bg-card"
    >
      <h3
        id="operations-findings-heading"
        className="flex items-center gap-2 border-b border-amber-500/40 px-4 py-3 text-lg font-semibold"
      >
        Needs a look: operations
        <span className="rounded-full bg-amber-500 px-2 text-sm font-semibold text-black">
          {open.length}
        </span>
      </h3>
      <ul>
        {open.map((finding) => (
          <li key={finding.id} className="border-b px-4 py-3 last:border-b-0">
            <p className="font-semibold">{REASON_WORDS[finding.kind] ?? finding.kind}</p>
            <p className="text-sm">{finding.detail}</p>
            <p className="text-sm text-muted-foreground">
              Open since <RelativeTime value={finding.first_seen_at} /> · seen {finding.occurrences}{" "}
              {finding.occurrences === 1 ? "time" : "times"} · <code>{finding.kind}</code>
            </p>
          </li>
        ))}
      </ul>
    </section>
  );
}

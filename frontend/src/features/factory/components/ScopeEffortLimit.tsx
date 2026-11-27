import type { PlanRefusal } from "@/types/factory";

export function ScopeEffortLimit({ minutes }: { minutes?: number | null }) {
  if (minutes == null) return null;

  return (
    <p className="text-xs text-muted-foreground">
      Limit: scope ceiling (no estimate) · {minutes} min
    </p>
  );
}

/** The accepted plan estimate next to the scope's plan limit. It is plain text: an estimate over the limit is not a warning. */
export function PlanEstimate({ estimate, limit }: { estimate?: number | null; limit?: number | null }) {
  if (estimate == null || limit == null) return null;

  return (
    <p className="text-xs text-muted-foreground">
      Plan estimate: {estimate} min · plan limit: {limit} min
    </p>
  );
}

export function PlanRefusalNotice({ refusal }: { refusal?: PlanRefusal | null }) {
  if (!refusal) return null;

  return (
    <div className="mt-1 whitespace-normal text-xs text-amber-800 dark:text-amber-300">
      <p className="font-medium">Plan refused</p>
      <code className="break-all">{refusal.code}</code>
      <p>Estimate up to {refusal.estimate_minutes} min · plan limit {refusal.limit_minutes} min</p>
      <p>{refusal.split_suggestion}</p>
    </div>
  );
}

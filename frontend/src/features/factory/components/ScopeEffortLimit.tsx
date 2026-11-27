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

export function ScopeEffortLimit({ minutes }: { minutes?: number | null }) {
  if (minutes == null) return null;

  return (
    <p className="text-xs text-muted-foreground">
      Limit: scope ceiling (no estimate) · {minutes} min
    </p>
  );
}

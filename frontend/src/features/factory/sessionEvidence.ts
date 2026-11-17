import { useEffect, useState } from "react";
import { agentStatusView } from "@/lib/agentStatus";
import type { SessionAssociation } from "@/types/factory";

/**
 * The words for the owner's session in Technical details (#1073).
 *
 * The strict session state stays as it is: "bound" needs a live session with a capability token and exactly one
 * verified pane, and it feeds the offline-slot hint and the owner-offline escalation. When that state is "unknown",
 * the same member's agent status can still say what it saw, for example "online, not bound to a verified pane".
 * An expired or missing agent status stays plain "unknown". Nothing here can make a session "bound".
 */
export function ownerSessionEvidence(
  session: SessionAssociation,
  now: number,
): string {
  if (session.state !== "unknown") return session.state;
  const view = agentStatusView(session.agent_status, now);
  if (view.session.state === "unknown") return "unknown";           // also what agentStatusView says for an expired status
  const activity = view.activity.state === "unknown" ? "" : `, ${view.activity.state}`;
  return view.session.state === "online"
    ? `online${activity}, not bound to a verified pane`
    : `offline${activity}, no verified pane`;
}

/** The clock, read again every so often, so an agent status that expires is shown as unknown again. */
export function useNow(periodMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), periodMs);
    return () => window.clearInterval(timer);
  }, [periodMs]);
  return now;
}

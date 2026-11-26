import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { operatorHeaders } from "@/features/agent-teams/api";
import { useOperatorTokenFlow } from "@/features/agent-teams/useOperatorTokenFlow";
import { apiClient } from "@/lib/api";
import { anchor } from "./styles";

type Workspace = {
  workspace_id: number;
  path: string;
  state: "free" | "leased" | "blocked" | "missing";
  reason: string | null;
  leased_issue_number: number | null;
};

type WorkspaceReadiness = {
  scope_id: number;
  workspaces: Workspace[];
  waiting_items: { work_item_id: number; issue_number: number; reason: string }[];
};

const workspaceStates = { free: "Free", leased: "Leased", blocked: "Blocked", missing: "Missing" };
const waitingReasons: Record<string, string> = {
  repository_identity_mismatch: "A workspace does not match this repository.",
  workspace_missing: "A workspace is missing.",
  repository_identity_unavailable: "The repository could not be verified.",
  workspace_identity_unavailable: "A workspace could not be verified.",
  workspace_provision_incomplete: "Workspace provisioning is incomplete.",
  workspace_preparation_failed: "Workspace preparation failed.",
  no_free_workspace: "No workspace is free.",
};

export function RepositoryWorkspaces({ scopeId }: { scopeId: number }) {
  const { hash } = useLocation();
  const card = useRef<HTMLElement>(null);
  const { withOperatorToken, tokenDialog } = useOperatorTokenFlow("to read workspace status");
  const [data, setData] = useState<WorkspaceReadiness | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (hash === "#workspaces") {
      card.current?.scrollIntoView?.({ block: "start" });
      card.current?.focus({ preventScroll: true });
    }
  }, [hash]);

  async function load() {
    setLoading(true);
    setData(null);
    setFailed(false);
    try {
      const result = await withOperatorToken((token) =>
        apiClient<WorkspaceReadiness>(`agent-teams/github-scopes/${scopeId}/workspaces/readiness`, {
          headers: operatorHeaders(token),
        }),
      );
      if (result.scope_id !== scopeId) throw new Error("Workspace status scope changed");
      setData(result);
    } catch (error) {
      if (!(error instanceof Error && error.message === "The Director key is required for this action.")) {
        setFailed(true);
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <section
      ref={card}
      id="workspaces"
      tabIndex={-1}
      aria-labelledby="repository-workspaces-title"
      aria-busy={loading}
      className="min-w-0 scroll-mt-6 space-y-3 rounded-lg border bg-card p-4 sm:p-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="repository-workspaces-title" className="text-base font-semibold">Workspaces</h3>
        <Button type="button" variant="outline" size="sm" onClick={() => void load()} disabled={loading}>
          {data ? "Refresh workspace status" : "Load workspace status"}
        </Button>
      </div>
      {loading && <p role="status" className="text-sm text-muted-foreground">Loading workspace status…</p>}
      {failed && <p role="alert" className="text-sm text-destructive">Workspace status could not be loaded.</p>}
      {data && (
        <div className="space-y-3 text-sm">
          {data.workspaces.length ? (
            <ul aria-label="Dispatch workspaces" className="divide-y">
              {data.workspaces.map((workspace) => (
                <li key={workspace.workspace_id} className="grid min-w-0 gap-1 py-2 sm:grid-cols-[minmax(0,1fr)_14rem] sm:gap-4">
                  <code className="break-all text-xs text-muted-foreground">{workspace.path}</code>
                  <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="font-medium">
                      {workspaceStates[workspace.state]}
                      {workspace.state === "leased" && workspace.leased_issue_number != null && ` to #${workspace.leased_issue_number}`}
                    </span>
                    {workspace.state === "blocked" && workspace.reason && (
                      <code className="break-all text-xs text-muted-foreground">{workspace.reason}</code>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          ) : <p className="text-muted-foreground">No workspaces are configured.</p>}
          <div className="border-t pt-3">
            <h4 className="font-medium">Waiting for a workspace</h4>
            {data.waiting_items.length ? (
              <ul className="mt-1 space-y-1">
                {data.waiting_items.map((item) => (
                  <li key={item.work_item_id} className="flex flex-wrap items-baseline gap-x-2">
                    <Link className={anchor} to={`/work/${item.work_item_id}`}>#{item.issue_number}</Link>
                    <span className="min-w-0 break-words text-muted-foreground">{waitingReasons[item.reason] ?? item.reason}</span>
                  </li>
                ))}
              </ul>
            ) : <p className="mt-1 text-muted-foreground">No items are waiting for a workspace.</p>}
          </div>
        </div>
      )}
      {tokenDialog}
    </section>
  );
}

/**
 * Scope control for a Claude Code Harnesses page: the global scope of one OS user, or a project.
 * The choice lives in the page's URL (?scope_user= and ?project=). The page names the directories it reads and
 * says when the controller cannot read or write the global one.
 */
import { useEffect, useState, type ReactNode } from 'react';
import { apiClient } from '@/lib/api';
import { useProjectContext } from '@/contexts/ProjectContext';
import { ProjectSelector } from '@/features/projects/ProjectSelector';
import type { HarnessScopeResponse, HarnessScopeUser } from '@/types/harnessScope';

function useHarnessScope(): HarnessScopeResponse | null {
  const [scope, setScope] = useState<HarnessScopeResponse | null>(null);
  useEffect(() => {
    let cancelled = false;
    apiClient<HarnessScopeResponse>('harness-scope')
      .then((response) => { if (!cancelled) setScope(response); })
      .catch(() => { if (!cancelled) setScope(null); });
    return () => { cancelled = true; };
  }, []);
  return scope;
}

function userLabel(user: HarnessScopeUser): string {
  const notes = [user.is_controller ? 'controller' : 'agent user', user.is_default ? 'default' : null].filter(Boolean);
  return `${user.name} (${notes.join(', ')})`;
}

export function ScopeSelector({ showGlobalUser = true }: { showGlobalUser?: boolean }) {
  const { activeProject, scopeUser, setScopeUser } = useProjectContext();
  const scope = useHarnessScope();
  const users = scope?.users ?? [];
  const chosen = users.find((user) => user.name === scopeUser) ?? users.find((user) => user.is_default) ?? null;
  const showUserPicker = showGlobalUser && users.length > 1;

  return (
    <div className="space-y-2" role="group" aria-label="Scope">
      <div className="flex flex-wrap items-end gap-3 text-sm">
        {showUserPicker && (
          <div className="grid gap-1">
            <label htmlFor="scope-user" className="font-medium">Global scope of</label>
            <select
              id="scope-user"
              className="min-w-48 rounded border bg-background p-2"
              value={chosen?.name ?? ''}
              onChange={(event) => {
                const name = event.target.value;
                // The default user needs no URL value, so the address stays short.
                setScopeUser(name === scope?.default_user ? null : name);
              }}
            >
              {users.map((user) => <option key={user.name} value={user.name}>{userLabel(user)}</option>)}
            </select>
          </div>
        )}
        <ProjectSelector />
      </div>
      {showGlobalUser && chosen && (
        <div className="space-y-1 text-sm text-muted-foreground">
          <p>
            Global scope reads <code className="break-all rounded bg-muted px-1">{chosen.claude_dir}</code>
            {users.length > 1 ? ` as the user ${chosen.name}` : ''}.
            {activeProject && <> Project scope reads <code className="break-all rounded bg-muted px-1">{activeProject.path}/.claude</code>.</>}
          </p>
          {!chosen.exists && <p>This directory does not exist yet.</p>}
          {chosen.exists && !chosen.readable && (
            <p role="alert" className="text-amber-700 dark:text-amber-400">The controller cannot read this directory. Its items cannot be listed.</p>
          )}
          {chosen.readable && !chosen.writable && (
            <p role="status" className="text-amber-700 dark:text-amber-400">The controller cannot write to this directory, so changes here will be refused.</p>
          )}
        </div>
      )}
    </div>
  );
}

/** A page reads its data when it opens. A new global user is a new page, so it opens again and reads again. */
export function ScopedPage({ children }: { children: ReactNode }) {
  const { scopeUser } = useProjectContext();
  return <div key={scopeUser ?? 'default'} className="space-y-4">{children}</div>;
}

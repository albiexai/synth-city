/**
 * Project Context for shared project state across the application
 */
import {
  createContext,
  useContext,
  useState,
  useCallback,
  useEffect,
  useMemo,
  type ReactNode,
} from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import { apiClient } from '@/lib/api';
import { fetchAgentTeamPresets } from '@/features/agent-teams/api';
import { projectScopedSurface } from '@/features/native-settings/projectScope';
import type {
  ProjectResponse,
  ProjectListResponse,
  ProjectDiscoveryResponse,
  ProjectCreate,
} from '@/types/projects';

export interface KnownFolder {
  path: string;
  name: string;
  source: 'team' | 'tracked';
  /** For a team folder: the members that use it. */
  detail?: string;
}

function folderName(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? path;
}

function normalizePath(path: string): string {
  return path.replace(/\/+$/, '') || '/';
}

interface TeamSlot {
  provider: string;
  path: string;
  name: string;
}

function teamFoldersOf(slots: TeamSlot[]): KnownFolder[] {
  const byPath = new Map<string, string[]>();
  for (const slot of slots) byPath.set(slot.path, [...(byPath.get(slot.path) ?? []), slot.name]);
  return [...byPath.entries()].map(([path, members]) => ({
    path,
    name: folderName(path),
    source: 'team' as const,
    detail: members.join(', '),
  }));
}

// /harnesses/folders is the Projects page, not a harness.
function pageHarness(pathname: string): string | null {
  const parts = pathname.split('/').filter(Boolean);
  if (parts[0] !== 'harnesses' || parts.length < 2 || parts[1] === 'folders') return null;
  return parts[1];
}

interface ProjectContextType {
  projects: ProjectResponse[];
  /** The project that this page's URL (?project=) names, or null. Per page and per browser. */
  activeProject: ProjectResponse | null;
  /** The folders this page offers. A harness page offers only its own members' folders. */
  pageFolders: KnownFolder[];
  knownFolders: KnownFolder[];
  setProjectPath: (path: string | null) => void;
  /** The OS user whose global scope this Harnesses page shows (?scope_user=), or null for the default. */
  scopeUser: string | null;
  setScopeUser: (name: string | null) => void;
  loading: boolean;
  error: string | null;
  fetchProjects: () => Promise<void>;
  addProject: (projectData: ProjectCreate) => Promise<ProjectResponse>;
  removeProject: (projectId: number) => Promise<void>;
  discoverProjects: (basePath: string) => Promise<ProjectCreate[]>;
  setActiveProject: (projectId: number) => Promise<void>;
  clearActiveProject: () => Promise<void>;
}

const ProjectContext = createContext<ProjectContextType | undefined>(undefined);

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<ProjectResponse[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [teamSlots, setTeamSlots] = useState<TeamSlot[] | null>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const { pathname } = useLocation();

  const fetchProjects = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiClient<ProjectListResponse>('projects');
      setProjects(response.projects);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to fetch projects');
    } finally {
      setLoading(false);
    }
  }, []);

  const addProject = useCallback(async (projectData: ProjectCreate) => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiClient<ProjectResponse>('projects', {
        method: 'POST',
        body: JSON.stringify(projectData),
      });
      await fetchProjects();
      return response;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to add project');
      throw err;
    } finally {
      setLoading(false);
    }
  }, [fetchProjects]);

  const removeProject = useCallback(async (projectId: number) => {
    setLoading(true);
    setError(null);
    try {
      await apiClient(`projects/${projectId}`, {
        method: 'DELETE',
      });
      await fetchProjects();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to remove project');
      throw err;
    } finally {
      setLoading(false);
    }
  }, [fetchProjects]);

  const discoverProjects = useCallback(async (basePath: string) => {
    setLoading(true);
    setError(null);
    try {
      const response = await apiClient<ProjectDiscoveryResponse>('projects/discover', {
        method: 'POST',
        body: JSON.stringify({ base_path: basePath }),
      });
      return response.discovered;
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to discover projects');
      throw err;
    } finally {
      setLoading(false);
    }
  }, []);

  const setActiveProject = useCallback(async (projectId: number) => {
    setLoading(true);
    setError(null);
    try {
      await apiClient('projects/active', {
        method: 'PUT',
        body: JSON.stringify({ project_id: projectId }),
      });
      await fetchProjects();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to set active project');
      throw err;
    } finally {
      setLoading(false);
    }
  }, [fetchProjects]);

  const clearActiveProject = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await apiClient('projects/active', {
        method: 'DELETE',
      });
      await fetchProjects();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear active project');
      throw err;
    } finally {
      setLoading(false);
    }
  }, [fetchProjects]);

  const knownFolders = useMemo<KnownFolder[]>(() => {
    const byPath = new Map<string, KnownFolder>();
    for (const folder of teamFoldersOf(teamSlots ?? [])) byPath.set(folder.path, folder);
    for (const project of projects) {
      const path = normalizePath(project.path);
      if (!byPath.has(path)) byPath.set(path, { path, name: project.name, source: 'tracked' });
    }
    return [...byPath.values()];
  }, [teamSlots, projects]);

  const harness = pageHarness(pathname);
  const pageFolders = useMemo<KnownFolder[]>(() => {
    if (harness === null) return knownFolders;
    if (teamSlots === null) return [];
    const teamPaths = new Set(teamSlots.map((slot) => slot.path));
    const byPath = new Map<string, KnownFolder>();
    for (const folder of teamFoldersOf(teamSlots.filter((slot) => slot.provider === harness))) byPath.set(folder.path, folder);
    for (const project of projects) {
      const path = normalizePath(project.path);
      if (!byPath.has(path) && !teamPaths.has(path)) byPath.set(path, { path, name: project.name, source: 'tracked' });
    }
    return [...byPath.values()];
  }, [harness, knownFolders, teamSlots, projects]);

  // The project comes from this page's URL. A missing or unknown value is no project, with no error.
  const scoped = projectScopedSurface(pathname) !== null;
  const projectParam = scoped ? searchParams.get('project') : null;
  const activeProject = useMemo<ProjectResponse | null>(() => {
    if (!projectParam) return null;
    const known = pageFolders.find((folder) => folder.path === normalizePath(projectParam));
    if (!known) return null;
    const tracked = projects.find((project) => normalizePath(project.path) === known.path);
    return {
      id: tracked?.id ?? -1,
      name: known.name,
      path: known.path,
      is_active: false,
      last_accessed: tracked?.last_accessed ?? '',
      created_at: tracked?.created_at ?? '',
    };
  }, [projectParam, pageFolders, projects]);

  const setProjectPath = useCallback((path: string | null) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (path) next.set('project', path);
      else next.delete('project');
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  const scopeUser = pathname.startsWith('/harnesses') ? searchParams.get('scope_user') : null;
  const setScopeUser = useCallback((name: string | null) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (name) next.set('scope_user', name);
      else next.delete('scope_user');
      return next;
    }, { replace: true });
  }, [setSearchParams]);

  // Membership is read again on each harness page entry, so a member folder moved since the last visit is not stale.
  useEffect(() => {
    let cancelled = false;
    fetchAgentTeamPresets()
      .then((response) => {
        if (cancelled) return;
        const slots: TeamSlot[] = [];
        for (const preset of response.presets) {
          for (const slot of preset.slots) {
            if (!slot.repo_path) continue;
            slots.push({ provider: slot.provider, path: normalizePath(slot.repo_path), name: slot.display_name });
          }
        }
        setTeamSlots(slots);
      })
      .catch(() => {
        if (!cancelled) setTeamSlots(null);
      });
    return () => {
      cancelled = true;
    };
  }, [harness]);

  // Fetch projects on mount
  useEffect(() => {
    fetchProjects();
  }, [fetchProjects]);

  return (
    <ProjectContext.Provider
      value={{
        projects,
        activeProject,
        pageFolders,
        knownFolders,
        setProjectPath,
        scopeUser,
        setScopeUser,
        loading,
        error,
        fetchProjects,
        addProject,
        removeProject,
        discoverProjects,
        setActiveProject,
        clearActiveProject,
      }}
    >
      {children}
    </ProjectContext.Provider>
  );
}

// For screens that work without the provider. It starts no request of its own.
// eslint-disable-next-line react-refresh/only-export-components
export function useOptionalProjectContext() {
  return useContext(ProjectContext);
}

// eslint-disable-next-line react-refresh/only-export-components
export function useProjectContext() {
  const context = useContext(ProjectContext);
  if (!context) {
    throw new Error('useProjectContext must be used within a ProjectProvider');
  }
  return context;
}

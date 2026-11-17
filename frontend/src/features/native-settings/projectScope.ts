// Native pages whose data depends on a project folder. Each one reads the
// project from its own URL (?project=<path>), not from a server-wide setting.
const PROJECT_SCOPED_SURFACES = new Set([
  'config', 'mcp', 'plugins', 'commands', 'hooks', 'permissions', 'agents',
  'skills', 'memory', 'backup', 'output-styles', 'plans',
])

/** The project-scoped native surface that a URL path shows, or null. */
export function projectScopedSurface(pathname: string): string | null {
  const parts = pathname.split('/').filter(Boolean)
  // A harness's own page (/harnesses/<id>) shows tile counts that follow the project too.
  if (parts[0] === 'harnesses' && parts.length === 2) return 'summary'
  const surface = parts[0] === 'harnesses' ? parts[2] : parts[0]
  return surface && PROJECT_SCOPED_SURFACES.has(surface) ? surface : null
}

// Surfaces whose backend routes carry the harness scope (#969): they read the global scope of the chosen
// OS user. Backup and Plans stay on the controller's own home, so they offer no user choice.
const GLOBAL_USER_SURFACES = new Set([
  'summary', 'config', 'mcp', 'plugins', 'commands', 'hooks', 'permissions', 'agents', 'skills', 'memory', 'output-styles',
])

/** True when the page at this surface reads the global scope of the user that the picker names. */
export function globalUserSurface(surface: string | null): boolean {
  return surface !== null && GLOBAL_USER_SURFACES.has(surface)
}

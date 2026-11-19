// The version of the user's Claude Code, as the Harnesses UI shows it (#1069).
//
// With a chosen non-controller user the backend sends `version_user` and either `version` or an unknown reason, read from
// that user's install. This text does NOT depend on the controller's `installed`: `installed` and `binary_path` describe
// the controller's binary, and an agent user's version is worth showing (or its unknown message) whether or not the
// controller has a `claude` of its own. Without `version_user` nothing here applies and callers keep their old output.

export const VERSION_UNKNOWN = "version unknown (cannot read the agent user's install)";

export interface ScopedVersionFields {
  version?: string | null;
  version_user?: string | null;
  version_source?: string | null;
  version_unknown_reason?: string | null;
}

/** "Version 2.1.326 (agentuser)", or the exact unknown message. Null when the status carries no scoped version. */
export function scopedVersionLine(status: ScopedVersionFields | null | undefined): string | null {
  if (!status?.version_user) return null;
  return status.version ? `Version ${status.version} (${status.version_user})` : (status.version_unknown_reason ?? VERSION_UNKNOWN);
}

/** The short form for a card or a tooltip: "v2.1.326", or the exact unknown message. Null with no scoped version. */
export function scopedVersionBadge(status: ScopedVersionFields | null | undefined): string | null {
  if (!status?.version_user) return null;
  return status.version ? `v${status.version}` : (status.version_unknown_reason ?? VERSION_UNKNOWN);
}

/**
 * The header's entry for a harness comes from /status, which carries the CONTROLLER's version. For claude-code the
 * registry (GET /providers) is the one source of the version, so its version fields replace the /status ones in every
 * case: the chosen user's version, the controller's own when the scope is the controller, and no version at all while
 * the registry is stale (see withoutClaudeVersion). A version is never kept from /status for claude-code, so loading
 * can not fall back to the controller's version as if it were the selected user's. Other harnesses are unchanged.
 */
export function withRegistryVersion<T extends ScopedVersionFields & { id?: string }>(entry: T, registry: ScopedVersionFields | null | undefined): T {
  if (entry.id !== "claude-code" || !registry) return entry;
  return {
    ...entry,
    version: registry.version ?? null,
    version_user: registry.version_user,
    version_source: registry.version_source,
    version_unknown_reason: registry.version_unknown_reason,
  };
}

/**
 * A registry fetched for another scope must not show that scope's Claude Code version. The version of every other
 * harness is the controller's whatever the scope, so only claude-code is cleared.
 */
export function withoutClaudeVersion<T extends ScopedVersionFields & { id?: string }>(entry: T): T {
  if (entry.id !== "claude-code") return entry;
  return { ...entry, version: null, version_user: undefined, version_source: undefined, version_unknown_reason: undefined };
}

/** One OS user whose global (~/.claude) scope a Harnesses page can show. */
export interface HarnessScopeUser {
  name: string
  is_controller: boolean
  is_default: boolean
  claude_dir: string
  exists: boolean
  readable: boolean
  writable: boolean
}

export interface HarnessScopeResponse {
  users: HarnessScopeUser[]
  default_user: string
}

export interface HarnessReadScope {
  user: string
  claude_dir: string
  exists: boolean
  readable: boolean
  /** Null for a configured Plans folder whose path and readability are undisclosed. */
  data_dir?: string | null
}

export function scopeUnreadable(scope: HarnessReadScope | null): boolean {
  return scope?.readable === false && scope.data_dir !== null
}

export function scopeReadDenial(error: unknown): string | null {
  if (error instanceof Error && 'status' in error && error.status === 403
    && error.message.startsWith('The controller cannot read ')) {
    return 'The controller cannot read this data folder.'
  }
  return null
}

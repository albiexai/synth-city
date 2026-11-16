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

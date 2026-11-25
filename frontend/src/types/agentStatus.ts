/** Display facts from the backend. They grant no authority. */
export interface AgentStatus {
  session_state: 'online' | 'offline' | 'unknown'
  activity_state: 'working' | 'idle' | 'stopped' | 'unknown'
  activity_reason: string
  wake_state: 'can_wake' | 'cannot_wake' | 'unknown'
  wake_reason: string | null
  idle_since: string | null
  checked_at: string
  valid_until: string
  /** Whether the member's Mail tool list is the deployed one (#960). An older backend omits both fields. */
  tool_catalog_state?: 'current' | 'stale' | 'unknown'
  tool_catalog_reason?: string | null
}

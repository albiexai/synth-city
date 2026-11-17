import { describe, expect, it } from 'vitest'
import { ownerSessionEvidence } from '../src/features/factory/sessionEvidence'
import type { AgentStatus } from '../src/types/agentStatus'
import type { SessionAssociation } from '../src/types/factory'

// #1073: the owner's session in Technical details uses the agent-status evidence when the strict state is "unknown".

const NOW = Date.parse('2026-11-17T12:00:00Z')
const status = (over: Partial<AgentStatus> = {}): AgentStatus => ({
  session_state: 'online', activity_state: 'unknown', activity_reason: 'provider_unsupported', wake_state: 'unknown',
  wake_reason: null, idle_since: null, checked_at: '2026-11-17T11:59:00Z', valid_until: '2026-11-17T12:05:00Z', ...over,
})
const session = (state: SessionAssociation['state'], agent_status?: AgentStatus | null): SessionAssociation => ({
  state, agent_status, bridge_target: null, observed_provider: null,
})

describe('the owner session wording', () => {
  it('says what the agent status saw when the strict state is unknown', () => {
    expect(ownerSessionEvidence(session('unknown', status()), NOW)).toBe('online, not bound to a verified pane')
    expect(ownerSessionEvidence(session('unknown', status({ activity_state: 'idle' })), NOW)).toBe('online, idle, not bound to a verified pane')
    expect(ownerSessionEvidence(session('unknown', status({ activity_state: 'working' })), NOW)).toBe('online, working, not bound to a verified pane')
    expect(ownerSessionEvidence(session('unknown', status({ session_state: 'offline' })), NOW)).toBe('offline, no verified pane')
  })

  it('stays plain "unknown" when the agent status is unknown, expired or missing', () => {
    expect(ownerSessionEvidence(session('unknown', status({ session_state: 'unknown' })), NOW)).toBe('unknown')
    expect(ownerSessionEvidence(session('unknown', status({ valid_until: '2026-11-17T11:59:59Z' })), NOW)).toBe('unknown')
    expect(ownerSessionEvidence(session('unknown', null), NOW)).toBe('unknown')
    expect(ownerSessionEvidence(session('unknown'), NOW)).toBe('unknown')
  })

  it('keeps the strict states as they were and never turns a status into "bound"', () => {
    for (const state of ['bound', 'offline', 'ambiguous'] as const) {
      expect(ownerSessionEvidence(session(state, status()), NOW)).toBe(state)
      expect(ownerSessionEvidence(session(state, status({ session_state: 'offline' })), NOW)).toBe(state)
    }
    for (const over of [{}, { activity_state: 'working' as const }, { session_state: 'offline' as const }]) {
      expect(ownerSessionEvidence(session('unknown', status(over)), NOW)).not.toMatch(/^bound/)
    }
  })
})

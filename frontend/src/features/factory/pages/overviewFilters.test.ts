import { describe, expect, it } from 'vitest'
import type { AgentTeamPreset } from '@/types/agentTeams'
import { teamVisible } from './overviewFilters'

function team(id: number, providers: string[]): AgentTeamPreset {
  return {
    id,
    name: `Team ${id}`,
    slots: providers.map((provider, index) => ({ id: id * 10 + index, provider })),
  } as unknown as AgentTeamPreset
}

describe('teamVisible', () => {
  it('shows every team when no filter is set', () => {
    expect(teamVisible(team(1, ['codex']), { provider: null, scopeTeamIds: null })).toBe(true)
  })

  it('keeps a team with a roster slot on the selected provider', () => {
    const mixed = team(1, ['claude-code', 'codex'])
    expect(teamVisible(mixed, { provider: 'codex', scopeTeamIds: null })).toBe(true)
  })

  it('hides a team with no roster slot on the selected provider', () => {
    expect(teamVisible(team(2, ['claude-code']), { provider: 'codex', scopeTeamIds: null })).toBe(false)
  })

  it('keeps only the team that owns the selected repository', () => {
    const owner = new Set([3])
    expect(teamVisible(team(3, []), { provider: null, scopeTeamIds: owner })).toBe(true)
    expect(teamVisible(team(4, []), { provider: null, scopeTeamIds: owner })).toBe(false)
  })

  it('requires both the provider and the repository team', () => {
    const owner = new Set([1])
    const filters = { provider: 'codex', scopeTeamIds: owner }
    expect(teamVisible(team(1, ['codex']), filters)).toBe(true)
    expect(teamVisible(team(1, ['claude-code']), filters)).toBe(false)
  })
})

import { describe, expect, it } from 'vitest'
import type { AgentTeamPreset, AgentTeamSummary } from '@/types/agentTeams'
import { filterNeedsYouRows, needsYouRows, type NeedsYouRow } from './needsYou'

function row(overrides: Partial<NeedsYouRow>): NeedsYouRow {
  return {
    key: 'k',
    title: 'Human review',
    detail: 'on #937',
    team: 'Team',
    href: '/work/1',
    cta: 'Open #937',
    scopeId: 8,
    workItemId: 1,
    ...overrides,
  }
}

describe('filterNeedsYouRows repository filter', () => {
  it('keeps only rows from the selected repository, so #937 of another repository is hidden', () => {
    const rows = [
      row({ key: 'mine', scopeId: 8, detail: 'on #612' }),
      row({ key: 'other', scopeId: 9, detail: 'on #937' }),
    ]
    const kept = filterNeedsYouRows(rows, { scopeId: 8, provider: null })
    expect(kept.map((item) => item.key)).toEqual(['mine'])
  })

  it('keeps team-wide rows with no repository and labels them', () => {
    const rows = [row({ key: 'hold', scopeId: null, workItemId: null, detail: 'supervisor is quiet' })]
    const kept = filterNeedsYouRows(rows, { scopeId: 8, provider: null })
    expect(kept).toHaveLength(1)
    expect(kept[0].detail).toBe('supervisor is quiet · team-wide')
  })

  it('keeps every row when no repository is selected', () => {
    const rows = [row({ key: 'a', scopeId: 8 }), row({ key: 'b', scopeId: 9 })]
    expect(filterNeedsYouRows(rows, { scopeId: null, provider: null })).toEqual(rows)
  })
})

describe('filterNeedsYouRows provider filter', () => {
  it('keeps a row whose work item the provider list shows, without a label', () => {
    const rows = [row({ key: 'listed', workItemId: 4, detail: 'on #612' })]
    const kept = filterNeedsYouRows(rows, {
      scopeId: null,
      provider: { workItemIds: new Set([4]), complete: true },
    })
    expect(kept).toEqual(rows)
  })

  it('drops a row whose work item is absent when the provider list is complete', () => {
    const rows = [row({ key: 'absent', workItemId: 9 })]
    const kept = filterNeedsYouRows(rows, {
      scopeId: null,
      provider: { workItemIds: new Set([4]), complete: true },
    })
    expect(kept).toEqual([])
  })

  it('keeps an absent work item labelled unconfirmed while the provider list is partial', () => {
    const rows = [row({ key: 'partial', workItemId: 9, detail: 'on #612' })]
    const kept = filterNeedsYouRows(rows, {
      scopeId: null,
      provider: { workItemIds: new Set([4]), complete: false },
    })
    expect(kept).toHaveLength(1)
    expect(kept[0].detail).toBe('on #612 · provider not confirmed')
  })

  it('keeps a row with no work item, labelled unconfirmed, because it cannot be matched', () => {
    const rows = [row({ key: 'unlinked', workItemId: null, detail: 'supervisor is quiet' })]
    const kept = filterNeedsYouRows(rows, {
      scopeId: null,
      provider: { workItemIds: new Set([4]), complete: true },
    })
    expect(kept).toHaveLength(1)
    expect(kept[0].detail).toBe('supervisor is quiet · provider not confirmed')
  })
})

describe('needsYouRows', () => {
  it('carries the repository and work item of a human action onto its row', () => {
    const preset = { id: 3, name: 'Builders', slots: [] } as unknown as AgentTeamPreset
    const summary = {
      human_actions: [
        {
          kind: 'human_review',
          state: 'requested',
          scope_id: 8,
          issue_number: 937,
          work_item_id: 12,
          pull_request_number: null,
        },
      ],
      attention: [],
      waiting_input: [],
      reminder_attention: [],
      required_ci_unreadable: [],
    } as unknown as AgentTeamSummary
    const [built] = needsYouRows([preset], [summary])
    expect(built.scopeId).toBe(8)
    expect(built.workItemId).toBe(12)
  })
})

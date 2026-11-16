import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { WorkPublicationPanel } from '../src/features/agent-teams/WorkPublicationPanel'
import { apiClient } from '../src/lib/api'
import type { GithubWorkItem } from '../src/types/agentTeams'

vi.mock('../src/lib/api', () => ({ apiClient: vi.fn() }))
const GENERATION = 'a'.repeat(64)
const item = { id: 1, dispatch_nonce: null, dispatch_generation: GENERATION, owner_slot_id: 2, repo_owner: 'owner',
  repo_name: 'repo', dispatch_head_ref: 'task', dispatch_status: 'dispatched', issue_number: 10 } as GithubWorkItem
function progress(changes = {}) {
  const now = Date.now()
  return { work_item_id: 1, dispatch_nonce: null, dispatch_generation: GENERATION, owner_slot_id: 2,
    checked_at: new Date(now).toISOString(), valid_until: new Date(now + 15000).toISOString(),
    phase: 'implementation', next_actor: 'owner', next_action: 'Continue approved work.',
    next_poll_expected_at: null, last_check_head: null,
    publication: { state: 'current', observed_at: new Date(now).toISOString(), local_sha: 'a'.repeat(40),
      published_sha: 'b'.repeat(40), unpublished_commits: 2, tracked_changes: null, untracked_files: null,
      relation: 'ahead', reason: null, file_counts_reason: 'safe_metadata_read',
      publication_first_observed_at: new Date(now).toISOString(),
      publication_time_source: 'github_head_observation', destination_url: 'https://github.com/owner/repo/tree/task' },
    ...changes }
}
beforeEach(() => { vi.mocked(apiClient).mockReset() })

describe('Work publication matching by the public dispatch generation', () => {
  it('matches a progress response that carries no nonce when the generation is the same', async () => {
    vi.mocked(apiClient).mockResolvedValue(progress())
    render(<WorkPublicationPanel item={item} />)
    expect(await screen.findByText('2 unpublished commits')).toBeTruthy()
  })
  it('rejects a response for another dispatch generation', async () => {
    vi.mocked(apiClient).mockResolvedValue(progress({ dispatch_generation: 'b'.repeat(64) }))
    render(<WorkPublicationPanel item={item} />)
    expect(await screen.findByText(/Unable to refresh progress/)).toBeTruthy()
    expect(screen.queryByText('2 unpublished commits')).toBeNull()
  })
  it('never uses the nonce as the identity of a dispatch', async () => {
    // The nonce differs but the public generation is the same: the response is for this dispatch.
    vi.mocked(apiClient).mockResolvedValue(progress({ dispatch_nonce: 'something-else' }))
    render(<WorkPublicationPanel item={{ ...item, dispatch_nonce: 'unrelated' } as GithubWorkItem} />)
    expect(await screen.findByText('2 unpublished commits')).toBeTruthy()
  })
  it('shows no progress and sends no request for an item without a generation, even with a nonce', async () => {
    const bare = { ...item, dispatch_generation: null, dispatch_nonce: 'dispatch' } as GithubWorkItem
    vi.mocked(apiClient).mockResolvedValue(progress({ dispatch_generation: null, dispatch_nonce: 'dispatch' }))
    render(<WorkPublicationPanel item={bare} />)
    expect(await screen.findByText('No progress data is available.')).toBeTruthy()
    expect(screen.queryByText('2 unpublished commits')).toBeNull()
    expect(vi.mocked(apiClient)).not.toHaveBeenCalled()
  })
  it.each([
    ['a response with no generation for an item that has one', GENERATION, null],
    ['a response with a generation for an item that has none', null, GENERATION],
    ['a response with no generation for an item that has none', null, null],
  ])('does not accept %s as current', async (_name, itemGeneration, responseGeneration) => {
    vi.mocked(apiClient).mockResolvedValue(progress({ dispatch_generation: responseGeneration }))
    render(<WorkPublicationPanel item={{ ...item, dispatch_generation: itemGeneration } as GithubWorkItem} />)
    expect((await screen.findAllByText(/No progress data is available|Unable to refresh progress/)).length).toBeGreaterThan(0)
    expect(screen.queryByText('2 unpublished commits')).toBeNull()
    expect(screen.queryByText('Continue approved work.')).toBeNull()
  })
})

import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { OperationsFindings } from '../src/features/factory/components/OperationsFindings'
import { resetFactoryReads } from '../src/features/factory/reads'
import { fixtureFetch, jsonResponse, renderRoute, settle } from './helpers/factory'

// #941: the Overview shows the factory's own stuck states, and nothing while all is well.
beforeEach(() => resetFactoryReads())
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const finding = (over: Record<string, unknown>) => ({
  id: 1, kind: 'poll_rolled_back', subject: 'repo:o/r', detail: 'A poll of o/r failed (RuntimeError) and its uncommitted writes were discarded.',
  status: 'open', occurrences: 3, first_seen_at: '2026-11-15T10:00:00Z', last_seen_at: '2026-11-15T10:03:00Z', ...over,
})

async function open(body: unknown, status = 200) {
  fixtureFetch((path) => (path === 'health/findings' ? jsonResponse(body, status) : jsonResponse({})))
  renderRoute(<OperationsFindings />, '/', '/')
  await settle()
  await settle()
}

describe('Operations findings', () => {
  it('lists each open finding in words, with its detail, count and reason code', async () => {
    await open({
      health: 'degraded', reasons: ['admission_missing', 'poll_rolled_back'], open_findings: 2,
      findings: [
        finding({}),
        finding({ id: 2, kind: 'admission_missing', subject: 'issue:7', detail: 'Issue #7 carries the dispatch label but has had no work item for 3 polls in a row.', occurrences: 1 }),
      ],
    })
    const card = screen.getByRole('region', { name: /Needs a look: operations/ })
    expect(within(card).getByText('2')).toBeInTheDocument()
    expect(within(card).getByText('A poll failed and its writes were discarded')).toBeInTheDocument()
    expect(within(card).getByText('A ready issue has no work item')).toBeInTheDocument()
    expect(within(card).getByText(/Issue #7 carries the dispatch label/)).toBeInTheDocument()
    expect(within(card).getByText('poll_rolled_back')).toBeInTheDocument()
    expect(within(card).getByText(/seen 3 times/)).toBeInTheDocument()
    expect(within(card).getByText(/seen 1 time ·/)).toBeInTheDocument()
  })

  it('shows nothing while no finding is open', async () => {
    await open({ health: 'ok', reasons: [], open_findings: 0, findings: [finding({ status: 'resolved' })] })
    expect(screen.queryByRole('region', { name: /Needs a look: operations/ })).not.toBeInTheDocument()
  })

  it('shows nothing when the findings cannot be read', async () => {
    await open({ detail: 'down' }, 500)
    expect(screen.queryByRole('region', { name: /Needs a look: operations/ })).not.toBeInTheDocument()
  })
})

import { cleanup, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { WorkDetailPage } from '../src/features/factory/pages/WorkDetailPage'
import { resetFactoryReads } from '../src/features/factory/reads'
import { fixtureFetch, jsonResponse, renderRoute, settle } from './helpers/factory'
import detail from './fixtures/factory/v1/work-item.json'

// #1073: the Work page's owner session shows the agent-status evidence, not a bare "unknown", when the strict
// state is "unknown"; "bound" stays strict, so the Bridge link and the launch hints are exactly as before.

const status = (over: object = {}) => ({
  session_state: 'online', activity_state: 'unknown', activity_reason: 'provider_unsupported', wake_state: 'unknown',
  wake_reason: null, idle_since: null, checked_at: new Date(Date.now() - 60_000).toISOString(),
  valid_until: new Date(Date.now() + 3_600_000).toISOString(), ...over,
})

function withSession(source: { response: unknown }, session: object) {
  const body = structuredClone(source.response) as { work_item: { session: object } }
  Object.assign(body.work_item.session, session)
  return body
}

async function open(body: unknown) {
  fixtureFetch(() => jsonResponse(body))
  renderRoute(<WorkDetailPage />, '/work/2', '/work/:workItemId')
  await settle()
}

const details = () => screen.getByText('Technical details').closest('details') as HTMLElement

beforeEach(() => { resetFactoryReads() })
afterEach(() => { cleanup(); resetFactoryReads(); vi.unstubAllGlobals() })

describe('the owner session on the Work page', () => {
  it('shows an online agent that has no verified pane as that, not as "unknown"', async () => {
    await open(withSession(detail.pending, { agent_status: status() }))

    expect(within(details()).getByText(/owner session online, not bound to a verified pane/)).toBeInTheDocument()
    expect(within(details()).queryByText(/owner session unknown/)).toBeNull()
  })

  it('adds what the agent is doing when the status knows it', async () => {
    await open(withSession(detail.pending, { agent_status: status({ activity_state: 'idle' }) }))

    expect(within(details()).getByText(/owner session online, idle, not bound to a verified pane/)).toBeInTheDocument()
  })

  it('says offline when the status does', async () => {
    await open(withSession(detail.pending, { agent_status: status({ session_state: 'offline' }) }))

    expect(within(details()).getByText(/owner session offline, no verified pane/)).toBeInTheDocument()
  })

  it('stays "unknown" with no status, an unknown status or an expired one', async () => {
    for (const agent_status of [null, status({ session_state: 'unknown' }), status({ valid_until: new Date(Date.now() - 1000).toISOString() })]) {
      await open(withSession(detail.pending, { agent_status }))
      expect(within(details()).getByText(/owner session unknown · runtime seen unknown/)).toBeInTheDocument()
      cleanup(); resetFactoryReads()
    }
  })

  it('never offers the Bridge session link for an online but unbound owner', async () => {
    await open(withSession(detail.pending, { agent_status: status() }))

    expect(screen.queryByRole('link', { name: 'Read-only verified session' })).toBeNull()
  })

  it('keeps bound as it was: its words, its Bridge link, whatever the status says', async () => {
    await open(withSession(detail.completed, { agent_status: status({ session_state: 'offline' }) }))

    expect(within(details()).getByText(/owner session bound · runtime seen codex-cli/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Read-only verified session' })).toBeInTheDocument()
  })

  it('keeps a verified offline owner as offline, with its launch review', async () => {
    await open(withSession(detail.verified_offline_owner, { agent_status: status() }))

    expect(within(details()).getByText(/owner session offline/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Review launch for/ })).toBeInTheDocument()
  })
})

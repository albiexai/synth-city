import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { HARNESS_NAMES, MAIL_ACTION_HARNESSES, connectedHarnessNames, hasMailAction, mailConfirm, mailSetupState, mailTechnicalLines } from '@/features/agent-mail/mailSetup'
import { ProviderLaunchOptionsFields } from '@/features/providers/ProviderLaunchOptionsFields'
import type { AgentMailInstallStatus } from '@/types/agentMail'
import type { ProviderLaunchOptionsResponse } from '@/types/providers'

const MCP_FILE = '/home/u/.gemini/config/mcp_config.json'
const HOOKS_FILE = '/home/u/.gemini/config/hooks.json'

function status(overrides: Partial<AgentMailInstallStatus>): AgentMailInstallStatus {
  return {
    claude_code_hooks: [], claude_code_hooks_missing: ['SessionStart'], claude_code_mcp_installed: false,
    codex_hooks: [], codex_hooks_missing: ['SessionStart'], codex_mcp_installed: false,
    copilot_hooks: [], copilot_hooks_missing: ['sessionStart'], copilot_mcp_installed: false,
    opencode_plugin_events: [], opencode_plugin_events_missing: ['session.created'], opencode_mcp_installed: false,
    pi_mail_ready: false,
    antigravity_cli_available: true, antigravity_mcp_installed: false, antigravity_hooks: [],
    antigravity_hooks_missing: ['SessionStart', 'PreInvocation', 'PreToolUse'],
    antigravity_mcp_config_path: MCP_FILE, antigravity_hooks_path: HOOKS_FILE,
    ...overrides,
  } as AgentMailInstallStatus
}

const CONNECTED = { antigravity_mcp_installed: true, antigravity_hooks: ['SessionStart', 'PreInvocation', 'PreToolUse'], antigravity_hooks_missing: [] }

describe('Antigravity CLI harness', () => {
  it('has a managed Agent Mail setup with its own name', () => {
    expect(HARNESS_NAMES['antigravity-cli']).toBe('Antigravity CLI')
    expect(hasMailAction('antigravity-cli')).toBe(true)
    expect(MAIL_ACTION_HARNESSES).toContain('antigravity-cli')
  })

  it('reads its setup state from the Antigravity install fields', () => {
    expect(mailSetupState('antigravity-cli', status({}))).toBe('not_set_up')
    expect(mailSetupState('antigravity-cli', status({ antigravity_mcp_installed: true }))).toBe('partial')
    expect(mailSetupState('antigravity-cli', status({ antigravity_hooks: ['SessionStart'], antigravity_hooks_missing: ['PreInvocation', 'PreToolUse'] }))).toBe('partial')
    expect(mailSetupState('antigravity-cli', status(CONNECTED))).toBe('connected')
    expect(connectedHarnessNames(status(CONNECTED))).toContain('Antigravity CLI')
  })

  it('treats a status without the Antigravity fields as not set up', () => {
    const older = status({})
    for (const key of ['antigravity_mcp_installed', 'antigravity_hooks', 'antigravity_hooks_missing'] as const) delete older[key]
    expect(mailSetupState('antigravity-cli', older)).toBe('not_set_up')
  })

  it('names both real files in its setup and removal confirmations', () => {
    const value = status({})
    const setup = mailConfirm('antigravity-cli', 'setup', value)
    expect(setup.title).toBe('Install Agent Mail for Antigravity CLI')
    expect(setup.description).toContain('before Albiex Synth City')
    expect(setup.mutations.join('\n')).toContain(MCP_FILE)
    expect(setup.mutations.join('\n')).toContain(HOOKS_FILE)
    const remove = mailConfirm('antigravity-cli', 'remove', value)
    expect(remove.title).toBe('Remove Agent Mail from Antigravity CLI')
    expect(remove.mutations.join('\n')).toContain(MCP_FILE)
    expect(remove.mutations.join('\n')).toContain(HOOKS_FILE)
  })

  it('names the agy-safe MCP server key in its confirmations (#991)', () => {
    const setup = mailConfirm('antigravity-cli', 'setup', status({})).mutations.join('\n')
    expect(setup).toContain('MCP server synth-city-mail')
    expect(setup).not.toContain('MCP server albiex-synth-city-mail')
    const remove = mailConfirm('antigravity-cli', 'remove', status({})).mutations.join('\n')
    expect(remove).toContain('MCP server synth-city-mail')
    // An earlier Synth City entry under the old key is migrated or removed too.
    expect(remove).toContain('albiex-synth-city-mail')
  })

  it('falls back to the documented file locations when the status has none', () => {
    const value = status({ antigravity_mcp_config_path: null, antigravity_hooks_path: null })
    const text = mailConfirm('antigravity-cli', 'setup', value).mutations.join('\n')
    expect(text).toContain('~/.gemini/config/mcp_config.json')
    expect(text).toContain('~/.gemini/config/hooks.json')
  })

  it('says that team launch readiness stays blocked after setup', () => {
    const setup = mailConfirm('antigravity-cli', 'setup', status({}))
    expect(setup.description).toMatch(/readiness stays blocked/i)
    const lines = mailTechnicalLines('antigravity-cli', status(CONNECTED))
    const readiness = lines.find((line) => line.label === 'Team launch readiness')
    expect(readiness?.value).toMatch(/blocked/)
    expect(readiness?.value).toMatch(/onboarding/)
    expect(lines).toContainEqual({ label: 'MCP file', value: MCP_FILE })
    expect(lines).toContainEqual({ label: 'Hooks file', value: HOOKS_FILE })
    expect(lines).toContainEqual({ label: 'Hooks', value: 'installed' })
    expect(lines).toContainEqual({ label: 'Antigravity CLI', value: 'available' })
  })

  it('counts partly installed hooks in its technical details', () => {
    const lines = mailTechnicalLines('antigravity-cli', status({ antigravity_hooks: ['SessionStart'], antigravity_hooks_missing: ['PreInvocation', 'PreToolUse'] }))
    expect(lines).toContainEqual({ label: 'Hooks', value: '1/3 installed' })
    expect(lines).toContainEqual({ label: 'MCP', value: 'not installed' })
  })

  it('offers a model field and reasoning effort, but no platform', () => {
    const descriptor = {
      provider: 'antigravity-cli', bedrock_supported: false, reasoning_effort_supported: true,
      reasoning_effort_options: [{ value: 'high', label: 'high' }],
      model_options: [],
    } as unknown as ProviderLaunchOptionsResponse
    render(<ProviderLaunchOptionsFields provider="antigravity-cli" value={{}} onChange={() => {}} descriptor={descriptor} />)
    expect(screen.getByLabelText('Model')).toBeInTheDocument()
    expect(screen.getByText('Reasoning effort')).toBeInTheDocument()
    expect(screen.queryByText('Platform')).toBeNull()
    expect(screen.getByText(/Antigravity CLI/)).toBeInTheDocument()
  })
})

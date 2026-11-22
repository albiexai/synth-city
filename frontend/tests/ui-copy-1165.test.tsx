import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { surfaceName } from '../src/features/harnesses/surfaceCatalog'

const source = (path: string) => readFileSync(resolve(__dirname, '../src', path), 'utf8')

describe('issue 1165 copy', () => {
  it('says Director key where the bundle said operator token', () => {
    for (const path of ['features/cc-bridge/KillSessionDialog.tsx', 'features/cc-bridge/ImageAttachmentDialog.tsx']) {
      expect(source(path), path).not.toMatch(/operator token/i)
      expect(source(path), path).toContain('Director key')
    }
  })

  it('reports the Leader selection with the Director wording', () => {
    const page = source('features/agent-teams/AgentTeamsPage.tsx')
    expect(page).not.toContain('Operator selected the Leader')
    expect(page).toContain('Director selected the Leader in the team roster.')
  })

  it('uses one Assigned harness label in the factory filters', () => {
    const filters = source('features/factory/filters.tsx')
    expect(filters).not.toContain('Team roster harness')
    expect(filters).toContain('Assigned harness')
  })

  it('names Copilot as GitHub Copilot CLI in the bridge provider list and the last-session label', () => {
    expect(source('features/cc-bridge/CCBridgePage.tsx')).toContain("{ value: 'copilot-cli', label: 'GitHub Copilot CLI' }")
    expect(source('features/cc-bridge/NewSessionDialog.tsx')).toContain("isCopilot ? 'GitHub Copilot CLI' : 'Codex'} session")
  })

  it('says session history is unavailable for Codex CLI instead of the generic adapter message', () => {
    expect(source('features/native-settings/NativeRoute.tsx')).toContain("Session history isn't available for Codex CLI yet")
  })

  it('drops CLAUDE.md from the Codex memory name and keeps it for Claude', () => {
    expect(surfaceName('codex-cli', 'memory')).toBe('Memory')
    expect(surfaceName('claude-code', 'memory')).toBe('Memory (CLAUDE.md)')
  })
})

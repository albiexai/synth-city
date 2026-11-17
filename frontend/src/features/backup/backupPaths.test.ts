import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/lib/api'

const sources = import.meta.glob<string>('./*.tsx', { query: '?raw', import: 'default', eager: true })

function prefixedLines(name: string): string[] {
  return sources[`./${name}`].split('\n').filter((line) => line.includes('/api/v1/'))
}

describe('backup request paths', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('joins a bare backup endpoint onto one /api/v1/ prefix', async () => {
    const fetchMock = vi.fn<(url: string) => Promise<Response>>(async () =>
      new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )
    vi.stubGlobal('fetch', fetchMock)
    await apiClient('backup/7/plan?project_path=%2Fsrv')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/backup/7/plan?project_path=%2Fsrv')
  })

  it('leaves no apiClient call in the backup screens that adds the prefix a second time', () => {
    expect(prefixedLines('BackupList.tsx')).toEqual([])
    expect(prefixedLines('RestoreWizard.tsx')).toEqual([])
    expect(prefixedLines('BackupPage.tsx').every((line) => line.includes('href'))).toBe(true)
  })
})

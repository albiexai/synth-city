import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { ThemeProvider } from './contexts/ThemeContext'

describe('unknown routes', () => {
  beforeEach(() => {
    vi.stubGlobal('APP_VERSION', '0.0.0-test')
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      writable: true,
      value: vi.fn((query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    window.history.replaceState({}, '', '/')
  })

  it('keeps the app shell and shows a not-found message for /settings', async () => {
    window.history.replaceState({}, '', '/settings')
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response('{"detail":"unavailable"}', { status: 503, headers: { 'Content-Type': 'application/json' } }),
    ))
    render(
      <ThemeProvider>
        <App />
      </ThemeProvider>,
    )
    await screen.findByRole('heading', { name: 'Page not found' })
    expect(screen.getByRole('link', { name: 'Back to Overview' }).getAttribute('href')).toBe('/')
  })
})

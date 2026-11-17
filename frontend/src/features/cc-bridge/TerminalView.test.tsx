import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { TerminalView } from './TerminalView'

const detach = vi.fn()
const attach = vi.fn(() => Promise.resolve())

vi.mock('./useTerminal', () => ({
  useTerminal: () => ({
    connected: true,
    readOnly: true,
    setReadOnly: vi.fn(),
    attach,
    detach,
    focusTerminal: vi.fn(),
  }),
}))

describe('TerminalView Detach', () => {
  beforeEach(() => {
    detach.mockClear()
    attach.mockClear()
  })

  it('closes the local socket and reports the pane for grid removal', () => {
    const onDetach = vi.fn()
    render(<TerminalView target="deck:0.0" onDetach={onDetach} />)

    fireEvent.click(screen.getByRole('button', { name: 'Detach' }))

    expect(detach).toHaveBeenCalledTimes(1)
    expect(onDetach).toHaveBeenCalledTimes(1)
  })

  it('still detaches when no grid removal handler is provided', () => {
    render(<TerminalView target="deck:0.0" />)

    fireEvent.click(screen.getByRole('button', { name: 'Detach' }))

    expect(detach).toHaveBeenCalledTimes(1)
  })
})

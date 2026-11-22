import { describe, expect, it } from 'vitest'
import { activityReasonText } from './agentStatus'

describe('activityReasonText', () => {
  it('explains an unreadable native log in the visible status', () => {
    expect(activityReasonText('native_log_unreadable')).toBe(
      "The controller cannot read this member's native activity log. See the install notes for separate controller and agent users.",
    )
  })

  it('names an unconfirmed native wait without claiming a question or permission', () => {
    expect(activityReasonText('native_wait_unconfirmed')).toBe(
      'The member is waiting on something other than a question. Its activity cannot be confirmed.',
    )
  })

  it('keeps the generic text for an unknown reason', () => {
    expect(activityReasonText('no_such_reason')).toBe('Activity could not be confirmed.')
  })
})

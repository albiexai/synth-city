import { describe, expect, it } from 'vitest'
import { activityReasonText } from './agentStatus'

describe('activityReasonText', () => {
  it('explains an unreadable native log in the visible status', () => {
    expect(activityReasonText('native_log_unreadable')).toBe(
      "The controller cannot read this member's native activity log. See the install notes for separate controller and agent users.",
    )
  })

  it('keeps the generic text for an unknown reason', () => {
    expect(activityReasonText('no_such_reason')).toBe('Activity could not be confirmed.')
  })
})

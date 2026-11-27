import { useEffect, useState } from 'react'
import { apiClient } from '@/lib/api'
import type { HarnessScopeResponse } from '@/types/harnessScope'

export function SharedOperatorUserWarning() {
  const [sharesUser, setSharesUser] = useState(false)

  useEffect(() => {
    let cancelled = false
    apiClient<HarnessScopeResponse>('harness-scope')
      .then(({ users, default_user }) => {
        if (cancelled) return
        const [controller] = users
        setSharesUser(users.length === 1 && controller.is_controller && controller.name === default_user)
      })
      .catch(() => { if (!cancelled) setSharesUser(false) })
    return () => { cancelled = true }
  }, [])

  if (!sharesUser) return null
  return (
    <p role="status" className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-amber-950 dark:text-amber-100">
      Agents running as the controller can read the operator token. This installation uses a shared OS user.
    </p>
  )
}

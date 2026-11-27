import { useProviderContext } from '@/contexts/ProviderContext'

export function SharedOperatorUserWarning() {
  const context = useProviderContext(false)
  const installation = context?.installationSearch
  if (context?.loading) return null
  if (context?.error || !installation?.controller_user || installation.agent_user === undefined) {
    return <p role="status" className="text-sm text-muted-foreground">Synth City could not verify whether the controller and agents share an OS user.</p>
  }
  if (installation.agent_user !== null && installation.agent_user !== installation.controller_user) return null
  return (
    <p role="status" className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-amber-950 dark:text-amber-100">
      Agents running as the controller can read the Director key. This installation uses a shared OS user.
    </p>
  )
}

import { scopeUnreadable, type HarnessReadScope } from '@/types/harnessScope'

interface ReadScopeNoticeProps {
  scope: HarnessReadScope | null
  denial: string | null
  fallbackFolder: string
}

export function ReadScopeNotice({ scope, denial, fallbackFolder }: ReadScopeNoticeProps) {
  if (denial) return <p role="alert" className="break-words text-sm text-destructive">{denial}</p>
  if (!scope) return null

  const unreadable = scopeUnreadable(scope)
  return (
    <p
      role={unreadable ? 'alert' : undefined}
      className={unreadable ? 'text-sm text-destructive' : 'text-sm text-muted-foreground'}
    >
      {unreadable ? 'Cannot read' : 'Reading'} {scope.user}'s{' '}
      {scope.data_dir ? <code className="break-all">{scope.data_dir}</code> : fallbackFolder}
      {unreadable && '.'}
    </p>
  )
}

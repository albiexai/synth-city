import { useState, useEffect } from 'react'
import { useSessionsApi } from '@/hooks/useSessionsApi'
import { scopeReadDenial } from '@/types/harnessScope'
import { SessionCard } from './SessionCard'
import type { SessionSummary } from '@/types/sessions'

interface Props {
  projectFolder: string | null
}

export function SessionList({ projectFolder }: Props) {
  const { listSessions } = useSessionsApi()
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [readError, setReadError] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    async function loadSessions() {
      setLoading(true)
      try {
        const data = await listSessions({
          project_folder: projectFolder || undefined,
          limit: 50,
          sort_by: 'date',
          sort_order: 'desc',
        })
        if (!current) return
        setSessions(data.sessions)
        setReadError(null)
      } catch (err) {
        if (current) setReadError(scopeReadDenial(err) ?? 'Could not read sessions.')
      } finally {
        if (current) setLoading(false)
      }
    }
    void loadSessions()
    return () => { current = false }
  }, [listSessions, projectFolder])

  if (loading) {
    return <div className="text-center py-8">Loading sessions...</div>
  }

  if (readError) {
    return <p className="py-8 text-center text-sm text-destructive" role="alert">{readError}</p>
  }

  if (sessions.length === 0) {
    return (
      <div className="text-center py-8 text-muted-foreground">
        No sessions found
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {sessions.map(session => (
        <SessionCard
          key={`${session.project_folder}/${session.id}`}
          session={session}
        />
      ))}
    </div>
  )
}

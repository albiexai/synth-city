import { useEffect, useState, useCallback, useRef } from 'react'
import { Gauge, HelpCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { RefreshButton } from '@/components/shared/RefreshButton'
import { ReadScopeNotice } from '@/components/shared/ReadScopeNotice'
import { scopeReadDenial, scopeUnreadable, type HarnessReadScope } from '@/types/harnessScope'
import { useContextApi } from '@/hooks/useContextApi'
import { ActiveSessionsList } from './ActiveSessionsList'
import { ContextGauge } from './ContextGauge'
import { ContextTimelineChart } from './ContextTimelineChart'
import { ContextCompositionChart } from './ContextCompositionChart'
import { FileConsumptionTable } from './FileConsumptionTable'
import { ToolUsageTable } from './ToolUsageTable'
import { CacheEfficiencyCard } from './CacheEfficiencyCard'
import { ProjectionsCard } from './ProjectionsCard'
import type { ActiveSessionContext, ContextAnalysis } from '@/types/context'

export function ContextPage() {
  const { getActiveSessions, getSessionContext } = useContextApi()
  const [sessions, setSessions] = useState<ActiveSessionContext[]>([])
  const [selectedSession, setSelectedSession] = useState<ActiveSessionContext | null>(null)
  const [analysis, setAnalysis] = useState<ContextAnalysis | null>(null)
  const [loading, setLoading] = useState(true)
  const [analysisLoading, setAnalysisLoading] = useState(false)
  const [showHelp, setShowHelp] = useState(false)
  const [scope, setScope] = useState<HarnessReadScope | null>(null)
  const [readDenial, setReadDenial] = useState<string | null>(null)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const requests = useRef({ sessions: 0, analysis: 0 })
  const unreadable = readDenial !== null || scopeUnreadable(scope)

  const clearAnalysis = useCallback(() => {
    requests.current.analysis++
    setSelectedSession(null)
    setAnalysis(null)
    setAnalysisLoading(false)
  }, [])

  const fetchSessions = useCallback(async () => {
    const request = ++requests.current.sessions
    try {
      const data = await getActiveSessions()
      if (request !== requests.current.sessions) return false
      const source = data.scope ?? null
      const blocked = scopeUnreadable(source)
      setScope(source)
      setReadDenial(null)
      setSessions(blocked ? [] : data.sessions)
      if (blocked) clearAnalysis()
      return !blocked
    } catch (err) {
      const denial = scopeReadDenial(err)
      if (request === requests.current.sessions && denial) {
        setReadDenial(denial)
        setSessions([])
        clearAnalysis()
      }
      return false
    } finally {
      if (request === requests.current.sessions) setLoading(false)
    }
  }, [clearAnalysis, getActiveSessions])

  const fetchAnalysis = useCallback(async (session: ActiveSessionContext) => {
    const request = ++requests.current.analysis
    setAnalysisLoading(true)
    try {
      const data = await getSessionContext(session.project_folder, session.session_id)
      if (request === requests.current.analysis) setAnalysis(data.analysis)
    } catch {
      if (request === requests.current.analysis) setAnalysis(null)
    } finally {
      if (request === requests.current.analysis) setAnalysisLoading(false)
    }
  }, [getSessionContext])

  const handleSelect = useCallback((session: ActiveSessionContext) => {
    setSelectedSession(session)
    fetchAnalysis(session)
  }, [fetchAnalysis])

  const handleRefresh = useCallback(async () => {
    setLoading(true)
    const readable = await fetchSessions()
    if (readable && selectedSession) {
      await fetchAnalysis(selectedSession)
    }
  }, [fetchSessions, fetchAnalysis, selectedSession])

  // Initial fetch + auto-poll
  useEffect(() => {
    const pending = requests.current
    fetchSessions()

    const startPolling = () => {
      intervalRef.current = setInterval(fetchSessions, 10_000)
    }

    const handleVisibility = () => {
      if (document.hidden) {
        if (intervalRef.current) clearInterval(intervalRef.current)
      } else {
        fetchSessions()
        startPolling()
      }
    }

    startPolling()
    document.addEventListener('visibilitychange', handleVisibility)

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
      document.removeEventListener('visibilitychange', handleVisibility)
      pending.sessions++
      pending.analysis++
    }
  }, [fetchSessions])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold flex items-center gap-2">
            <Gauge className="h-8 w-8" />
            Context Window
          </h1>
          <p className="text-muted-foreground">
            Analyze context window usage across active sessions
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowHelp(!showHelp)}
            className={showHelp ? 'text-primary' : ''}
          >
            <HelpCircle className="h-4 w-4 mr-1.5" />
            Help
          </Button>
          <RefreshButton onClick={handleRefresh} loading={loading} />
        </div>
      </div>

      <ReadScopeNotice scope={scope} denial={readDenial} fallbackFolder="projects folder" />

      {/* Active Sessions */}
      {!unreadable && !loading && (
        <ActiveSessionsList
          sessions={sessions}
          selectedId={selectedSession?.session_id}
          onSelect={handleSelect}
        />
      )}

      {/* Analysis */}
      {!unreadable && analysisLoading && (
        <div className="py-8 text-center">
          <p className="text-muted-foreground">Analyzing session...</p>
        </div>
      )}

      {!unreadable && analysis && !analysisLoading && (
        <div className="space-y-4">
          {/* Top row: gauge + projections + cache */}
          <div className="grid gap-4 md:grid-cols-3">
            <div className="flex items-center justify-center">
              <ContextGauge
                percentage={analysis.context_percentage}
                currentTokens={analysis.current_context_tokens}
                maxTokens={analysis.max_context_tokens}
                model={analysis.model}
                showHelp={showHelp}
              />
            </div>
            <ProjectionsCard
              avgTokensPerTurn={analysis.avg_tokens_per_turn}
              estimatedTurnsRemaining={analysis.estimated_turns_remaining}
              contextZone={analysis.context_zone}
              totalTurns={analysis.total_turns}
              showHelp={showHelp}
            />
            <CacheEfficiencyCard cache={analysis.cache_efficiency} showHelp={showHelp} />
          </div>

          {/* Charts */}
          <ContextTimelineChart
            snapshots={analysis.snapshots}
            maxTokens={analysis.max_context_tokens}
            showHelp={showHelp}
          />

          <div className="grid gap-4 md:grid-cols-2">
            <ContextCompositionChart composition={analysis.composition} showHelp={showHelp} />
            <FileConsumptionTable files={analysis.file_consumptions} showHelp={showHelp} />
          </div>

          <ToolUsageTable tools={analysis.tool_consumptions} showHelp={showHelp} />
        </div>
      )}
    </div>
  )
}

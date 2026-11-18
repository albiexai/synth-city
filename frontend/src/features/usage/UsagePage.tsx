import { useState, useEffect, useCallback, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { BarChart3 } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { RefreshButton } from '@/components/shared/RefreshButton'
import { ReadScopeNotice } from '@/components/shared/ReadScopeNotice'
import { scopeReadDenial, scopeUnreadable, type HarnessReadScope } from '@/types/harnessScope'
import { useUsageApi } from '@/hooks/useUsageApi'
import { useProjectContext } from '@/contexts/ProjectContext'
import { UsageSummaryCards } from './UsageSummaryCards'
import { DailyUsageChart } from './DailyUsageChart'
import { CostChart } from './CostChart'
import { SessionUsageTable } from './SessionUsageTable'
import { BlocksView } from './BlocksView'
import { MonthlyUsageChart } from './MonthlyUsageChart'
import { ExportCard } from './ExportCard'
import { getFromCache, saveToCache, isCacheStale, invalidateCache } from '@/lib/usageCache'
import { scopeUserHeaders } from '@/lib/api'
import type {
  UsageSummary,
  DailyUsage,
  SessionUsage,
  MonthlyUsage,
  SessionBlock,
} from '@/types/usage'

interface UsageCacheData {
  daily: DailyUsage[]
  sessions: SessionUsage[]
  monthly: MonthlyUsage[]
  blocks: SessionBlock[]
  activeBlock?: SessionBlock
  totalSessionCost: number
  totalBlockCost: number
}

const EMPTY_USAGE: UsageCacheData = {
  daily: [], sessions: [], monthly: [], blocks: [], totalSessionCost: 0, totalBlockCost: 0,
}

function usageCacheKey(scope: HarnessReadScope | null, project: string | null): string | null {
  return scope?.data_dir ? JSON.stringify([scope.user, scope.data_dir, project]) : null
}

export function UsagePage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const { projects, activeProject } = useProjectContext()
  const { getSummary, getDaily, getSessions, getMonthly, getBlocks } = useUsageApi()

  const [selectedProject, setSelectedProject] = useState<string | null>(
    activeProject?.path || searchParams.get('project') || null
  )
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [scope, setScope] = useState<HarnessReadScope | null>(null)
  const [readDenial, setReadDenial] = useState<string | null>(null)
  const requests = useRef({ latest: 0 })
  const unreadable = readDenial !== null || scopeUnreadable(scope)

  // Data states
  const [summary, setSummary] = useState<UsageSummary | null>(null)
  const [usage, setUsage] = useState(EMPTY_USAGE)
  const { daily: dailyData, sessions: sessionData, monthly: monthlyData, blocks, activeBlock, totalSessionCost, totalBlockCost } = usage

  const loadData = useCallback(async (forceRefresh = false) => {
    const request = ++requests.current.latest
    const requestedUser = scopeUserHeaders()['X-Harness-Scope-User']
    const isCurrent = () => request === requests.current.latest
      && requestedUser === scopeUserHeaders()['X-Harness-Scope-User']
    setError(null)
    setScope(null)
    setLoading(true)
    setSummary(null)
    setUsage(EMPTY_USAGE)

    try {
      const params = selectedProject ? { project_path: selectedProject } : undefined
      const summaryRes = await getSummary(params)
      if (!isCurrent()) return
      const source = summaryRes.scope ?? null
      setScope(source)
      setReadDenial(null)
      if (scopeUnreadable(source)) {
        setSummary(null)
        setUsage(EMPTY_USAGE)
        return
      }
      setSummary(summaryRes.summary)

      // The default user can change between visits. Confirm the source before reading cached charts.
      const cacheKey = usageCacheKey(source, selectedProject)
      if (cacheKey && !forceRefresh) {
        const cached = getFromCache<UsageCacheData>(cacheKey)
        if (cached) {
          setUsage(cached)
          setLoading(false)
          if (!isCacheStale(cacheKey)) return
        }
      }

      const [dailyRes, sessionRes, monthlyRes, blockRes] = await Promise.all([
        getDaily(params),
        getSessions({ ...params, limit: 100 }),
        getMonthly(params),
        getBlocks({ ...params, recent: true }),
      ])
      if (!isCurrent()) return
      const updated = {
        daily: dailyRes.data,
        sessions: sessionRes.data,
        monthly: monthlyRes.data,
        blocks: blockRes.data,
        activeBlock: blockRes.active_block,
        totalSessionCost: sessionRes.total_cost,
        totalBlockCost: blockRes.total_cost,
      }
      setUsage(updated)
      if (cacheKey) saveToCache(cacheKey, updated)
    } catch (err) {
      if (isCurrent()) {
        setUsage(EMPTY_USAGE)
        const denial = scopeReadDenial(err)
        if (denial) {
          setReadDenial(denial)
          setSummary(null)
        } else {
          setError(err instanceof Error ? err.message : 'Failed to load usage data')
        }
      }
    } finally {
      if (isCurrent()) setLoading(false)
    }
  }, [selectedProject, getSummary, getDaily, getSessions, getMonthly, getBlocks])

  const handleRefresh = useCallback(() => {
    const key = usageCacheKey(scope, selectedProject)
    if (key) invalidateCache(key)
    loadData(true)
  }, [loadData, scope, selectedProject])

  useEffect(() => {
    const pending = requests.current
    loadData()
    return () => { pending.latest++ }
  }, [loadData])

  const handleProjectChange = (value: string) => {
    const path = value === 'all' ? null : value
    if (path !== selectedProject) {
      setLoading(true)
      setSummary(null)
      setUsage(EMPTY_USAGE)
    }
    setSelectedProject(path)
    setSearchParams(current => {
      const next = new URLSearchParams(current)
      if (path) next.set('project', path)
      else next.delete('project')
      return next
    })
  }

  const header = (
    <div className="flex items-center justify-between">
      <div>
        <h1 className="text-3xl font-bold flex items-center gap-2">
          <BarChart3 className="h-8 w-8" />
          Usage Tracking
        </h1>
        <p className="text-muted-foreground">
          Monitor Claude Code token usage and costs
        </p>
      </div>
      <RefreshButton onClick={handleRefresh} loading={loading} />
    </div>
  )
  const sourceNotice = <ReadScopeNotice scope={scope} denial={readDenial} fallbackFolder="projects folder" />
  if (unreadable) {
    return <div className="space-y-6">{header}{sourceNotice}</div>
  }

  return (
    <div className="space-y-6">
      {header}
      {sourceNotice}

      {/* Error Display */}
      {error && (
        <Card className="border-destructive">
          <CardHeader>
            <CardTitle className="text-destructive">Error</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm">{error}</p>
          </CardContent>
        </Card>
      )}

      {/* Project Filter */}
      <Card>
        <CardHeader className="pb-3">
          <CardTitle>Filter</CardTitle>
          <CardDescription>Filter usage data by project</CardDescription>
        </CardHeader>
        <CardContent>
          <Select value={selectedProject || 'all'} onValueChange={handleProjectChange}>
            <SelectTrigger className="w-[300px]">
              <SelectValue placeholder="All projects" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All Projects</SelectItem>
              {projects.map(p => (
                <SelectItem key={p.path} value={p.path}>
                  {p.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </CardContent>
      </Card>

      {/* Export */}
      <ExportCard projectPath={selectedProject} />

      {/* Summary Cards */}
      <UsageSummaryCards summary={summary} loading={loading} />

      {/* Tabbed Views */}
      {!error && (
        <Tabs defaultValue="overview" className="space-y-4">
          <TabsList>
            <TabsTrigger value="overview">Overview</TabsTrigger>
            <TabsTrigger value="daily">Daily</TabsTrigger>
            <TabsTrigger value="sessions">Sessions</TabsTrigger>
            <TabsTrigger value="monthly">Monthly</TabsTrigger>
            <TabsTrigger value="blocks">
              Blocks
              {activeBlock && (
                <span className="ml-1 w-2 h-2 bg-success rounded-full animate-pulse" />
              )}
            </TabsTrigger>
          </TabsList>

          {/* Overview Tab */}
          <TabsContent value="overview" className="space-y-4">
            <div className="grid gap-4 lg:grid-cols-2">
              <CostChart data={dailyData} loading={loading} />
              <MonthlyUsageChart data={monthlyData} loading={loading} />
            </div>
            <DailyUsageChart data={dailyData.slice(0, 14)} loading={loading} />
          </TabsContent>

          {/* Daily Tab */}
          <TabsContent value="daily" className="space-y-4">
            <DailyUsageChart data={dailyData} loading={loading} />
            <CostChart data={dailyData} loading={loading} />
          </TabsContent>

          {/* Sessions Tab */}
          <TabsContent value="sessions">
            <SessionUsageTable
              data={sessionData}
              loading={loading}
              totalCost={totalSessionCost}
            />
          </TabsContent>

          {/* Monthly Tab */}
          <TabsContent value="monthly">
            <MonthlyUsageChart data={monthlyData} loading={loading} />
          </TabsContent>

          {/* Blocks Tab */}
          <TabsContent value="blocks">
            <BlocksView
              blocks={blocks}
              activeBlock={activeBlock}
              loading={loading}
              totalCost={totalBlockCost}
            />
          </TabsContent>
        </Tabs>
      )}
    </div>
  )
}

import { Link, useSearchParams } from 'react-router-dom'
import { formatServerTime } from '@/lib/serverTime'
import type { CampaignListResponse, CampaignMeasure, CampaignRow } from '@/types/factory'
import { RelativeTime } from './components/RelativeTime'
import { ReadStatus } from './components/ReadStatus'
import { anchor } from './components/styles'
import { useFactoryFilters, type FilterOption } from './filterOptions'
import { useObservation } from './reads'

const STATUSES = [['all', 'Live and finished'], ['live', 'Live'], ['finished', 'Finished']] as const
const GROUPS = [['team', 'Team'], ['repository', 'Repository'], ['none', 'No grouping']] as const
const select = 'h-9 max-w-64 rounded-md border bg-background px-2'

// What the server's reason codes mean to a person. Unknown is never shown as zero.
const REASONS: Record<string, string> = {
  not_read_yet: 'not read yet',
  no_snapshot: 'no snapshot recorded',
  before_recording: 'ran before recording began',
  snapshot_without_repository_counts: 'the snapshot has no repository counts',
  incomplete_read: 'could not be read',
}
const reasonText = (reason: string | null) => (reason ? REASONS[reason] ?? reason.replaceAll('_', ' ') : 'not recorded')

function Unknown({ reason }: { reason: string | null }) {
  return <span className="text-muted-foreground"><em>unknown</em> · {reasonText(reason)}</span>
}

function Choice({ label, value, options, onChange }: {
  label: string
  value: string
  options: readonly (readonly [string, string])[]
  onChange: (value: string) => void
}) {
  return (
    <label className="grid gap-1 text-sm">
      {label}
      <select className={select} value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map(([id, text]) => <option key={id} value={id}>{text}</option>)}
      </select>
    </label>
  )
}

const pairs = (options: FilterOption[]) => options.map((option) => [option.value, option.label] as const)

/** The campaign's selector as a name and a kind, for example ``label:release:v4.4``. */
function nameOf(selector: string) {
  const at = selector.indexOf(':')
  return at < 0 ? { kind: 'selector', name: selector } : { kind: selector.slice(0, at), name: selector.slice(at + 1) }
}

function Merged({ merged, scope }: { merged: CampaignMeasure; scope: CampaignMeasure }) {
  if (merged.value === null || scope.value === null) return <Unknown reason={merged.value === null ? merged.reason : scope.reason} />
  return <>{merged.value} / {scope.value}</>
}

function Repositories({ row }: { row: CampaignRow }) {
  return (
    <>
      {row.repositories.length === 0 ? <Unknown reason={row.repositories_unknown_reason} /> : (
        <ul>
          {row.repositories.map((item) => (
            <li key={item.repo} className="break-words">
              {item.repo} (
              {item.issues === null ? <Unknown reason={item.reason} /> : item.issues}
              )
            </li>
          ))}
        </ul>
      )}
      {row.source && (
        <p className="text-xs text-muted-foreground">
          {row.source.kind === 'frozen'
            ? `Counts frozen ${formatServerTime(row.source.as_of, { dateStyle: 'medium' })}`
            : <>Counts read <RelativeTime value={row.source.as_of} /></>}
        </p>
      )}
    </>
  )
}

const CELL = 'px-3 py-2 align-top max-md:block max-md:before:block max-md:before:text-xs max-md:before:text-muted-foreground max-md:before:content-[attr(data-l)] md:table-cell'

function CampaignLine({ row }: { row: CampaignRow }) {
  const { kind, name } = nameOf(row.selector)
  const live = row.status !== 'finished'
  return (
    <tr className="border-t max-md:block max-md:py-2">
      <th scope="row" className={`${CELL} text-left font-normal`} data-l="Campaign">
        <Link className={`${anchor} font-medium`} to={`/insights/retrospectives/${row.id}`}>{name}</Link>
        <span className="block text-xs text-muted-foreground">
          {kind} · {live ? 'started' : 'finished'} {formatServerTime(live ? row.started_at : row.finished_at ?? row.started_at, { dateStyle: 'medium' })}
        </span>
      </th>
      <td className={CELL} data-l="Status">
        <span className={`rounded border px-2 text-xs font-medium ${live ? 'border-primary/60 text-primary' : 'border-border text-muted-foreground'}`}>
          {row.status === 'active' ? 'Live' : row.status === 'finishing' ? 'Finishing' : 'Finished'}
        </span>
      </td>
      <td className={`${CELL} md:text-right`} data-l="Merged / in scope"><Merged merged={row.merged} scope={row.in_scope} /></td>
      <td className={CELL} data-l="Repositories (issues)"><Repositories row={row} /></td>
    </tr>
  )
}

/** Rows under their group headings. A campaign in several repositories appears under each when grouped by repository. */
function groups(rows: CampaignRow[], by: string): [string, CampaignRow[]][] {
  if (by === 'none') return [['', rows]]
  const found = new Map<string, CampaignRow[]>()
  const add = (key: string, row: CampaignRow) => found.set(key, [...(found.get(key) ?? []), row])
  for (const row of rows) {
    if (by === 'team') add(row.team ? `Team ${row.team.name}` : 'Retired team', row)
    else if (row.repositories.length === 0) add('Repositories unknown', row)
    else for (const item of row.repositories) add(item.repo, row)
  }
  return [...found.entries()].sort(([a], [b]) => (by === 'repository' ? a.localeCompare(b) : 0))
}

function Summary({ data, filtered }: { data: CampaignListResponse; filtered: boolean }) {
  const parts = [
    `${data.total} ${data.total === 1 ? 'campaign' : 'campaigns'}`,
    `${data.counts.live} live`,
    `${data.counts.finished} finished${data.finished_before_recording > 0 ? ` (${data.finished_before_recording} before recording began in 4.4)` : ''}`,
  ]
  return (
    <div role="status" className="space-y-1 text-sm text-muted-foreground">
      <p>{parts.join(' · ')}</p>
      {data.hidden_unknown_repositories > 0 && (
        <p>
          {data.hidden_unknown_repositories} {data.hidden_unknown_repositories === 1 ? 'campaign has' : 'campaigns have'} unknown
          repositories, so the repository filter does not show {data.hidden_unknown_repositories === 1 ? 'it' : 'them'}.
        </p>
      )}
      {data.has_more && <p>Showing the first {data.campaigns.length} campaigns.</p>}
      {!filtered && data.total > 0 && (
        <p><em>unknown</em> means the factory did not record it. It never means zero.</p>
      )}
    </div>
  )
}

/** The campaigns of every team (#1252, design section 8). It reads recorded data only. */
export function RetrospectivesList() {
  const [raw, setRaw] = useSearchParams()
  const { params, teams, repositories, ready } = useFactoryFilters()
  const status = STATUSES.some(([id]) => id === raw.get('status')) ? raw.get('status')! : 'all'
  const group = GROUPS.some(([id]) => id === raw.get('group')) ? raw.get('group')! : 'team'
  const query = new URLSearchParams({ limit: '100', status })
  for (const key of ['team_id', 'scope_id']) if (params.get(key)) query.set(key, params.get(key)!)
  const read = useObservation<CampaignListResponse>(ready ? `factory/campaigns?${query}` : null)
  const filtered = Boolean(params.get('team_id') || params.get('scope_id')) || status !== 'all'

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(raw)
    for (const [key, value] of Object.entries(changes)) {
      if (value && !(key === 'status' && value === 'all') && !(key === 'group' && value === 'team')) next.set(key, value)
      else next.delete(key)
    }
    setRaw(next)
  }
  const data = read.data
  return (
    <div className="space-y-4">
      <form className="flex flex-wrap items-end gap-3 rounded-lg border bg-card p-3" aria-label="Retrospective filters" onSubmit={(event) => event.preventDefault()}>
        <Choice label="Repository" value={params.get('scope_id') ?? ''} onChange={(value) => update({ scope_id: value, team_id: '' })}
          options={[['', `All repositories (${repositories.length})`], ...pairs(repositories)]} />
        <Choice label="Team" value={params.get('team_id') ?? ''} onChange={(value) => update({ team_id: value, scope_id: '' })}
          options={[['', `All teams (${teams.length})`], ...pairs(teams)]} />
        <Choice label="Status" value={status} onChange={(value) => update({ status: value })} options={STATUSES} />
        <Choice label="Group by" value={group} onChange={(value) => update({ group: value })} options={GROUPS} />
      </form>
      <ReadStatus state={read} />
      {data && <Summary data={data} filtered={filtered} />}
      {data && data.campaigns.length === 0 && (
        <p className="rounded-lg border p-4 text-sm text-muted-foreground">
          {filtered ? 'No campaign matches these filters.' : 'No campaigns yet. A team starts one for its backlog label or milestone.'}
        </p>
      )}
      {data && data.campaigns.length > 0 && (
        <div className="rounded-lg border">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Campaigns and their repositories</caption>
            <thead className="bg-muted max-md:sr-only">
              <tr>
                <th scope="col" className="px-3 py-2">Campaign</th>
                <th scope="col" className="px-3 py-2">Status</th>
                <th scope="col" className="px-3 py-2 md:text-right">Merged / in scope</th>
                <th scope="col" className="px-3 py-2">Repositories (issues)</th>
              </tr>
            </thead>
            {groups(data.campaigns, group).map(([heading, rows]) => (
              <tbody key={heading || 'all'}>
                {heading && (
                  <tr className="border-t bg-muted/50"><th colSpan={4} scope="colgroup" className="px-3 py-2 text-left font-semibold">{heading}</th></tr>
                )}
                {rows.map((row) => <CampaignLine key={row.id} row={row} />)}
              </tbody>
            ))}
          </table>
        </div>
      )}
    </div>
  )
}

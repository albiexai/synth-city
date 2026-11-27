import { Link, useSearchParams } from 'react-router-dom'
import { formatServerTime } from '@/lib/serverTime'
import type { CampaignListResponse, CampaignMeasure, CampaignRepositoryCount, CampaignRow } from '@/types/factory'
import { RelativeTime } from './components/RelativeTime'
import { ReadStatus } from './components/ReadStatus'
import { anchor } from './components/styles'
import { useFactoryFilters, type FilterOption } from './filterOptions'
import { useObservations } from './reads'
import { useState } from 'react'

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
  repository_added_after_snapshot: 'added after the last count',
  membership_read_after_cutoff: 'read after the cutoff',
  departure_after_cutoff: 'a departure after the cutoff',
  delivery_after_cutoff: 'a delivery after the cutoff',
  delivery_history_missing: 'delivery history missing',
  delivery_identity_unknown: 'delivery identity unknown',
}
const reasonText = (reason: string | null) => (reason ? REASONS[reason] ?? reason.replaceAll('_', ' ') : 'not recorded')

/** A repository once, whatever the spelling of its owner and name (GitHub names are not case-sensitive). */
const once = (items: CampaignRepositoryCount[]) => [...new Map(items.map((item) => [item.repo.toLowerCase(), item])).values()]

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

/** A number that the snapshot knows only in part says so: its value stays, with the count of unknown outcomes and why. */
function Partial({ measure }: { measure: CampaignMeasure }) {
  const unknown = measure.unknown_count ?? 0
  if (measure.coverage !== 'partial' && unknown === 0) return null
  return (
    <span className="block text-xs text-muted-foreground">
      partial{unknown > 0 ? ` · ${unknown} unknown` : ''}{measure.reasons?.length ? ` · ${measure.reasons.map(reasonText).join(', ')}` : ''}
    </span>
  )
}

function Merged({ merged, scope }: { merged: CampaignMeasure; scope: CampaignMeasure }) {
  if (merged.value === null || scope.value === null) return <Unknown reason={merged.value === null ? merged.reason : scope.reason} />
  return <>{merged.value} / {scope.value}<Partial measure={merged} /><Partial measure={scope} /></>
}

function Repositories({ row }: { row: CampaignRow }) {
  return (
    <>
      {row.repositories.length === 0 ? <Unknown reason={row.repositories_unknown_reason} /> : (
        <ul>
          {once(row.repositories).map((item) => (
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
            ? `Counts as of ${formatServerTime(row.source.as_of, { dateStyle: 'medium' })}, frozen ${formatServerTime(row.source.frozen_at ?? row.source.as_of, { dateStyle: 'medium' })}`
            : <>Counts as of <RelativeTime value={row.source.as_of} /></>}
        </p>
      )}
    </>
  )
}

const CELL = 'px-3 py-2 align-top max-md:block max-md:before:block max-md:before:text-xs max-md:before:text-muted-foreground max-md:before:content-[attr(data-l)] md:table-cell'

function CampaignLine({ row, scopeId }: { row: CampaignRow; scopeId: string | null }) {
  const { kind, name } = nameOf(row.selector)
  const live = row.status !== 'finished'
  return (
    <tr className="border-t max-md:block max-md:py-2">
      <th scope="row" className={`${CELL} text-left font-normal`} data-l="Campaign">
        <Link className={`${anchor} font-medium`} to={`/insights/retrospectives/${row.id}${scopeId ? `?scope_id=${scopeId}` : ''}`}>{name}</Link>
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

/** Rows under their group headings. A team is its id and a repository is its lower-case name, so two teams with one
 * name stay apart and two spellings of one repository join. A campaign in several repositories appears under each. */
function groups(rows: CampaignRow[], by: string): [string, string, CampaignRow[]][] {
  if (by === 'none') return [['all', '', rows]]
  const found = new Map<string, [string, CampaignRow[]]>()
  const add = (key: string, label: string, row: CampaignRow) => found.set(key, [found.get(key)?.[0] ?? label, [...(found.get(key)?.[1] ?? []), row]])
  for (const row of rows) {
    if (by === 'team') add(row.team ? `team:${row.team.id}` : 'retired', row.team ? `Team ${row.team.name}` : 'Retired team', row)
    else if (row.repositories.length === 0) add('unknown', 'Repositories unknown', row)
    else for (const item of once(row.repositories)) add(item.repo.toLowerCase(), item.repo, row)
  }
  return [...found.entries()].map(([key, [label, list]]) => [key, label, list] as [string, string, CampaignRow[]])
    .sort((x, y) => (by === 'repository' ? x[1].localeCompare(y[1]) : 0))
}

function Summary({ data, shown, filtered }: { data: CampaignListResponse; shown: number; filtered: boolean }) {
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
      {shown < data.total && <p>Showing {shown} of {data.total} campaigns.</p>}
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
  // Later pages follow the cursor of the page before. A new filter starts again from the first page.
  const [more, setMore] = useState<{ query: string; cursors: string[] }>({ query: '', cursors: [] })
  const cursors = more.query === String(query) ? more.cursors : []
  const reads = useObservations<CampaignListResponse>(ready ? [String(query), ...cursors.map((cursor) => `${query}&cursor=${cursor}`)].map((q) => `factory/campaigns?${q}`) : [])
  const first = reads.states[0]
  const last = reads.states.at(-1)
  const data = first?.data ?? null
  const seen = new Set<number>()
  const campaigns = reads.states.flatMap((state) => state.data?.campaigns ?? []).filter((row) => !seen.has(row.id) && seen.add(row.id))
  const next = last?.data?.has_more ? last.data.next_cursor : null
  const filtered = Boolean(params.get('team_id') || params.get('scope_id')) || status !== 'all'

  const update = (changes: Record<string, string>) => {
    const next = new URLSearchParams(raw)
    for (const [key, value] of Object.entries(changes)) {
      if (value && !(key === 'status' && value === 'all') && !(key === 'group' && value === 'team')) next.set(key, value)
      else next.delete(key)
    }
    setRaw(next)
  }
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
      {reads.states.map((state, index) => <ReadStatus key={index} state={state} />)}
      {data && <Summary data={data} shown={campaigns.length} filtered={filtered} />}
      {data && campaigns.length === 0 && (
        <p className="rounded-lg border p-4 text-sm text-muted-foreground">
          {filtered ? 'No campaign matches these filters.' : 'No campaigns yet. A team starts one for its backlog label or milestone.'}
        </p>
      )}
      {data && campaigns.length > 0 && (
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
            {groups(campaigns, group).map(([key, heading, rows]) => (
              <tbody key={key}>
                {heading && (
                  <tr className="border-t bg-muted/50"><th colSpan={4} scope="colgroup" className="px-3 py-2 text-left font-semibold">{heading}</th></tr>
                )}
                {rows.map((row) => <CampaignLine key={row.id} row={row} scopeId={params.get('scope_id')} />)}
              </tbody>
            ))}
          </table>
        </div>
      )}
      {next && (
        <button type="button" className="h-9 rounded-md border px-3 text-sm hover:bg-accent" disabled={last?.refreshing && !last.data}
          onClick={() => setMore({ query: String(query), cursors: [...cursors, next] })}>
          Show more campaigns
        </button>
      )}
    </div>
  )
}

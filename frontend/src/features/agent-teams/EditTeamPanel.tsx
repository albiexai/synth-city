import { useState, type ReactNode } from 'react'
import { Copy, Save, Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { formatServerTime } from '@/lib/serverTime'
import type { TeamWorkSettings, TeamWorkSettingsResponse } from '@/types/agentTeams'

/**
 * The only place on the team page with editable team fields. It opens in the
 * page, under the header, when the person selects Edit team. Each action is
 * the existing handler.
 */
export function EditTeamPanel({
  open,
  onOpenChange,
  name,
  onNameChange,
  description,
  onDescriptionChange,
  saving,
  onSave,
  onDuplicate,
  onDelete,
  workSettings,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  name: string
  onNameChange: (value: string) => void
  description: string
  onDescriptionChange: (value: string) => void
  saving: boolean
  onSave: () => Promise<boolean>
  onDuplicate: () => Promise<boolean>
  onDelete: () => Promise<boolean>
  workSettings?: ReactNode
}) {
  const [busy, setBusy] = useState(false)
  if (!open) return null
  // A finished action closes the panel. A failed one keeps it open with the person's text.
  const run = async (action: () => Promise<boolean>) => {
    setBusy(true)
    try {
      if (await action()) onOpenChange(false)
    } finally {
      setBusy(false)
    }
  }
  return (
    <section
      aria-label="Edit team"
      className="space-y-4 rounded-lg border bg-card p-4"
      onKeyDown={(event) => { if (event.key === 'Escape') onOpenChange(false) }}
    >
      <h3 className="text-lg font-semibold">Edit team</h3>
      <div className="grid gap-4">
        <div className="grid gap-2">
          <Label htmlFor="preset-name">Name</Label>
          <Input id="preset-name" autoFocus value={name} onChange={(event) => onNameChange(event.target.value)} />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="preset-description">Description</Label>
          <Textarea
            id="preset-description"
            value={description}
            onChange={(event) => onDescriptionChange(event.target.value)}
          />
        </div>
      </div>
      {workSettings}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex gap-2">
          <Button variant="outline" disabled={busy} onClick={() => void run(onDuplicate)}>
            <Copy className="mr-2 h-4 w-4" />
            Duplicate
          </Button>
          <Button
            variant="outline"
            disabled={busy}
            className="border-destructive/60 text-destructive hover:bg-destructive/10 hover:text-destructive"
            onClick={() => void run(onDelete)}
          >
            <Trash2 className="mr-2 h-4 w-4" />
            Delete
          </Button>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={busy || saving || !name.trim()} onClick={() => void run(onSave)}>
            <Save className="mr-2 h-4 w-4" />
            Save
          </Button>
        </div>
      </div>
    </section>
  )
}

const timers = [
  ['mail_request_unanswered_seconds', 'Unanswered Mail timeout', 60],
  ['github_team_idle_timeout_seconds', 'Team idle timeout', 30],
] as const
const summarySetting = 'github_publish_summaries_enabled'
const labels: Record<string, string> = {
  ...Object.fromEntries(timers.map(([field, label]) => [field, label])),
  [summarySetting]: 'Publish GitHub summaries',
}

function settingText(value: unknown) {
  if (typeof value === 'boolean') return value ? 'On' : 'Off'
  return typeof value === 'number' ? `${value} s` : 'Installation default'
}

export function TeamWorkSettingsEditor({ load, save }: {
  load: (beforeId?: number) => Promise<TeamWorkSettingsResponse>
  save: (input: Partial<TeamWorkSettings>) => Promise<TeamWorkSettingsResponse>
}) {
  const [data, setData] = useState<TeamWorkSettingsResponse>()
  const [draft, setDraft] = useState<Partial<TeamWorkSettings>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const run = async (action: () => Promise<TeamWorkSettingsResponse>, append = false) => {
    setBusy(true)
    setError('')
    try {
      const next = await action()
      setData(append && data ? { ...data, history: [...data.history, ...next.history], next_before_id: next.next_before_id } : next)
      if (!append) setDraft({})
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Could not load or save work settings.')
    } finally {
      setBusy(false)
    }
  }
  const effective = (key: keyof TeamWorkSettings) => data && (
    <span className="text-xs text-muted-foreground">
      In effect {settingText(data.overrides[key] ?? data.defaults[key])} · {data.overrides[key] == null ? 'Installation default' : 'Team override'}
    </span>
  )
  const summaryValue = draft[summarySetting] === undefined ? data?.overrides[summarySetting] : draft[summarySetting]
  const summaryLabel = labels[summarySetting]
  return (
    <section aria-label="Team work settings" className="space-y-3 border-t pt-4">
      <h4 className="text-sm font-semibold">Work settings</h4>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      {!data ? (
        <Button type="button" variant="outline" disabled={busy} onClick={() => void run(load)}>Load work settings</Button>
      ) : (
        <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); void run(() => save(draft)) }}>
          <fieldset disabled={busy} className="grid gap-3 sm:grid-cols-2">
            {timers.map(([key, label, minimum]) => (
              <div key={key} className="grid gap-1">
                <Label htmlFor={key}>{label} (seconds)</Label>
                <Input id={key} type="number" min={minimum} max={86400}
                  placeholder={String(data.defaults[key])}
                  value={(draft[key] === undefined ? data.overrides[key] : draft[key]) ?? ''}
                  onChange={(event) => setDraft({ ...draft, [key]: event.target.value === '' ? null : event.target.valueAsNumber })} />
                {effective(key)}
              </div>
            ))}
            <div className="grid gap-1">
              <Label htmlFor={summarySetting}>{summaryLabel}</Label>
              <select id={summarySetting} className="h-9 rounded-md border bg-background px-2 text-sm"
                value={summaryValue == null ? '' : String(summaryValue)}
                onChange={(event) => setDraft({ ...draft, [summarySetting]: event.target.value === '' ? null : event.target.value === 'true' })}>
                <option value="">Installation default</option>
                <option value="true">On</option>
                <option value="false">Off</option>
              </select>
              {effective(summarySetting)}
            </div>
          </fieldset>
          <p className="text-xs text-muted-foreground">Clear a timeout to use the installation default.</p>
          <Button type="submit" disabled={busy || !Object.keys(draft).length}>Save work settings</Button>
          <details className="space-y-2 text-sm">
            <summary className="cursor-pointer font-medium">Work settings history</summary>
            {data.history.length ? (
              <ul className="space-y-2">
                {data.history.map((entry) => (
                  <li key={entry.event_id} className="break-words">
                    {formatServerTime(entry.occurred_at)} · {entry.actor_reference ?? entry.actor_kind}
                    {Object.entries(entry.changes).map(([name, change]) => (
                      <div key={name}>{labels[name]} · {settingText(change.before)} → {settingText(change.after)}</div>
                    ))}
                  </li>
                ))}
              </ul>
            ) : <p>No work settings changes yet.</p>}
            {data.next_before_id != null && (
              <Button type="button" variant="outline" disabled={busy}
                onClick={() => void run(() => load(data.next_before_id ?? undefined), true)}>Older changes</Button>
            )}
          </details>
        </form>
      )}
    </section>
  )
}

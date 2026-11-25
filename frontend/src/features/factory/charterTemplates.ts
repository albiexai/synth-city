import builder from './charter-templates/builder.md?raw'
import leader from './charter-templates/leader.md?raw'
import reviewer from './charter-templates/reviewer.md?raw'

// #1113: charter templates for the Leader, builder and reviewer. The resolver fills only the values it is given.
// The caller decides which values are verified. A value that is missing or blank stays as the literal {{name}}.

export const CHARTER_ROLES = ['leader', 'builder', 'reviewer'] as const
export type CharterRole = (typeof CHARTER_ROLES)[number]

export const CHARTER_PLACEHOLDERS = [
  'release_branch',
  'ready_label',
  'hold_label',
  'lane_label',
  'plan_size_limit',
  'supervisor_address',
] as const
export type CharterPlaceholder = (typeof CHARTER_PLACEHOLDERS)[number]
export type CharterTemplateValues = Partial<Record<CharterPlaceholder, string>>

export const CHARTER_TEMPLATES: Record<CharterRole, string> = { leader, builder, reviewer }

/** The documented plan-size limit in active minutes, until #1114a adds a configured limit. */
export const DEFAULT_PLAN_SIZE_LIMIT = 60

const PLACEHOLDER_PATTERN = /\{\{([a-z_]+)\}\}/g

/** Each {{name}} left in the text, once, in the order of first use. */
export function findUnresolved(text: string): string[] {
  const names: string[] = []
  for (const match of text.matchAll(PLACEHOLDER_PATTERN)) {
    if (!names.includes(match[1])) names.push(match[1])
  }
  return names
}

export function resolveCharterTemplate(
  role: CharterRole,
  values: CharterTemplateValues,
): { text: string; unresolved: string[] } {
  const text = CHARTER_TEMPLATES[role].replace(PLACEHOLDER_PATTERN, (whole, name: string) => {
    const value = (values as Record<string, string | undefined>)[name]
    return value && value.trim() ? value.trim() : whole
  })
  return { text, unresolved: findUnresolved(text) }
}

/** Adds the template below the existing text. The existing text stays byte for byte as the prefix. */
export function appendTemplate(existing: string, template: string): string {
  if (existing === '') return template
  const gap = existing.endsWith('\n\n') ? '' : existing.endsWith('\n') ? '\n' : '\n\n'
  return `${existing}${gap}${template}`
}

/** The documented default, clamped to the scope's max_item_effort_minutes when that is a lower positive integer. */
export function planSizeLimit(maxItemEffortMinutes?: number | null): number {
  const cap = maxItemEffortMinutes
  if (typeof cap === 'number' && Number.isInteger(cap) && cap > 0 && cap < DEFAULT_PLAN_SIZE_LIMIT) return cap
  return DEFAULT_PLAN_SIZE_LIMIT
}

export interface BoundSupervisor {
  display_name?: string | null
  actor_key?: string | null
}

/** "Name (key)" for the bound supervisor. No supervisor, or a blank name or key, gives no value. */
export function supervisorAddress(actor?: BoundSupervisor | null): string | undefined {
  const name = actor?.display_name?.trim()
  const key = actor?.actor_key?.trim()
  return name && key ? `${name} (${key})` : undefined
}

export interface CharterScopeInput {
  baseRef?: string | null
  dispatchLabel?: string | null
  reviewHoldLabel?: string | null
  areaLabels?: string[] | null
  maxItemEffortMinutes?: number | null
  supervisor?: BoundSupervisor | null
}

function clean(value?: string | null): string | undefined {
  const text = value?.trim()
  return text || undefined
}

/** The release branch from a base ref. The symbolic origin/HEAD names no release branch, so it gives no value. */
function releaseBranch(baseRef?: string | null): string | undefined {
  const ref = clean(baseRef)?.replace(/^origin\//, '')
  return ref && ref !== 'HEAD' ? ref : undefined
}

/** Template values from a scope's own settings. Anything the scope does not state is left out. */
export function charterTemplateValues(input: CharterScopeInput): CharterTemplateValues {
  const values: CharterTemplateValues = { plan_size_limit: String(planSizeLimit(input.maxItemEffortMinutes)) }
  const entries: [CharterPlaceholder, string | undefined][] = [
    ['release_branch', releaseBranch(input.baseRef)],
    ['ready_label', clean(input.dispatchLabel)],
    ['hold_label', clean(input.reviewHoldLabel)],
    ['lane_label', clean((input.areaLabels ?? []).map((label) => label.trim()).filter(Boolean).join(', '))],
    ['supervisor_address', supervisorAddress(input.supervisor)],
  ]
  for (const [name, value] of entries) if (value) values[name] = value
  return values
}

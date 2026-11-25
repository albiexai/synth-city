import { NavLink, Navigate, Outlet, useLocation } from 'react-router-dom'
import { cn } from '@/lib/utils'

// #1251: Insights replaces the Audit entry. Its tabs are Retrospectives (the default) and Audit log (the old Audit page).
const TABS = [
  ['Retrospectives', '/insights/retrospectives'],
  ['Audit log', '/insights/audit'],
] as const

export function InsightsPage() {
  return (
    <section className="space-y-4">
      <h2 className="text-2xl font-semibold">Insights</h2>
      <nav aria-label="Insights sections" className="flex flex-wrap gap-1 border-b">
        {TABS.map(([name, to]) => (
          <NavLink
            key={to}
            to={to}
            className={({ isActive }) => cn(
              '-mb-px min-h-11 border-b-2 px-3 py-2 text-sm font-medium transition-colors focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-ring',
              isActive ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground',
            )}
          >
            {name}
          </NavLink>
        ))}
      </nav>
      <Outlet />
    </section>
  )
}

/** The list of campaigns is a later issue (#1252). Until then the tab says what it will hold. */
export function RetrospectivesTab() {
  return <p className="text-sm text-muted-foreground">Campaign retrospectives will be listed here.</p>
}

function useQueryAndHash() {
  const { search, hash } = useLocation()
  return `${search}${hash}`
}

/** /insights opens the default tab. The query and the hash stay. */
export function InsightsIndexRedirect() {
  return <Navigate replace to={`/insights/retrospectives${useQueryAndHash()}`} />
}

/** An old /audit link opens the Audit log tab. The query and the hash stay. */
export function AuditAlias() {
  return <Navigate replace to={`/insights/audit${useQueryAndHash()}`} />
}

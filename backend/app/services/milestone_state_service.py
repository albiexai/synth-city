"""Read-only view of milestone moves: deferred items and issues that wait for a team (#1337).

A wait is a row of `github_milestone_waits` with a title. A deferral is a cancelled work item whose
applied "deferred" end-stopped audit event names the target milestone. Nothing here writes, calls
GitHub or sends Mail.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import factory_schemas as wire
from app.models.database import (
    AgentTeamPreset, FactoryAuditEvent, GithubMilestoneWait, GithubWorkItem, TeamGithubScope,
)
from app.services.factory_projection_service import github_identity, utc_now
# One definition of "deferred by milestone": the watcher writes it, so this view reads the same facts.
from app.services.github_watcher_service import _milestone_deferral_facts

ROW_LIMIT = 100


def _selected_scopes(query, filters: wire.FactoryFilters):
    if filters.team_id is not None:
        query = query.where(TeamGithubScope.preset_id == filters.team_id)
    if filters.scope_id is not None:
        query = query.where(TeamGithubScope.id == filters.scope_id)
    return query


def _repository(scope: TeamGithubScope) -> wire.WorkRepository:
    identity = github_identity(scope)
    return wire.WorkRepository(scope_id=scope.id, github=identity,
                               display_name=f"{identity.owner}/{identity.name}")


async def _waiting(db: AsyncSession, filters: wire.FactoryFilters):
    rows = (await db.execute(_selected_scopes(
        select(GithubMilestoneWait, TeamGithubScope)
        .join(TeamGithubScope, TeamGithubScope.id == GithubMilestoneWait.scope_id)
        .where(GithubMilestoneWait.milestone_title.is_not(None)), filters,
    ).order_by(GithubMilestoneWait.github_updated_at.desc(), GithubMilestoneWait.scope_id,
               GithubMilestoneWait.issue_number))).all()
    # One row for each repository and issue, even when several scopes recorded the same wait.
    seen, waiting = set(), []
    for wait, scope in rows:
        key = (scope.repo_owner.lower(), scope.repo_name.lower(), wait.issue_number)
        if key in seen:
            continue
        seen.add(key)
        waiting.append(wire.WaitingIssue(repository=_repository(scope), issue_number=wait.issue_number,
                                         milestone=wait.milestone_title, observed_at=wait.github_updated_at))
    return waiting[:ROW_LIMIT], len(waiting) > ROW_LIMIT


async def _deferred(db: AsyncSession, filters: wire.FactoryFilters):
    later = aliased(GithubWorkItem)
    later_scope = aliased(TeamGithubScope)
    # The deferral shows only while it is the newest item of its issue in the repository.
    superseded = (select(later.id).join(later_scope, later_scope.id == later.scope_id).where(
        later.issue_number == GithubWorkItem.issue_number, later.id > GithubWorkItem.id,
        func.lower(later_scope.repo_owner) == func.lower(TeamGithubScope.repo_owner),
        func.lower(later_scope.repo_name) == func.lower(TeamGithubScope.repo_name)).exists())
    latest_event = (select(func.max(FactoryAuditEvent.id))
                    .where(FactoryAuditEvent.item_id == GithubWorkItem.id, *_milestone_deferral_facts())
                    .correlate(GithubWorkItem).scalar_subquery())
    rows = (await db.execute(_selected_scopes(
        select(GithubWorkItem, TeamGithubScope, AgentTeamPreset, FactoryAuditEvent)
        .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
        .join(AgentTeamPreset, AgentTeamPreset.id == TeamGithubScope.preset_id)
        .join(FactoryAuditEvent, FactoryAuditEvent.id == latest_event)
        .where(GithubWorkItem.dispatch_status == "cancelled", ~superseded), filters,
    ).order_by(FactoryAuditEvent.occurred_at.desc(), GithubWorkItem.id.desc()).limit(ROW_LIMIT + 1))).all()
    deferred = []
    for item, scope, team, event in rows[:ROW_LIMIT]:
        target = (event.after_values or {}).get("target_milestone")
        deferred.append(wire.DeferredIssue(
            repository=_repository(scope), team=wire.TeamReference(id=team.id, name=team.name),
            work_item_id=item.id, issue_number=item.issue_number, issue_title=item.issue_title,
            target_milestone=target if isinstance(target, str) and target else None,
            deferred_at=event.occurred_at, previous_branch=item.dispatch_head_ref))
    return deferred, len(rows) > ROW_LIMIT


async def milestone_states(db: AsyncSession, filters: wire.FactoryFilters) -> wire.MilestoneStatesResponse:
    waiting, waiting_truncated = await _waiting(db, filters)
    deferred, deferred_truncated = await _deferred(db, filters)
    return wire.MilestoneStatesResponse(
        generated_at=utc_now(), filters=filters, waiting=waiting, waiting_truncated=waiting_truncated,
        deferred=deferred, deferred_truncated=deferred_truncated)

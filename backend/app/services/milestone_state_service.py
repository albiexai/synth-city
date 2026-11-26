"""Read-only view of milestone moves: deferred items and issues that wait for a team (#1337).

A wait is a row of `github_milestone_waits` with a title. A deferral is a cancelled work item whose
applied "deferred" end-stopped audit event names the target milestone. Nothing here writes, calls
GitHub or sends Mail.

A row keeps the repository it was recorded for. A deferral takes it from the GitHub issue URL that its
item stored at admission. A wait has no stored repository, so the scope PATCH deletes the waits of a
scope when its repository changes. A row whose repository cannot be proven is not shown.
"""
from __future__ import annotations

import re

from sqlalchemy import String, cast, func, literal, select
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

# The same owner and name rules as the factory reads, applied to a stored GitHub issue URL.
_ISSUE_URL = re.compile(
    r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9_.-]{1,100})/issues/([0-9]+)"
)


def _selected_scopes(query, filters: wire.FactoryFilters):
    if filters.team_id is not None:
        query = query.where(TeamGithubScope.preset_id == filters.team_id)
    if filters.scope_id is not None:
        query = query.where(TeamGithubScope.id == filters.scope_id)
    return query


def _repository(scope) -> wire.WorkRepository:
    identity = github_identity(scope)
    return wire.WorkRepository(scope_id=scope.id, github=identity,
                               display_name=f"{identity.owner}/{identity.name}")


def _recorded_repository(scope_id: int, issue_url: str, number: int) -> wire.WorkRepository | None:
    """The repository named by a stored issue URL, or None when the URL does not name this issue."""
    match = _ISSUE_URL.fullmatch(issue_url or "")
    if match is None or int(match.group(3)) != number or match.group(2) in (".", ".."):
        return None
    return wire.WorkRepository(scope_id=scope_id, github=wire.GithubIdentity(owner=match.group(1), name=match.group(2)),
                               display_name=f"{match.group(1)}/{match.group(2)}")


async def _waiting(db: AsyncSession, filters: wire.FactoryFilters):
    # One row for each repository and issue, even when several scopes recorded the same wait. The database
    # keeps the newest, orders and cuts, so a response builds at most one row more than it returns.
    ranked = _selected_scopes(
        select(
            TeamGithubScope.id.label("id"), TeamGithubScope.repo_owner.label("repo_owner"),
            TeamGithubScope.repo_name.label("repo_name"), GithubMilestoneWait.issue_number.label("issue_number"),
            GithubMilestoneWait.milestone_title.label("milestone"),
            GithubMilestoneWait.github_updated_at.label("observed_at"),
            func.row_number().over(
                partition_by=(func.lower(TeamGithubScope.repo_owner), func.lower(TeamGithubScope.repo_name),
                              GithubMilestoneWait.issue_number),
                order_by=(GithubMilestoneWait.github_updated_at.desc(), GithubMilestoneWait.scope_id),
            ).label("rank"),
        )
        .join(TeamGithubScope, TeamGithubScope.id == GithubMilestoneWait.scope_id)
        .where(GithubMilestoneWait.milestone_title.is_not(None)), filters,
    ).subquery()
    rows = (await db.execute(
        select(ranked).where(ranked.c.rank == 1)
        .order_by(ranked.c.observed_at.desc(), ranked.c.id, ranked.c.issue_number).limit(ROW_LIMIT + 1)
    )).all()
    waiting = [wire.WaitingIssue(repository=_repository(row), issue_number=row.issue_number,
                                 milestone=row.milestone, observed_at=row.observed_at)
               for row in rows[:ROW_LIMIT]]
    return waiting, len(rows) > ROW_LIMIT


async def _deferred(db: AsyncSession, filters: wire.FactoryFilters):
    later = aliased(GithubWorkItem)
    # The deferral shows only while it is the newest item of its issue. The issue is named by its GitHub URL,
    # which does not change when a scope changes its repository.
    superseded = (select(later.id).where(
        func.lower(later.issue_url) == func.lower(GithubWorkItem.issue_url), later.id > GithubWorkItem.id).exists())
    latest_event = (select(func.max(FactoryAuditEvent.id))
                    .where(FactoryAuditEvent.item_id == GithubWorkItem.id, *_milestone_deferral_facts())
                    .correlate(GithubWorkItem).scalar_subquery())
    rows = (await db.execute(_selected_scopes(
        select(GithubWorkItem, TeamGithubScope, AgentTeamPreset, FactoryAuditEvent)
        .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
        .join(AgentTeamPreset, AgentTeamPreset.id == TeamGithubScope.preset_id)
        .join(FactoryAuditEvent, FactoryAuditEvent.id == latest_event)
        .where(GithubWorkItem.dispatch_status == "cancelled", ~superseded,
               # A first cut in the database; _recorded_repository checks the URL exactly.
               func.lower(GithubWorkItem.issue_url).like(
                   literal("https://github.com/%/%/issues/").concat(cast(GithubWorkItem.issue_number, String))
               )), filters,
    ).order_by(FactoryAuditEvent.occurred_at.desc(), GithubWorkItem.id.desc()).limit(ROW_LIMIT + 1))).all()
    deferred = []
    for item, scope, team, event in rows[:ROW_LIMIT]:
        repository = _recorded_repository(scope.id, item.issue_url, item.issue_number)
        if repository is None:
            continue
        target = (event.after_values or {}).get("target_milestone")
        deferred.append(wire.DeferredIssue(
            repository=repository, team=wire.TeamReference(id=team.id, name=team.name),
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

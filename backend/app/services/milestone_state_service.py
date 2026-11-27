"""Read-only view of milestone moves: deferred items and issues that wait for a team (#1337).

A wait is a row of `github_milestone_waits` with a title. A deferral is a cancelled work item whose
applied "deferred" end-stopped audit event names the target milestone. Nothing here writes, calls
GitHub or sends Mail.

A deferral uses the repository in its stored GitHub issue URL. A wait uses its repository stamp.
Legacy waits whose repository is unknown stay visible without a link and are kept separate.
"""
from __future__ import annotations

import re

from pydantic import ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import factory_schemas as wire
from app.models.database import (
    AgentTeamPreset, FactoryAuditEvent, GithubMilestoneWait, GithubWorkItem, TeamGithubScope,
)
from app.services.factory_projection_service import utc_now
# One definition of "deferred by milestone": the watcher writes it, so this view reads the same facts.
from app.services.github_watcher_service import _milestone_deferral_facts

ROW_LIMIT = 100

# The same owner and name rules as the factory reads, applied to the stored references.
_REPOSITORY_PATH = r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9_.-]{1,100})"
_REPOSITORY_STAMP = re.compile(_REPOSITORY_PATH)
_ISSUE_URL = re.compile(rf"https://github\.com/{_REPOSITORY_PATH}/issues/([0-9]+)")


def _selected_scopes(query, filters: wire.FactoryFilters):
    if filters.team_id is not None:
        query = query.where(TeamGithubScope.preset_id == filters.team_id)
    if filters.scope_id is not None:
        query = query.where(TeamGithubScope.id == filters.scope_id)
    return query


def _repository(scope_id: int, owner: str, name: str) -> wire.WorkRepository | None:
    if name in (".", ".."):
        return None
    return wire.WorkRepository(scope_id=scope_id, github=wire.GithubIdentity(owner=owner, name=name),
                               display_name=f"{owner}/{name}")


def _recorded_repository(scope_id: int, issue_url: str, number: int) -> wire.WorkRepository | None:
    """The repository named by a stored issue URL, or None when the URL does not name this issue."""
    match = _ISSUE_URL.fullmatch(issue_url or "")
    if match is None or int(match.group(3)) != number:
        return None
    return _repository(scope_id, match.group(1), match.group(2))


# The characters that make a milestone title blank: exactly those that Python's str.strip() removes. The query
# leaves out a wait with a blank title before the ranking, so such a row never takes the place of one that can be shown.
_BLANK = "".join(char for char in map(chr, range(0x3001)) if char.isspace())


async def _page(db: AsyncSession, query, build):
    """The first ROW_LIMIT rows that `build` accepts, and whether one more exists.

    The rows are read in ordered batches. A stored row that cannot be shown (`build` gives None) is
    skipped and never takes the place of a row that can be shown, so a list is cut by shown rows only.
    """
    shown, offset = [], 0
    while len(shown) <= ROW_LIMIT:
        rows = (await db.execute(query.limit(ROW_LIMIT + 1).offset(offset))).all()
        for row in rows:
            built = build(row)
            if built is not None:
                shown.append(built)
                if len(shown) > ROW_LIMIT:
                    break
        if len(rows) < ROW_LIMIT + 1:
            break
        offset += len(rows)
    return shown[:ROW_LIMIT], len(shown) > ROW_LIMIT


async def _waiting(db: AsyncSession, filters: wire.FactoryFilters):
    repository = GithubMilestoneWait.repository
    # One row for each stamped repository and issue. Each unstamped scope keeps its own row. A row that cannot be
    # shown is left out before the ranking, so it never takes the place of one that can. The database keeps
    # the newest, orders and cuts, so a response builds at most one row more than it returns.
    ranked = _selected_scopes(
        select(
            GithubMilestoneWait.scope_id.label("id"), repository, GithubMilestoneWait.issue_number.label("issue_number"),
            GithubMilestoneWait.milestone_title.label("milestone"),
            GithubMilestoneWait.github_updated_at.label("observed_at"),
            func.row_number().over(
                partition_by=(func.lower(repository), GithubMilestoneWait.issue_number,
                              case((repository.is_(None), GithubMilestoneWait.scope_id), else_=0)),
                order_by=(GithubMilestoneWait.github_updated_at.desc(), GithubMilestoneWait.scope_id),
            ).label("rank"),
        )
        .join(TeamGithubScope, TeamGithubScope.id == GithubMilestoneWait.scope_id)
        .where(GithubMilestoneWait.milestone_title.is_not(None),
               func.length(func.trim(GithubMilestoneWait.milestone_title, _BLANK)) > 0), filters,
    ).subquery()

    def build(row):
        match = _REPOSITORY_STAMP.fullmatch(row.repository or "")
        repository = _repository(row.id, *match.groups()) if match else None
        try:
            return wire.WaitingIssue(repository=repository, issue_number=row.issue_number,
                                     milestone=row.milestone, observed_at=row.observed_at)
        except ValidationError:
            return None

    return await _page(db, select(ranked).where(ranked.c.rank == 1)
                       .order_by(ranked.c.observed_at.desc(), ranked.c.id, ranked.c.issue_number), build)


async def _deferred(db: AsyncSession, filters: wire.FactoryFilters):
    later = aliased(GithubWorkItem)
    # The deferral shows only while it is the newest item of its issue. The issue is named by its number and
    # its GitHub URL, which do not change when a scope changes its repository.
    superseded = (select(later.id).where(
        later.issue_number == GithubWorkItem.issue_number,
        func.lower(later.issue_url) == func.lower(GithubWorkItem.issue_url), later.id > GithubWorkItem.id).exists())
    latest_event = (select(func.max(FactoryAuditEvent.id))
                    .where(FactoryAuditEvent.item_id == GithubWorkItem.id, *_milestone_deferral_facts())
                    .correlate(GithubWorkItem).scalar_subquery())
    query = _selected_scopes(
        select(GithubWorkItem, TeamGithubScope, AgentTeamPreset, FactoryAuditEvent)
        .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
        .join(AgentTeamPreset, AgentTeamPreset.id == TeamGithubScope.preset_id)
        .join(FactoryAuditEvent, FactoryAuditEvent.id == latest_event)
        .where(GithubWorkItem.dispatch_status == "cancelled", ~superseded), filters,
    ).order_by(FactoryAuditEvent.occurred_at.desc(), GithubWorkItem.id.desc())

    def build(row):
        item, scope, team, event = row
        repository = _recorded_repository(scope.id, item.issue_url, item.issue_number)
        if repository is None:
            return None
        target = (event.after_values or {}).get("target_milestone")
        try:
            return wire.DeferredIssue(
                repository=repository, team=wire.TeamReference(id=team.id, name=team.name),
                work_item_id=item.id, issue_number=item.issue_number, issue_title=item.issue_title,
                target_milestone=target if isinstance(target, str) and target else None,
                deferred_at=event.occurred_at, previous_branch=item.dispatch_head_ref)
        except ValidationError:
            return None

    return await _page(db, query, build)


async def milestone_states(db: AsyncSession, filters: wire.FactoryFilters) -> wire.MilestoneStatesResponse:
    waiting, waiting_truncated = await _waiting(db, filters)
    deferred, deferred_truncated = await _deferred(db, filters)
    return wire.MilestoneStatesResponse(
        generated_at=utc_now(), filters=filters, waiting=waiting, waiting_truncated=waiting_truncated,
        deferred=deferred, deferred_truncated=deferred_truncated)

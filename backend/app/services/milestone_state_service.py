"""Read-only view of milestone moves: deferred items and issues that wait for a team (#1337).

A wait is a row of `github_milestone_waits` with a title. A deferral is a cancelled work item whose
applied "deferred" end-stopped audit event names the target milestone. Nothing here writes, calls
GitHub or sends Mail.

A row keeps the repository it was recorded for. A deferral takes it from the GitHub issue URL that its
item stored at admission. A wait has no stored repository, so the scope PATCH deletes the waits of a
scope when its repository changes. The PATCH of an earlier version left them behind, and the GitHub
update time of a wait does not say where the watcher saw the issue. So the waits of a scope that has
any applied repository change have no provable repository: each row stays in the list with its issue
number and no repository, and it is never merged with another row. The repository stamp that would
prove it is #1372.
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
from app.services.factory_projection_service import FactoryReadError, github_identity, utc_now
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


_REPOSITORY_FIELDS = ("repo_owner", "repo_name")
# The characters that make a milestone title blank. SQL and Python use the same set, so the database and
# the view agree on which wait rows can be shown.
_BLANK = " \t\r\n\x0b\x0c"


def _changes_repository(changed_fields, before: dict, after: dict) -> bool:
    """Whether one applied scope-change event changed the repository of its scope.

    A change of case only is not a change of repository. The ledger stores sanitized values, so two
    different names can look the same. When the receipt names the field and the values cannot tell,
    the repository is unknown, and the event counts as a change.
    """
    named = [name for name in _REPOSITORY_FIELDS if name in (changed_fields or [])]
    for name in _REPOSITORY_FIELDS:
        if str(before.get(name) or "").lower() != str(after.get(name) or "").lower():
            return True
    return any(before.get(name) == after.get(name) for name in named)


async def _unproven_scopes(db: AsyncSession, filters: wire.FactoryFilters) -> set[int]:
    """The selected scopes whose waits prove no repository.

    These are the scopes with an applied repository change, and the scopes whose identity the factory refuses.
    """
    unproven = await _moved_scopes(db, filters)
    for scope in (await db.scalars(_selected_scopes(select(TeamGithubScope), filters))).all():
        try:
            _repository(scope)
        except (FactoryReadError, ValidationError):
            unproven.add(scope.id)
    return unproven


async def _moved_scopes(db: AsyncSession, filters: wire.FactoryFilters) -> set[int]:
    """The selected scopes that have an applied repository change, from the scope-change events.

    The scope PATCH records the event in the transaction that changes the scope, and it has done so
    since the policy ledger began.
    """
    events = (await db.execute(_selected_scopes(
        select(FactoryAuditEvent.scope_id, FactoryAuditEvent.context_snapshot,
               FactoryAuditEvent.before_values, FactoryAuditEvent.after_values)
        .join(TeamGithubScope, TeamGithubScope.id == FactoryAuditEvent.scope_id)
        .where(FactoryAuditEvent.event_kind == "policy_change",
               FactoryAuditEvent.source == "agent_teams.update_github_scope",
               FactoryAuditEvent.action_outcome == "applied"), filters,
    ))).all()
    return {scope_id for scope_id, snapshot, before, after in events
            if _changes_repository((snapshot or {}).get("scope_changed_fields"), before or {}, after or {})}


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
    unproven = await _unproven_scopes(db, filters)
    # One row for each repository and issue, even when several scopes recorded the same wait. The rows of a
    # scope that proves no repository are each their own row. A row that cannot be
    # shown is left out before the ranking, so it never takes the place of one that can. The database keeps
    # the newest, orders and cuts, so a response builds at most one row more than it returns.
    ranked = _selected_scopes(
        select(
            TeamGithubScope.id.label("id"), TeamGithubScope.repo_owner.label("repo_owner"),
            TeamGithubScope.repo_name.label("repo_name"), GithubMilestoneWait.issue_number.label("issue_number"),
            GithubMilestoneWait.milestone_title.label("milestone"),
            GithubMilestoneWait.github_updated_at.label("observed_at"),
            func.row_number().over(
                partition_by=(func.lower(TeamGithubScope.repo_owner), func.lower(TeamGithubScope.repo_name),
                              GithubMilestoneWait.issue_number,
                              case((TeamGithubScope.id.in_(unproven), TeamGithubScope.id), else_=0)),
                order_by=(GithubMilestoneWait.github_updated_at.desc(), GithubMilestoneWait.scope_id),
            ).label("rank"),
        )
        .join(TeamGithubScope, TeamGithubScope.id == GithubMilestoneWait.scope_id)
        .where(GithubMilestoneWait.milestone_title.is_not(None),
               func.length(func.trim(GithubMilestoneWait.milestone_title, _BLANK)) > 0), filters,
    ).subquery()

    def build(row):
        if not row.milestone.strip(_BLANK):
            return None
        # A scope with a repository change, and a scope whose identity the factory refuses, prove no
        # repository for its waits. Those rows name none.
        repository = None if row.id in unproven else _repository(row)
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

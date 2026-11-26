"""Read campaign counts without changing dispatch or approval authority."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import time

from sqlalchemy import select

from app.config import settings
from app.models.database import AgentTeamPreset, AgentTeamSlot, GithubWorkItem, TeamGithubScope
from app.models.factory_schemas import (
    CampaignAreaGroup, CampaignDecision, CampaignDispatchItem, CampaignDoneItem, CampaignQueuedItem,
    CampaignUnderWayItem, TeamCampaignSummary,
)
from app.models.schemas import PlannedCampaignDecision
from app.services.github_dispatch_priority import dispatch_order_key, effective_priority
from app.services.work_item_states import is_done, needs_attention

logger = logging.getLogger(__name__)

_PARENT = re.compile(r"https://github\.com/([^/]+)/([^/]+)/issues/([1-9][0-9]*)\Z")
_STATE_PHRASES = {
    "dispatched": "building it",
    "verifying": "waiting for checks",
    "ready_for_review": "reviewing it",
    "awaiting_human_review": "waiting for you",
}
_DECISION_TEXT = {
    "review_pr": "Review the pull request.",
    "merge_pr": "Decide whether to merge the pull request.",
    "inspect_attempt": "Decide how to continue the work.",
    "inspect_checkpoint": "Decide how to recover the work.",
    "milestone_acceptance": "Decide whether to accept the milestone.",
    "pilot_decision": "Decide whether to continue the trial.",
    "scope_clarification": "Clarify the work scope.",
    "provide_evidence": "Provide the required evidence.",
}


@dataclass(frozen=True)
class BacklogObservation:
    read_at: float
    # None means unavailable. An empty tuple means a complete, empty backlog.
    issues: tuple[tuple[int, tuple[str, ...]], ...] | None
    observed_at: datetime | None


def _parent(url):
    match = _PARENT.fullmatch(url or "")
    return (match[1], match[2], int(match[3])) if match else None


def _branch(value):
    return value.removeprefix("origin/") if value else None


class AgentTeamCampaignService:
    def __init__(self, *, interval_seconds=None, clock=time.monotonic, wall_clock=None):
        self.interval_seconds = max(1, interval_seconds or settings.github_dispatch_interval_seconds)
        self.clock = clock
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[tuple, BacklogObservation] = {}

    def _expires_at(self, observation: BacklogObservation) -> float:
        # A failed first read delays the next attempt for one interval.
        intervals = 1 if observation.issues is None else 3
        return observation.read_at + intervals * self.interval_seconds

    async def _scopes(self, db, preset_id=None, scope_id=None):
        query = select(TeamGithubScope.id, TeamGithubScope.preset_id, TeamGithubScope.created_at,
            TeamGithubScope.repo_owner, TeamGithubScope.repo_name, TeamGithubScope.base_ref,
            AgentTeamPreset.parent_issue_url, AgentTeamPreset.backlog_milestone).join(
            AgentTeamPreset, AgentTeamPreset.id == TeamGithubScope.preset_id)
        if preset_id is not None:
            query = query.where(TeamGithubScope.preset_id == preset_id)
        if scope_id is not None:
            query = query.where(TeamGithubScope.id == scope_id)
        return (await db.execute(query.order_by(TeamGithubScope.id))).all()

    @staticmethod
    def _key(scope):
        return (scope.id, scope.preset_id, scope.created_at, scope.repo_owner, scope.repo_name,
                scope.parent_issue_url, scope.backlog_milestone)

    async def _read_backlog(self, scope, client):
        owner, repo = scope.repo_owner, scope.repo_name
        parent = _parent(scope.parent_issue_url)
        if scope.backlog_milestone:
            milestones = await client.list_milestones(owner, repo)
            matches = [m for m in milestones if m.get("title") == scope.backlog_milestone]
            if len(matches) != 1:
                return None
            milestone = matches[0].get("number")
        elif parent and parent[:2] == (owner, repo):
            issue = await client.get_issue(owner, repo, parent[2])
            milestone_data = issue.get("milestone") if issue else None
            milestone = milestone_data.get("number") if isinstance(milestone_data, dict) else None
        else:
            return None
        if type(milestone) is not int or milestone <= 0:
            return None
        issues = await client.list_milestone_issues(owner, repo, milestone)
        observed = {}
        for issue in issues:
            if not isinstance(issue, dict):
                return None
            if "pull_request" in issue:
                continue
            number, labels = issue.get("number"), issue.get("labels")
            if (type(number) is not int or number <= 0 or not isinstance(labels, list)
                    or not all(isinstance(label, dict) and isinstance(label.get("name"), str) for label in labels)):
                return None
            if parent == (owner, repo, number):
                continue
            value = tuple(sorted({label["name"] for label in labels}))
            if number in observed and observed[number] != value:
                return None
            observed[number] = value
        return tuple(sorted(observed.items()))

    async def refresh_scope(self, db, scope, client):
        """Use the existing poll client. Keep this read outside dispatch decisions."""
        scope_id = scope.id
        rows = await self._scopes(db, scope_id=scope_id)
        if not rows:
            return
        captured = rows[0]
        # #1243: a team recorded at the upgrade gets its campaign once, through this poll's client. It runs in its own
        # session (V1 review 6078808159), so its commits and rollbacks never expire what the caller loaded. A failure
        # stays pending for a later poll and never stops the backlog read.
        from sqlalchemy.ext.asyncio import AsyncSession

        from app.services import campaign_upgrade_service

        try:
            async with AsyncSession(db.bind, expire_on_commit=False) as own:
                await campaign_upgrade_service.resolve_pending(own, captured.preset_id, client)
        except Exception:  # noqa: BLE001  (the backlog read must run whatever happens here)
            logger.info("Upgrade campaign unresolved for team %s", captured.preset_id)
        key, started = self._key(captured), self.clock()
        cached = self._cache.get(key)
        if cached and cached.read_at + self.interval_seconds > started:
            return
        observed_at = self.wall_clock()
        try:
            issues = await asyncio.wait_for(self._read_backlog(captured, client), timeout=8)
        except Exception:
            # Never expose credential-bearing request objects or raw GitHub errors.
            issues = None
        fresh = await self._scopes(db, scope_id=scope_id)
        if not fresh or self._key(fresh[0]) != key:
            return
        now = self.clock()
        latest = self._cache.get(key)
        if latest is not None:
            if latest.read_at > started and (latest.issues is not None or issues is None):
                return
            if issues is None and latest.issues is not None and self._expires_at(latest) > now:
                return
        self._cache = {k: v for k, v in self._cache.items() if self._expires_at(v) > now}
        if key not in self._cache and len(self._cache) >= 1024:
            oldest = min(self._cache, key=lambda k: self._expires_at(self._cache[k]))
            self._cache.pop(oldest)
        self._cache[key] = BacklogObservation(started, issues, observed_at if issues is not None else None)

    async def summary(self, db, preset_id, human_actions=(), scope_id=None, provider=None):
        """The campaign counts. scope_id narrows them to one repository and provider to the work that an owner of
        that harness holds (#1142), as the Work page does; work without an owner has no harness and is not counted."""
        scopes = await self._scopes(db, preset_id=preset_id)
        owned = {scope.id for scope in scopes}
        roster = provider is None or await db.scalar(select(AgentTeamSlot.id).where(
            AgentTeamSlot.preset_id == preset_id, AgentTeamSlot.provider == provider).limit(1)) is not None
        if scope_id is not None:
            scopes = [scope for scope in scopes if scope.id == scope_id]
        # A team outside the filter has no campaign to show.
        matches = roster and (scope_id is None or scope_id in owned)
        if not matches:
            scopes = []
        by_scope = {scope.id: scope for scope in scopes}
        now = self.clock()
        observations = {scope.id: self._cache.get(self._key(scope)) for scope in scopes}
        available = bool(scopes) and all(observation is not None and self._expires_at(observation) > now
            and observation.issues is not None for observation in observations.values())
        oldest_observation = min(observations.values(), key=lambda observation: observation.read_at) if available else None
        backlog = {}
        if available:
            for scope in scopes:
                for number, labels in observations[scope.id].issues:
                    backlog[(scope.repo_owner, scope.repo_name, number)] = labels
        rows = (await db.execute(select(GithubWorkItem.id, GithubWorkItem.scope_id,
            GithubWorkItem.issue_number, GithubWorkItem.issue_title, GithubWorkItem.dispatch_status,
            GithubWorkItem.dispatch_priority,
            GithubWorkItem.dispatch_base_ref, GithubWorkItem.auto_merged_at,
            AgentTeamSlot.display_name).join(TeamGithubScope,
                TeamGithubScope.id == GithubWorkItem.scope_id).outerjoin(AgentTeamSlot,
                AgentTeamSlot.id == GithubWorkItem.owner_slot_id).where(
                TeamGithubScope.preset_id == preset_id,
                *([GithubWorkItem.scope_id.in_([scope.id for scope in scopes])] if scope_id is not None or not matches else []),
                *([AgentTeamSlot.provider == provider] if provider is not None else [])
            ).order_by(GithubWorkItem.id))).all()
        # Dispatch watches the scope, while campaign counts may watch only a
        # milestone. Keep the complete pending queue independent of those counts.
        positions, dispatch_order = Counter(), []
        pending = sorted((row for row in rows if row.dispatch_status == "pending"),
                         key=lambda row: (row.scope_id, *dispatch_order_key(row)))
        for row in pending:
            scope = by_scope[row.scope_id]
            positions[row.scope_id] += 1
            dispatch_order.append(CampaignDispatchItem(title=row.issue_title,
                scope_id=row.scope_id, repository=f"{scope.repo_owner}/{scope.repo_name}",
                issue_number=row.issue_number, priority=effective_priority(row.dispatch_priority),
                dispatch_position=positions[row.scope_id]))
        work = {}
        for row in rows:
            scope = by_scope[row.scope_id]
            key = (scope.repo_owner, scope.repo_name, row.issue_number)
            if _parent(scope.parent_issue_url) == key:
                continue
            if not available or key in backlog:
                work[key] = row
        done, under_way, queued = [], [], []
        other_branch = attention = 0
        for row in work.values():
            scope = by_scope[row.scope_id]
            if is_done(row.dispatch_status):
                # One definition of done in every view (#1106). A merge into a branch other than the scope's is still
                # done; it is disclosed rather than dropped from every bucket. Only a recorded base is evidence of
                # another branch: rows from before dispatch_base_ref existed have none and are not claimed.
                done.append(row)
                merged_base = _branch(row.dispatch_base_ref)
                if row.dispatch_status == "merged" and merged_base is not None and merged_base != _branch(scope.base_ref):
                    other_branch += 1
            elif needs_attention(row.dispatch_status):
                # Escalated or failed: someone must act, so it is never under way (#1129), as on the tiles.
                attention += 1
            elif row.dispatch_status in _STATE_PHRASES:
                under_way.append(CampaignUnderWayItem(title=row.issue_title,
                    owner_short_name=row.display_name.split()[0] if row.display_name and row.display_name.split() else None,
                    state_phrase=_STATE_PHRASES[row.dispatch_status]))
            elif row.dispatch_status == "pending":
                queued.append(CampaignQueuedItem(title=row.issue_title))
        done.sort(key=lambda row: (row.auto_merged_at or datetime.min, row.id), reverse=True)
        groups = Counter()
        if available and provider is None:
            slot_labels = (await db.scalars(select(AgentTeamSlot.area_labels).where(
                AgentTeamSlot.preset_id == preset_id).order_by(AgentTeamSlot.position, AgentTeamSlot.id))).all()
            areas = list(dict.fromkeys(label for labels in slot_labels for label in (labels or []) if isinstance(label, str)))
            for key, labels in backlog.items():
                if key not in work:
                    groups[next((area for area in areas if area in labels), "Other")] += 1
        decisions = []
        for action in human_actions:
            if (action.assessment_current and action.state in {"requested", "context_pending"}
                    and action.kind in _DECISION_TEXT):
                text = "Complete the action details." if action.state == "context_pending" else _DECISION_TEXT[action.kind]
                decision = CampaignDecision(text=text, state="now")
                if decision not in decisions:
                    decisions.append(decision)
        planned = await db.scalar(select(AgentTeamPreset.planned_decisions).where(AgentTeamPreset.id == preset_id))
        for value in (planned or []) if matches else []:
            # Legacy or manually damaged data cannot insert raw objects into the response.
            try:
                decision = PlannedCampaignDecision.model_validate(value)
            except ValueError:
                continue
            decisions.append(CampaignDecision(text=decision.text, state=decision.state))
        return TeamCampaignSummary(backlog_state="available" if available else "backlog unavailable",
            backlog_observed_at=oldest_observation.observed_at if oldest_observation else None,
            backlog_stale=oldest_observation is not None and oldest_observation.read_at + self.interval_seconds < now,
            done_count=len(done), done_on_other_branch_count=other_branch, latest_done=[CampaignDoneItem(title=row.issue_title,
                merged_at=row.auto_merged_at) for row in done[:3]],
            under_way_count=len(under_way), under_way=under_way, attention_count=attention, queued_count=len(queued), queued=queued,
            dispatch_order=dispatch_order,
            not_started_count=sum(groups.values()) if available else None,
            not_started_groups=[CampaignAreaGroup(area=area, count=count) for area, count in sorted(groups.items())],
            decisions_ahead=decisions)


agent_team_campaign_service = AgentTeamCampaignService()

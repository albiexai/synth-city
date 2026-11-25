"""Collect existing team signals. This read grants no execution authority."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from functools import partial
from types import SimpleNamespace
from typing import get_args

from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import OperationalError
from mcp_shim.work_remaining_protocol import MAX_AGE

from app.models.database import (
    AgentTeamPreset, AgentTeamSlot, GithubApprovalRequest, GithubOwnerObservationPause,
    GithubWorkItem, GithubWorkspace, MailExternalActor, TeamGithubScope,
)
from app.models.factory_schemas import (
    AgentTeamSummaryResponse, TeamRemainingWork, TeamSummaryAttention, TeamSummaryHumanAction,
    TeamRequiredCIUnreadable, TeamSummaryActivityReason, TeamSummaryCoverageReason, TeamWaitingInput,
)
from app.models.schemas import UnansweredRequestView
from app.services.github_coordination_service import hold_code
from app.services.github_dispatch_service import ESCALATION_REASONS
from app.services.github_operator_attention_service import github_operator_attention_service
from app.services.github_operator_context_service import bind_attempt
from app.services.github_work_progress_service import github_work_progress_service
from app.services.agent_team_campaign_service import agent_team_campaign_service
from app.services import required_ci_attention
from app.services.agent_team_supervisor_service import observe_supervisor, supervisor_observation, team_needs_no_supervision
from app.services.work_item_states import ATTENTION_STATUSES, FINISHED_STATUSES

_ACTORS = ("owner", "leader", "operator", "controller", "reviewer")
_KINDS = {"review_pr", "merge_pr", "inspect_attempt", "inspect_checkpoint", "milestone_acceptance",
          "pilot_decision", "scope_clarification", "provide_evidence"}
_STATES = {"requested", "context_pending", "waiting_for_prerequisites", "historical", "resolved",
           "head_changed", "pr_unavailable", "pr_identity_unavailable"}
_LEGACY_OBSERVATION_PAUSE_REASON = "owner_observation_pause"
_INSPECTION_REASONS = ESCALATION_REASONS | {_LEGACY_OBSERVATION_PAUSE_REASON}
_ACTIVITY_REASONS = frozenset(get_args(TeamSummaryActivityReason))
_SUMMARY_VALIDITY_SECONDS = 30


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class _FilterMatch:
    """Which rows of one team a Repository and Assigned-harness filter keeps (#1141, #1142).

    scope_id keeps rows of that repository. provider keeps rows whose owner slot has that harness (a work item's
    owner, a reminder's or waiting member's slot); a repository row with no owner has no harness and is dropped.
    A team-level row (no scope, no owner) shows only when the team owns the repository and has a slot of that harness.
    """

    def __init__(self, scope_id, provider, team_scopes, slots, owners):
        self.scope_id, self.provider = scope_id, provider
        self.team_scopes, self.slots, self.owners = team_scopes, slots, owners

    @property
    def team_matches(self):
        return ((self.scope_id is None or self.scope_id in self.team_scopes)
                and (self.provider is None or self.provider in self.slots.values()))

    def visible(self, scope, work_item_id=None, slot_id=None):
        if self.scope_id is not None:
            # A row without a scope belongs to the team: it shows only when the team owns the repository.
            if scope is None and self.scope_id not in self.team_scopes or scope is not None and scope != self.scope_id:
                return False
        if self.provider is None:
            return True
        owner = slot_id if slot_id is not None else self.owners.get(work_item_id)
        if owner is not None:
            return self.slots.get(owner) == self.provider
        # Team-level rows follow the configured roster; a repository row without an owner has no harness.
        return scope is None and work_item_id is None and self.provider in self.slots.values()


class AgentTeamSummaryService:
    def __init__(self, *, progress=None, attention=None, campaign=None, observation_seconds=8, item_limit=64):
        self.read_progress = progress.summary if progress is not None else partial(
            github_work_progress_service.summary, validity_seconds=_SUMMARY_VALIDITY_SECONDS)
        self.attention = attention or github_operator_attention_service
        self.campaign = campaign or agent_team_campaign_service
        self.observation_seconds = min(8, max(0.001, observation_seconds))
        self.item_limit = min(64, max(1, item_limit))

    async def _matcher(self, db, preset_id, scope_id, provider):
        if scope_id is None and provider is None:
            return None
        team_scopes = set((await db.execute(
            select(TeamGithubScope.id).where(TeamGithubScope.preset_id == preset_id))).scalars())
        slots = {row.id: row.provider for row in (await db.execute(
            select(AgentTeamSlot.id, AgentTeamSlot.provider).where(AgentTeamSlot.preset_id == preset_id))).all()}
        owners = {row.id: row.owner_slot_id for row in (await db.execute(
            select(GithubWorkItem.id, GithubWorkItem.owner_slot_id).join(
                TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id).where(
                TeamGithubScope.preset_id == preset_id))).all()}
        return _FilterMatch(scope_id, provider, team_scopes, slots, owners)

    async def _snapshot(self, db, preset_id, scope_id=None, provider=None):
        team = (await db.execute(select(AgentTeamPreset.id, AgentTeamPreset.description,
            AgentTeamPreset.parent_issue_url, AgentTeamPreset.autonomy_enabled, AgentTeamPreset.updated_at,
            AgentTeamPreset.backlog_milestone, AgentTeamPreset.planned_decisions,
            AgentTeamPreset.supervisor_actor_id, AgentTeamPreset.supervisor_checked_at,
            AgentTeamPreset.supervisor_next_check_at, AgentTeamPreset.supervisor_grace_seconds,
            AgentTeamPreset.supervisor_note, AgentTeamPreset.supervisor_observed_status,
            MailExternalActor.display_name.label("supervisor_display_name"))
            .outerjoin(MailExternalActor, MailExternalActor.id == AgentTeamPreset.supervisor_actor_id)
            .where(AgentTeamPreset.id == preset_id))).one_or_none()
        if team is None:
            return None
        scopes = tuple((await db.execute(select(TeamGithubScope.id, TeamGithubScope.repo_owner,
            TeamGithubScope.repo_name, TeamGithubScope.merge_policy, TeamGithubScope.enabled,
            TeamGithubScope.updated_at).where(TeamGithubScope.preset_id == preset_id)
            .order_by(TeamGithubScope.id).limit(33))).all())
        selected = (select(GithubWorkItem).join(TeamGithubScope,
            TeamGithubScope.id == GithubWorkItem.scope_id).where(TeamGithubScope.preset_id == preset_id,
            GithubWorkItem.dispatch_status.not_in(FINISHED_STATUSES)))
        # #1142: a repository or harness filter narrows the unfinished items themselves, so remaining work, next
        # actors and coverage are computed over the same work. A harness keeps only owned work.
        if scope_id is not None:
            selected = selected.where(GithubWorkItem.scope_id == scope_id)
        if provider is not None:
            selected = selected.join(AgentTeamSlot, AgentTeamSlot.id == GithubWorkItem.owner_slot_id).where(
                AgentTeamSlot.preset_id == preset_id, AgentTeamSlot.provider == provider)
        count = await db.scalar(select(func.count()).select_from(selected.subquery()))
        # Private bindings exist only in the local equality check. Never serialize them.
        items = tuple((await db.execute(selected.with_only_columns(
            GithubWorkItem.id, GithubWorkItem.scope_id, GithubWorkItem.issue_number,
            GithubWorkItem.dispatch_status, GithubWorkItem.escalation_reason, GithubWorkItem.dispatch_nonce,
            GithubWorkItem.owner_slot_id, GithubWorkItem.active_scope_revision, GithubWorkItem.attempt_phase,
            GithubWorkItem.dispatch_head_ref, GithubWorkItem.last_verified_sha, GithubWorkItem.updated_at,
            GithubWorkItem.checks_unavailable_since, GithubWorkItem.checks_unavailable_reason,
            GithubWorkspace.id.label("workspace_id"), GithubWorkspace.lease_token,
            GithubWorkspace.leased_at, GithubWorkspace.path,
        ).outerjoin(GithubWorkspace, GithubWorkspace.leased_item_id == GithubWorkItem.id)
            .order_by(GithubWorkItem.id).limit(self.item_limit + 1))).all())
        approvals = tuple((await db.execute(select(GithubApprovalRequest.id, GithubApprovalRequest.status,
            GithubApprovalRequest.dispatch_nonce, GithubApprovalRequest.approval_round)
            .join(GithubWorkItem, GithubWorkItem.id == GithubApprovalRequest.work_item_id)
            .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
            .where(TeamGithubScope.preset_id == preset_id, GithubApprovalRequest.status == "pending")
            .order_by(GithubApprovalRequest.id).limit(65))).all())
        return team, scopes, items, count, approvals

    async def _pauses(self, db, preset_id):
        return tuple((await db.execute(select(GithubOwnerObservationPause.id,
            GithubOwnerObservationPause.work_item_id, GithubOwnerObservationPause.paused_at,
            GithubOwnerObservationPause.resume_deadline, GithubWorkItem.scope_id, GithubWorkItem.issue_number)
            .join(GithubWorkItem, GithubWorkItem.id == GithubOwnerObservationPause.work_item_id)
            .join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id)
            .where(TeamGithubScope.preset_id == preset_id, GithubOwnerObservationPause.status == "paused",
                   GithubWorkItem.dispatch_status == "escalated",
                   GithubWorkItem.escalation_reason == _LEGACY_OBSERVATION_PAUSE_REASON)
            .order_by(GithubOwnerObservationPause.id).limit(65))).all())

    async def _inspection_reasons(self, db, preset_id, actions, dispatch_nonces):
        keys = {
            (action["scope_id"], action["issue_number"], action["work_item_id"], action["dispatch_epoch"])
            for action in actions
            if action["kind"] == "inspect_attempt" and action["state"] in {"requested", "context_pending"}
            and action.get("assessment_current") is True and action.get("work_item_id") in dispatch_nonces
            and action.get("dispatch_epoch")
        }
        if not keys:
            return {}
        # The visible inspections have their own cap. Pending progress rows cannot crowd this read out.
        rows = (await db.execute(select(
            GithubWorkItem.id, GithubWorkItem.scope_id, GithubWorkItem.issue_number,
            GithubWorkItem.dispatched_at, GithubWorkItem.created_at, GithubWorkItem.approval_round_count,
            GithubWorkItem.active_scope_revision, GithubWorkItem.dispatch_nonce, GithubWorkItem.escalation_reason,
        ).join(TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id).where(
            TeamGithubScope.preset_id == preset_id,
            GithubWorkItem.id.in_({key[2] for key in keys}),
            GithubWorkItem.dispatch_status.in_(ATTENTION_STATUSES),
        ))).all()
        reasons = {}
        for row in rows:
            if row.dispatch_nonce != dispatch_nonces[row.id] or row.escalation_reason not in _INSPECTION_REASONS:
                continue
            binding = {}
            bind_attempt(binding, row)
            key = (row.scope_id, row.issue_number, row.id, binding["dispatch_epoch"])
            if key in keys:
                reasons[key] = row.escalation_reason
        return reasons

    async def summary(self, db, preset_id, scope_id=None, provider=None):
        started = datetime.now(timezone.utc)
        deadline = asyncio.get_running_loop().time() + self.observation_seconds
        await observe_supervisor(db, preset_id, now=started)
        matcher = await self._matcher(db, preset_id, scope_id, provider)
        initial = await self._snapshot(db, preset_id, scope_id, provider)
        if initial is None:
            return None
        team, scope_rows, items, total, approvals = initial
        scopes = [SimpleNamespace(**row._mapping) for row in scope_rows[:32]]
        attention, visible_actions, actors, observations = [], [], set(), []
        if team.backlog_milestone:
            from app.services.github_watcher_service import milestone_routes

            for scope in scopes:
                if scope_id is not None and scope.id != scope_id:
                    continue
                routes = await milestone_routes(db, scope)
                if list(routes.values()).count(team.backlog_milestone) > 1:
                    attention.append(TeamSummaryAttention(code="milestone_routing_conflict", scope_id=scope.id))
        coverage = []
        dispatch_nonces = {}
        waiting_input, native_fence, native_known = None, {}, False
        expires = [started + timedelta(seconds=_SUMMARY_VALIDITY_SECONDS)]
        # Remaining work is computed from the unfinished items alone (#1107): their coverage, their reports and a
        # stable snapshot. The human-action, native, pause and dispatch observations feed coverage_complete only, so
        # an aging coordination observation of a paused team no longer flips a finished team's remaining work.
        items_complete = total <= self.item_limit and len(items) == total
        item_expires = list(expires)
        if not items_complete:
            coverage.append(TeamSummaryCoverageReason(code="work_item_limit"))
        if len(scope_rows) > 32:
            coverage.append(TeamSummaryCoverageReason(code="scope_limit"))
        if len(approvals) > 64:
            coverage.append(TeamSummaryCoverageReason(code="approval_limit"))
        hold = hold_code()

        # Reserve five seconds of the eight-second overall deadline for native evidence and mail.
        # Recheck mail near publication. Charge the native and mail phases to their shared budget,
        # without charging intervening GitHub calls to it as well.
        from app.services.mail_request_stall_service import mail_request_stall_service
        loop = asyncio.get_running_loop()
        stall_deadline = min(deadline, loop.time() + mail_request_stall_service.deadline_seconds)

        async def bounded(read, limit=None):
            remaining = (deadline if limit is None else min(deadline, limit)) - loop.time()
            if remaining <= 0:
                raise TimeoutError
            return await asyncio.wait_for(read(), timeout=remaining)

        # The identity baseline is taken BEFORE the native await, inside the stall budget. A failed
        # capture stays None (untrusted): the stall view never takes a replacement baseline.
        native_baseline, native_failure = await mail_request_stall_service.capture_baseline(
            db, preset_id, stall_deadline)
        native_states = {}
        stall_time_left = None
        source = "native"
        reading_item_id = None
        try:
            from app.services.agent_activity_service import counts_as_external_wait, observe_team, _team_inputs
            native = await bounded(lambda: observe_team(db, preset_id, include_agent_status=False,
                persist_identities=False, max_input_rows=256, input_fence=native_fence,
                validity_seconds=_SUMMARY_VALIDITY_SECONDS), stall_deadline)
            stall_time_left = max(0, stall_deadline - loop.time())
            native_known = all(entry.state != "unknown" for entry in native.slots)
            leased_owner_slots = {item.owner_slot_id for item in items if item.lease_token}
            for entry in native.slots:
                reason = getattr(entry, "reason", None)
                # An unlaunched slot with no leased work has no active owner to observe.
                if entry.state != "unknown" or (reason == "no_current_binding" and entry.slot_id not in leased_owner_slots):
                    continue
                coverage.append(TeamSummaryCoverageReason(code="native_unknown", slot_id=entry.slot_id,
                    activity_reason=reason if reason in _ACTIVITY_REASONS else None))
            native_states = {entry.slot_id: entry.state for entry in native.slots}
            waiting_input = [TeamWaitingInput(slot_id=entry.slot_id, reason=entry.waiting_input.reason)
                for entry in native.slots if entry.waiting_input is not None]
            expires.append(_utc(native.valid_until))
            source = "dispatch"
            dispatch = await bounded(lambda: self.attention.dispatch_snapshot(db, scopes))
            if not dispatch["complete"]:
                coverage.append(TeamSummaryCoverageReason(code="dispatch_incomplete"))
            # Capture scalars before another dispatch read can refresh the ORM rows.
            dispatch_nonces = {entry.id: entry.dispatch_nonce for entry in dispatch.get("items", ())}
            for action in dispatch["actions"]:
                kind = action.get("kind")
                code = {"inspect_attempt": "attempt_stopped", "inspect_checkpoint": "recovery_decision",
                        "review_pr": "human_review"}.get(kind)
                if code and (matcher is None or matcher.visible(action["scope_id"], action.get("work_item_id"))):
                    attention.append(TeamSummaryAttention(code=code, work_item_id=action.get("work_item_id"),
                        scope_id=action["scope_id"], issue_number=action["issue_number"]))
                    if kind in {"inspect_attempt", "inspect_checkpoint"}:
                        actors.add("operator")
            source = "pauses"
            pauses = await bounded(lambda: self._pauses(db, preset_id))
            if len(pauses) > 64:
                coverage.append(TeamSummaryCoverageReason(code="pause_limit"))
            for pause in pauses[:64]:
                if matcher is not None and not matcher.visible(pause.scope_id, pause.work_item_id):
                    continue
                attention.append(TeamSummaryAttention(code="observation_pause", pause_id=pause.id,
                    work_item_id=pause.work_item_id, scope_id=pause.scope_id, issue_number=pause.issue_number,
                    observed_at=pause.paused_at, resume_deadline=pause.resume_deadline))
                actors.add("operator")
            for item in items[:self.item_limit]:
                if item.dispatch_status == "escalated" and item.escalation_reason in {
                    "owner_idle_timeout", "owner_unavailable", "prepared_owner_unavailable"}:
                    attention.append(TeamSummaryAttention(code="owner_stalled", work_item_id=item.id,
                        scope_id=item.scope_id, issue_number=item.issue_number))
            source = "human_actions"
            human = await bounded(lambda: self.attention.summary(db, preset_id))
            if not human["coverage_complete"]:
                coverage.append(TeamSummaryCoverageReason(code="human_actions_incomplete"))
            expires.append(_utc(human["observation_expires_at"]))
            for action in human["actions"][:64]:
                kind, state = action.get("kind"), action.get("state")
                if kind not in _KINDS or state not in _STATES:
                    if not any(entry.code == "human_action_invalid" for entry in coverage):
                        coverage.append(TeamSummaryCoverageReason(code="human_action_invalid"))
                    continue
                if matcher is None or matcher.visible(action["scope_id"], action.get("work_item_id")):
                    visible_actions.append(action)
            if len(human["actions"]) > 64:
                coverage.append(TeamSummaryCoverageReason(code="human_action_limit"))
            source = "progress"
            for item in items[:self.item_limit]:
                reading_item_id = item.id
                value = await bounded(lambda: self.read_progress(db, item.id))
                if value is None:
                    coverage.append(TeamSummaryCoverageReason(code="progress_missing", work_item_id=item.id))
                    items_complete = False
                    continue
                observations.append(value)
                expires.append(_utc(value.valid_until))
                item_expires.append(_utc(value.valid_until))
                if value.remaining_work.state == "current" and value.remaining_work.reported_at is not None:
                    expires.append(_utc(value.remaining_work.reported_at) + MAX_AGE)
                    item_expires.append(_utc(value.remaining_work.reported_at) + MAX_AGE)
                if value.next_actor in _ACTORS:
                    actors.add(value.next_actor)
            source, reading_item_id = "native", None
            native_current = {entry[0]: entry for entry in await bounded(lambda: _team_inputs(db, preset_id, 256))}
            if native_current != native_fence:
                waiting_input = None
                coverage.append(TeamSummaryCoverageReason(code="fence_changed", source="native"))
        except (TimeoutError, OSError, ValueError, OperationalError) as error:
            waiting_input = None
            coverage.append(TeamSummaryCoverageReason(
                code="observation_timeout" if isinstance(error, TimeoutError) else "observation_failed",
                source=source, work_item_id=reading_item_id))
            items_complete = False  # a failed read may have cut the item reads short
            if stall_time_left is None:
                native_failure = native_failure or mail_request_stall_service.failure_reason(error)
            if db.in_transaction():
                await db.rollback()
        # Reuse fresh scalar rows after all asynchronous reads. Never use expired ORM attributes.
        if db.in_transaction():
            await db.rollback()
        final = await self._snapshot(db, preset_id, scope_id, provider)
        if final is None:
            return None
        try:
            attempt_reasons = await bounded(
                lambda: self._inspection_reasons(db, preset_id, visible_actions, dispatch_nonces))
        except (TimeoutError, OSError, ValueError, OperationalError):
            # A reason is optional. Keep the action and its usual detail if the lookup is unavailable.
            attempt_reasons = {}
            if db.in_transaction():
                await db.rollback()
        human_actions = []
        stopped_items = {(item.scope_id, item.issue_number) for item in items[:self.item_limit]
                         if item.dispatch_status in ATTENTION_STATUSES}
        for action in visible_actions:
            # Existing action data also contains private context and prose. Use a narrow allowlist.
            kind, state = action["kind"], action["state"]
            action_scope_id, issue_number = action["scope_id"], action["issue_number"]
            work_item_id = action.get("work_item_id")
            assessment_current = action.get("assessment_current") is True
            reason = None
            if kind == "inspect_attempt" and state in {"requested", "context_pending"} and assessment_current:
                reason = attempt_reasons.get((action_scope_id, issue_number, work_item_id, action.get("dispatch_epoch")))
            human_actions.append(TeamSummaryHumanAction(
                scope_id=action_scope_id, issue_number=issue_number, work_item_id=work_item_id,
                kind=kind, state=state, source=action.get("source") if action.get("source") in {"leader", "dispatch"}
                else "unknown", assessment_current=assessment_current,
                pull_request_number=action.get("pull_request_number"), escalation_reason=reason))
            stopped_inspection = (kind in {"inspect_attempt", "inspect_checkpoint"}
                                  and (action_scope_id, issue_number) in stopped_items)
            if state not in {"resolved", "historical"} and not stopped_inspection:
                actors.add("operator")
        changed = final != initial or hold_code() != hold
        now = datetime.now(timezone.utc)
        if changed:
            coverage.append(TeamSummaryCoverageReason(code="source_changed"))
        items_complete = items_complete and not changed and min(item_expires) > now
        current = [value for value in observations if value.remaining_work.state == "current"
            and value.remaining_work.reported_at is not None
            and -timedelta(minutes=5) <= now - _utc(value.remaining_work.reported_at) <= MAX_AGE]
        known = items_complete and len(current) == total and all(
            value.remaining_work.effort_low_minutes is not None
            and value.remaining_work.effort_high_minutes is not None
            and value.remaining_work.effort_low_minutes <= value.remaining_work.effort_high_minutes
            and value.remaining_work.confidence != "unknown" for value in current)
        confidence = min((value.remaining_work.confidence for value in current),
            key={"low": 0, "medium": 1, "high": 2}.get, default="high") if known else "unknown"
        remaining = TeamRemainingWork(state="known" if known else "unknown", unfinished_items=final[3],
            reported_items=len(current),
            effort_low_minutes=sum(value.remaining_work.effort_low_minutes for value in current) if known else None,
            effort_high_minutes=sum(value.remaining_work.effort_high_minutes for value in current) if known else None,
            confidence=confidence, reason="complete" if known else "context_changed" if changed
            else "observation_incomplete" if not items_complete else "report_unknown")
        final_team = final[0]
        supervisor = supervisor_observation(final_team, now=now, actor=SimpleNamespace(
            id=final_team.supervisor_actor_id, display_name=final_team.supervisor_display_name)
            if final_team.supervisor_actor_id is not None else None,
            quiet=await team_needs_no_supervision(db, final_team))  # unfiltered, whatever the summary's filter
        if supervisor["next_check_at"] is not None:
            boundary = supervisor["next_check_at"]
            if supervisor["status"] == "late":
                boundary += timedelta(seconds=supervisor["grace_seconds"])
            if boundary > now:
                expires.append(boundary)
        from app.services.github_team_idle_service import github_team_idle_service
        idle = []
        try:
            idle = await bounded(lambda: github_team_idle_service.observe(db, preset_id, now=now,
                coverage_complete=not coverage and min(expires) > now and waiting_input is not None and native_known,
                # The summary lists a provider refusal, but it is not a wait on someone else (#1091).
                external_wait=any(counts_as_external_wait(w.reason) for w in (waiting_input or []))
                or any(a.state not in {"resolved", "historical"} for a in human_actions)))
        except (TimeoutError, OSError, ValueError):
            if db.in_transaction():
                await db.rollback()
        # Unanswered explicit requests (#752): its own bounded, read-only view. It reuses the native
        # observation above and its fence; a changed context is not a stable observation. It never
        # adds a next actor and does not change team_idle.
        if changed:
            stall = UnansweredRequestView(complete=False, reason="fence_changed")
        else:
            mail_deadline = (stall_deadline if stall_time_left is None
                             else min(deadline, loop.time() + stall_time_left))
            stall = await mail_request_stall_service.observe(
                db, preset_id, now=now, native_states=native_states, native_ok=stall_time_left is not None,
                deadline=mail_deadline, native_fence=native_fence, native_baseline=native_baseline,
                native_failure=native_failure)
        # The unfiltered call keeps its exact arguments; a filter is passed only when set (#1142).
        campaign_filter = {"scope_id": scope_id, "provider": provider} if matcher is not None else {}
        campaign = await self.campaign.summary(db, preset_id, human_actions if not changed else (), **campaign_filter)
        unreadable = []
        for item in final[2][:self.item_limit]:
            signal = required_ci_attention.signal(item, now=now)
            if signal is not None:
                unreadable.append(TeamRequiredCIUnreadable(**signal.model_dump(),
                    work_item_id=item.id, scope_id=item.scope_id, issue_number=item.issue_number))
        reminders = await github_operator_attention_service.reminder_snapshot(
            db, [SimpleNamespace(**row._mapping) for row in final[1]], now=now)

        # The guard describes the input state at publication. Earlier fences cannot certify the
        # result after the Mail, campaign and reminder reads have yielded.
        published = final
        publication_error = None
        rechecking_native = native_changed = False
        try:
            published = await bounded(lambda: self._snapshot(db, preset_id, scope_id, provider))
            if published is not None and stall_time_left is not None:
                rechecking_native = True
                published_native = {entry[0]: entry for entry in await bounded(lambda: _team_inputs(db, preset_id, 256))}
                native_changed = published_native != native_fence
        except (TimeoutError, OSError, ValueError, OperationalError) as error:
            publication_error = error
            if db.in_transaction():
                await db.rollback()
        if published is None:
            return None
        published_hold = hold_code()
        late_change = published != final or published_hold != hold
        if late_change and not changed:
            coverage.append(TeamSummaryCoverageReason(code="source_changed"))
        if native_changed and not any(reason.code == "fence_changed" and reason.source == "native" for reason in coverage):
            coverage.append(TeamSummaryCoverageReason(code="fence_changed", source="native"))
        if publication_error is not None:
            code = "observation_timeout" if isinstance(publication_error, TimeoutError) else "observation_failed"
            if not any(reason.code == code for reason in coverage):
                coverage.append(TeamSummaryCoverageReason(code=code, source="native" if rechecking_native else None))
        native_unverified = native_changed or (publication_error is not None and rechecking_native)
        if native_unverified:
            waiting_input = None
        if late_change or (publication_error is not None and not rechecking_native):
            changed = True
            remaining = TeamRemainingWork(state="unknown", unfinished_items=published[3],
                reported_items=len(current), confidence="unknown", reason="context_changed")
        if late_change or native_changed:
            stall = UnansweredRequestView(complete=False, reason="fence_changed")
        elif publication_error is not None and (rechecking_native or stall.complete):
            # A partial result can still carry rows that depend on the native check.
            stall = UnansweredRequestView(complete=False, reason=mail_request_stall_service.failure_reason(publication_error))
        if published_hold:
            attention.insert(0, TeamSummaryAttention(code=published_hold))
            actors.add("operator")
        if not changed and not native_unverified:
            for entry in idle:
                attention.append(TeamSummaryAttention(code="team_idle", **entry))
                actors.add("leader")
        for entry in stall.items:
            attention.append(TeamSummaryAttention(code="unanswered_request", request_id=entry.request_id,
                requester=entry.requester, recipient=entry.recipient, unanswered_seconds=entry.unanswered_seconds))
        if not stall.complete:
            attention.append(TeamSummaryAttention(code="unanswered_request_incomplete",
                coverage_reason=stall.reason, omitted_count=stall.omitted_count or None))
        supervisor["unanswered_requests"] = stall

        valid_until = min(expires)
        if valid_until <= datetime.now(timezone.utc):
            coverage.append(TeamSummaryCoverageReason(code="window_expired"))
        complete = not coverage
        return AgentTeamSummaryResponse(preset_id=preset_id, description=final_team.description,
            parent_issue_url=final_team.parent_issue_url, checked_at=now, valid_until=valid_until,
            coverage_complete=complete, coverage_reason=coverage, hold_code=published_hold, attention=attention,
            human_actions=human_actions,
            required_ci_unreadable=unreadable, reminder_attention=reminders["rows"],
            reminder_attention_complete=reminders["complete"],
            supervisor_observation=supervisor, waiting_input=waiting_input if not changed else None,
            remaining_work=remaining, next_actors=[actor for actor in _ACTORS if actor in actors],
            next_actor_coverage_complete=complete and len(observations) == total, campaign=campaign)

    async def for_work_item(self, db, preset_id, work_item_id):
        """Keep the item's and unbound blockers over the same team observation."""
        tracked_items = select(GithubWorkItem.id, GithubWorkItem.scope_id, GithubWorkItem.issue_number).join(
            TeamGithubScope, TeamGithubScope.id == GithubWorkItem.scope_id).where(
                TeamGithubScope.preset_id == preset_id)
        if (await db.execute(tracked_items.where(GithubWorkItem.id == work_item_id))).one_or_none() is None:
            return None
        result = await self.summary(db, preset_id)
        if result is None:
            return None
        # A Leader-reported action can carry a tracked issue's identity before it carries the work item id.
        unbound = {(row.scope_id, row.issue_number) for row in (*result.human_actions, *result.attention)
                   if row.work_item_id is None and row.scope_id is not None and row.issue_number is not None}
        bindings = {}
        if unbound:
            rows = await db.execute(tracked_items.where(tuple_(GithubWorkItem.scope_id, GithubWorkItem.issue_number)
                                                       .in_(unbound)))
            bindings = {(row.scope_id, row.issue_number): row.id for row in rows}

        def relevant(row):
            return (row.work_item_id or bindings.get((row.scope_id, row.issue_number))) in (None, work_item_id)

        updates = {"attention": [row for row in result.attention if relevant(row)],
                   "human_actions": [row for row in result.human_actions if relevant(row)]}
        # Binding an issue to a work item may await after the team observation. Preserve its timestamp
        # and fail closed if the coverage expired or the hold changed while the bindings were read.
        current_hold = hold_code()
        hold_changed = current_hold != result.hold_code
        window_expired = _utc(result.valid_until) <= datetime.now(timezone.utc)
        if hold_changed or window_expired:
            coverage = list(result.coverage_reason)
            existing_codes = {reason.code for reason in coverage}
            if hold_changed and "source_changed" not in existing_codes:
                coverage.append(TeamSummaryCoverageReason(code="source_changed"))
            if window_expired and "window_expired" not in existing_codes:
                coverage.append(TeamSummaryCoverageReason(code="window_expired"))
            updates.update(coverage_complete=False, coverage_reason=coverage,
                           next_actor_coverage_complete=False, waiting_input=None)
            if current_hold is not None:
                updates["hold_code"] = current_hold
        return result.model_copy(update=updates)

    async def needs_you(self, db, preset_id, *, scope_id=None, provider=None):
        """The summary narrowed to a repository and an assigned harness (#1141, #1142).

        The work-based parts (unfinished items, remaining work, next actors, the campaign, the human actions)
        are narrowed where they are built. The lists that come from team-wide services (reminders, waiting
        members, the supervisor row, the stored attention rows) are trimmed here by the same rule.
        """
        if scope_id is None and provider is None:
            return await self.summary(db, preset_id)
        match = await self._matcher(db, preset_id, scope_id, provider)
        result = await self.summary(db, preset_id, scope_id=scope_id, provider=provider)
        if result is None:
            return None
        visible = match.visible
        # Work reasons already use the filtered snapshot. A hidden native slot can still affect
        # team-wide completeness, so retain its fixed reason without any slot details.
        coverage = [row for row in result.coverage_reason if row.slot_id is None or visible(None, slot_id=row.slot_id)]
        if not coverage and result.coverage_reason:
            coverage = [TeamSummaryCoverageReason(code=result.coverage_reason[0].code)]
        update = {
            "human_actions": [row for row in result.human_actions if visible(row.scope_id, row.work_item_id)],
            "attention": [row for row in result.attention if visible(row.scope_id, row.work_item_id)],
            "required_ci_unreadable": [row for row in result.required_ci_unreadable
                                       if visible(row.scope_id, row.work_item_id)],
            "reminder_attention": [row for row in result.reminder_attention
                                   if visible(row.scope_id, row.work_item_id, row.owner_slot_id)],
            "coverage_reason": coverage,
        }
        if result.waiting_input is not None:
            update["waiting_input"] = [row for row in result.waiting_input if visible(None, slot_id=row.slot_id)]
        observation = result.supervisor_observation
        if observation is not None and observation.attention and not visible(None):
            update["supervisor_observation"] = observation.model_copy(update={"attention": None})
        return result.model_copy(update=update)


agent_team_summary_service = AgentTeamSummaryService()

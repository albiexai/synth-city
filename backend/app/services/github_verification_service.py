"""GitHub PR verification and merge loop for autonomous dispatch."""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import subprocess
from collections.abc import Mapping
from datetime import datetime, timedelta

import httpx
from sqlalchemy import JSON, and_, exists, func, inspect, or_, select, update
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import (
    AgentTeamPreset,
    AgentTeamSlot,
    GithubApprovalRequest,
    GithubAttemptScopeRevision,
    GithubHeadReview,
    GithubOwnerObservationPause,
    GithubWorkItem,
    GithubWorkspace,
    MailMessage,
    MailTeamMember,
    TeamGithubScope,
)
from app.services.github_app_auth_service import github_app_auth_service
from app.services.agent_mail_service import agent_mail_service
from app.services.accepted_source_imports import matching_imported_paths, source_import_claim_conditions
from app.services.github_approval_service import (
    IMPLEMENTATION_COMPLETION_ACTIONS,
    github_approval_service,
)
from app.services.github_client import (
    GithubClient,
    GithubClientResponseError,
    GithubTreeEntry,
    github_client,
)
from app.services.github_check_history_service import observe_tracked_checks
from app.services.github_check_observation import GithubCheckObservationError
from app.services import github_merge_attempt_service as merge_attempts
from app.services import required_ci_attention
from app.services.github_ci_watch_service import github_ci_watch_service
from app.services.github_terminal_revision_service import (
    TerminalRevisionContextChanged, complete_current_revision, lock_terminal_transition,
)
from app.services.github_dispatch_service import github_dispatch_service, restored_escalation_reason
from app.services.github_rate_limit_service import GithubRateLimited
from app.services.github_recovery_gate import (
    GithubRecoveryOnlyAttempt,
    configured_recovery_only_attempt,
)
from app.services.github_workspace_service import github_workspace_service
from app.services.team_communication_policy import HUMAN_REVIEW_SUMMARY_GUIDANCE
from app.services.session_recovery import recover_session

from app.services.factory_delivery_policy import (
    claim_policy_context, effective_policy, policy_context, policy_context_conditions,
    required_check_blockers, scope_policy,
)

_SUCCESS_CONCLUSIONS = {"success", "neutral", "skipped"}
_INCONCLUSIVE_CONCLUSIONS = {"cancelled", "timed_out"}
_STATUS_SUCCESS_STATES = {"success"}
_STATUS_FAILURE_STATES = {"failure", "error"}
_TRANSIENT_MERGE_STATES = {"unstable", "blocked"}
_MERGE_CHECK_WAIT_LIMIT = timedelta(minutes=60)
_HUMAN_MERGE_NOTE_PREFIXES = (
    "Auto-merge blocked",
    "Auto-merge budget exhausted",
    "Auto-merge failed",
    "Auto-merge retry budget exhausted",
)
# The two human-merge fallbacks whose cause can clear without a person (#1037).
_CAP_FALLBACK_PREFIX = "Auto-merge budget exhausted"
_STALL_FALLBACK_PREFIX = "Auto-merge blocked: GitHub checks stalled"
_MERGE_TRANSIENT_STATUS_CODES = {405, 409, 422}
# Escalations a late PR legitimately resolves: the agent said it was stuck, or Albiex Synth City
# inferred it from a timer. Anything not listed stays rejected by default.
_PR_OPENED_RECOVERABLE_ESCALATIONS = frozenset(
    {
        "plan_blocked",
        "owner_idle_timeout",
        "owner_offline",
        "leader_offline",
        "leader_ack_timeout",
        "brief_unread",
    }
)

logger = logging.getLogger(__name__)


class ContinuationCompletionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# What a merged-PR settlement must find unchanged under the terminal writer: the attempt, the scope's target and
# access, and (compared whole) the exact submitted revision.
_SETTLEMENT_ITEM_IDENTITY = (
    "id", "scope_id", "dispatch_status", "dispatch_nonce", "launch_id", "owner_slot_id", "active_scope_revision",
    "attempt_phase", "pr_number", "dispatch_head_ref", "dispatch_base_ref", "handoff_state",
)
_SETTLEMENT_SCOPE_IDENTITY = ("id", "preset_id", "repo_owner", "repo_name", "github_auth_mode", "enabled")
# What the superseded-revision merge (#972) must find unchanged at its last local read before the merge call.
_MERGE_FENCE_ITEM = (
    "id", "scope_id", "dispatch_status", "dispatch_nonce", "launch_id", "owner_slot_id", "active_scope_revision",
    "attempt_phase", "pr_number", "dispatch_head_ref", "dispatch_base_ref", "last_verified_sha",
    "approval_round_count", "ack_approval_round", "ack_enforcement_epoch", "ack_approver_member_id",
)
_MERGE_FENCE_NOTE_IDENTITY = (
    "id", "scope_id", "dispatch_status", "dispatch_nonce", "owner_slot_id", "active_scope_revision", "pr_number",
)
_MERGE_FENCE_SCOPE = ("id", "preset_id", "repo_owner", "repo_name", "github_auth_mode", "delivery_policy_revision")


def _latest_member(slot_id):
    """The id of the member currently occupying a slot: the latest by update time, then id (as the slot lookups do)."""
    other = aliased(MailTeamMember)
    return (select(other.id).where(other.team_slot_id == slot_id)
            .order_by(other.updated_at.desc(), other.id.desc()).limit(1).scalar_subquery())


def _same_value(column, value):
    """A null-safe equality; a JSON column holds a missing value as SQL NULL or as a JSON null."""
    if value is None:
        return or_(column.is_(None), column == JSON.NULL) if isinstance(column.type, JSON) else column.is_(None)
    return column == value


class GithubVerificationService:
    def __init__(self) -> None:
        self._pr_ready_locks: dict[int, asyncio.Lock] = {}
        self._escalated_merge_cursors: dict[int, int] = {}
        # Inconsistent continuation states already logged in this process, so a stuck item logs once per state.
        self._logged_inconsistent: set[tuple] = set()
        # The items verification skipped in the current poll: read by the scheduler for #941's skipped-item detector.
        self.skipped_item_ids: set[int] = set()

    @staticmethod
    def _same_tree_entry(
        left: Mapping[str, GithubTreeEntry], right: Mapping[str, GithubTreeEntry], path: str,
    ) -> bool:
        a, b = left.get(path), right.get(path)
        if a is None or b is None:
            return a is None and b is None
        return (a.mode, a.object_type, a.sha) == (b.mode, b.object_type, b.sha)

    async def _base_side_paths(
        self,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        revision: GithubAttemptScopeRevision,
        client: GithubClient,
        token: str,
        pull: dict,
        head_sha: str,
        baseline_tree: Mapping[str, GithubTreeEntry],
        current_tree: Mapping[str, GithubTreeEntry],
        paths: set[str],
    ) -> set[str]:
        """The given paths that only a base update changed, so they are not scope growth (#1030).

        A path is base-side when the PR's own diff leaves it untouched both at the revision's baseline and at the
        head: each matches its merge base with the PR's current base tip. An edit of that path in the PR, before or
        after the baseline, is still the PR's own change. Any missing, foreign or unreadable evidence is inconclusive.
        """
        base = pull.get("base")
        base_ref = base.get("ref") if isinstance(base, dict) else None
        base_sha = base.get("sha") if isinstance(base, dict) else None
        try:
            expected_ref = await self.normalize_base_ref(scope, client, token=token, base_ref=item.dispatch_base_ref)
            if base_ref != expected_ref:
                raise ValueError("the PR base is not the item's dispatch base")
            merge_trees = []
            for commit_sha in (revision.baseline_head_sha, head_sha):
                merge_base = await client.get_merge_base(
                    scope.repo_owner, scope.repo_name, base_sha, commit_sha, token=token)
                snapshot = await client.get_commit_snapshot(
                    scope.repo_owner, scope.repo_name, merge_base, token=token)
                merge_trees.append(await client.get_recursive_tree(
                    scope.repo_owner, scope.repo_name, snapshot.tree_sha, token=token))
        except (ValueError, GithubClientResponseError, httpx.HTTPError) as exc:
            raise ContinuationCompletionError("continuation_diff_inconclusive") from exc
        at_baseline, at_head = merge_trees
        return {
            path for path in paths
            if self._same_tree_entry(baseline_tree, at_baseline, path)
            and self._same_tree_entry(current_tree, at_head, path)
        }

    @staticmethod
    def _changed_tree_paths(
        baseline: Mapping[str, GithubTreeEntry],
        current: Mapping[str, GithubTreeEntry],
    ) -> set[str]:
        def snapshot(
            entries: Mapping[str, GithubTreeEntry],
        ) -> dict[str, tuple[str, str, str]]:
            return {
                entry.path: (entry.mode, entry.object_type, entry.sha)
                for entry in entries.values()
                if entry.object_type != "tree"
            }

        baseline_paths = snapshot(baseline)
        current_paths = snapshot(current)
        return {
            path
            for path in baseline_paths.keys() | current_paths.keys()
            if baseline_paths.get(path) != current_paths.get(path)
        }

    async def submit_continuation_completion(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        scope: TeamGithubScope,
        *,
        authenticated_owner_member_id: int,
        authenticated_owner_slot_id: int,
        revision_number: int,
        dispatch_nonce: str,
        current_head_sha: str,
        result_summary: str,
        evidence: dict,
        lease_token: str,
        client: GithubClient | None = None,
    ) -> bool:
        client = client or github_client
        await db.refresh(item)
        if item.scope_id != scope.id:
            raise ContinuationCompletionError("scope_mismatch")
        if item.dispatch_nonce != dispatch_nonce:
            raise ContinuationCompletionError("stale_nonce")
        if item.owner_slot_id != authenticated_owner_slot_id:
            raise ContinuationCompletionError("not_item_owner")
        owner, _leader = await agent_mail_service._dispatch_participants(db, item)
        if owner is None or owner.id != authenticated_owner_member_id:
            raise ContinuationCompletionError("not_item_owner")
        revision = (
            await db.execute(
                select(GithubAttemptScopeRevision).where(
                    GithubAttemptScopeRevision.work_item_id == item.id,
                    GithubAttemptScopeRevision.dispatch_nonce == dispatch_nonce,
                    GithubAttemptScopeRevision.revision == revision_number,
                )
            )
        ).scalar_one_or_none()
        if revision is None:
            raise ContinuationCompletionError("scope_revision_not_found")
        await db.refresh(revision)
        if (
            item.active_scope_revision != revision.revision
            or revision.owner_slot_id != authenticated_owner_slot_id
            or revision.owner_member_id != authenticated_owner_member_id
            or revision.phase != "implementation"
        ):
            raise ContinuationCompletionError("stale_scope_revision")
        if revision.status not in {"active", "submitted"}:
            raise ContinuationCompletionError("scope_revision_not_active")
        if not IMPLEMENTATION_COMPLETION_ACTIONS.issubset(
            set(revision.allowed_actions)
        ):
            raise ContinuationCompletionError("continuation_actions_missing")
        workspace = await github_workspace_service.get_leased_workspace(db, item.id)
        if (
            workspace is None
            or workspace.id != revision.expected_workspace_id
            or workspace.lease_token is None
        ):
            raise ContinuationCompletionError("workspace_lease_changed")
        if not hmac.compare_digest(workspace.lease_token, lease_token):
            raise ContinuationCompletionError("lease_token_mismatch")
        if not github_approval_service.lease_token_matches(
            lease_token,
            revision.expected_lease_token_hash,
        ):
            raise ContinuationCompletionError("workspace_lease_changed")
        if item.pr_number is None:
            raise ContinuationCompletionError("continuation_pr_required")
        token = await github_approval_service.github_read_token(scope)
        pull = await client.get_pull(
            scope.repo_owner,
            scope.repo_name,
            item.pr_number,
            token=token,
        )
        head = pull.get("head")
        github_head_sha = head.get("sha") if isinstance(head, dict) else None
        if pull.get("state") != "open":
            raise ContinuationCompletionError("continuation_pr_not_open")
        if github_head_sha != current_head_sha:
            raise ContinuationCompletionError("continuation_head_changed")
        current_snapshot = await client.get_commit_snapshot(
            scope.repo_owner,
            scope.repo_name,
            current_head_sha,
            token=token,
        )
        try:
            baseline_tree = await client.get_recursive_tree(
                scope.repo_owner,
                scope.repo_name,
                revision.baseline_tree_sha,
                token=token,
            )
            current_tree = await client.get_recursive_tree(
                scope.repo_owner,
                scope.repo_name,
                current_snapshot.tree_sha,
                token=token,
            )
        except GithubClientResponseError as exc:
            raise ContinuationCompletionError(
                "continuation_diff_inconclusive"
            ) from exc
        changed_paths = self._changed_tree_paths(baseline_tree, current_tree)
        outside_paths = {
            path for path in changed_paths
            if not github_approval_service.path_is_allowed(path, revision.allowed_paths)
        }
        if outside_paths:
            outside_paths -= await self._base_side_paths(
                scope, item, revision, client, token, pull, current_head_sha,
                baseline_tree, current_tree, outside_paths,
            )
        import_conditions = []
        if outside_paths:
            import_conditions = source_import_claim_conditions(item, revision, workspace, scope)
            try:
                async with asyncio.timeout(45):
                    imported = await matching_imported_paths(
                        db, item, revision, workspace, scope, current_tree,
                        outside_paths, current_head_sha, client=client, token=token,
                    )
            except (ValueError, TimeoutError, GithubClientResponseError, httpx.HTTPError) as exc:
                raise ContinuationCompletionError("continuation_import_inconclusive") from exc
            outside_paths -= imported
        if outside_paths:
            raise ContinuationCompletionError("continuation_paths_out_of_scope")
        if (
            revision.status == "submitted"
            and item.dispatch_status == "verifying"
            and revision.submitted_head_sha == current_head_sha
            and revision.result_summary == result_summary
            and revision.evidence == evidence
        ):
            return False
        if revision.status != "active" or item.dispatch_status != "dispatched":
            raise ContinuationCompletionError("stale_continuation_context")

        now = datetime.utcnow()
        item_result = await db.execute(
            update(GithubWorkItem)
            .where(
                GithubWorkItem.id == item.id,
                GithubWorkItem.dispatch_status == "dispatched",
                GithubWorkItem.dispatch_nonce == dispatch_nonce,
                GithubWorkItem.owner_slot_id == authenticated_owner_slot_id,
                GithubWorkItem.active_scope_revision == revision.revision,
                GithubWorkItem.pr_number.is_not(None),
                *import_conditions,
                exists(
                    select(GithubWorkspace.id).where(
                        GithubWorkspace.id == workspace.id,
                        GithubWorkspace.leased_item_id == item.id,
                        GithubWorkspace.lease_token == lease_token,
                    )
                ),
            )
            .values(dispatch_status="verifying", verification_head_sha=current_head_sha,
                    verification_started_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        revision_result = await db.execute(
            update(GithubAttemptScopeRevision)
            .where(
                GithubAttemptScopeRevision.id == revision.id,
                GithubAttemptScopeRevision.status == "active",
                GithubAttemptScopeRevision.dispatch_nonce == dispatch_nonce,
                GithubAttemptScopeRevision.owner_slot_id
                == authenticated_owner_slot_id,
                GithubAttemptScopeRevision.owner_member_id
                == authenticated_owner_member_id,
                GithubAttemptScopeRevision.expected_workspace_id == workspace.id,
            )
            .values(
                status="submitted",
                result_summary=result_summary,
                evidence=evidence,
                submitted_head_sha=current_head_sha,
                submitted_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if item_result.rowcount != 1 or revision_result.rowcount != 1:
            await db.rollback()
            raise ContinuationCompletionError("stale_continuation_context")
        await db.commit()
        await db.refresh(item)
        return True

    async def submit_diagnostic_completion(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        scope: TeamGithubScope,
        *,
        authenticated_owner_member_id: int,
        authenticated_owner_slot_id: int,
        revision_number: int,
        dispatch_nonce: str,
        current_head_sha: str,
        result_summary: str,
        evidence: dict,
        lease_token: str,
        client: GithubClient | None = None,
    ) -> bool:
        client = client or github_client
        await db.refresh(item)
        if item.scope_id != scope.id:
            raise ContinuationCompletionError("scope_mismatch")
        if item.dispatch_nonce != dispatch_nonce:
            raise ContinuationCompletionError("stale_nonce")
        if item.owner_slot_id != authenticated_owner_slot_id:
            raise ContinuationCompletionError("not_item_owner")
        owner, _leader = await agent_mail_service._dispatch_participants(db, item)
        if owner is None or owner.id != authenticated_owner_member_id:
            raise ContinuationCompletionError("not_item_owner")
        revision = (
            await db.execute(
                select(GithubAttemptScopeRevision).where(
                    GithubAttemptScopeRevision.work_item_id == item.id,
                    GithubAttemptScopeRevision.dispatch_nonce == dispatch_nonce,
                    GithubAttemptScopeRevision.revision == revision_number,
                )
            )
        ).scalar_one_or_none()
        if revision is None:
            raise ContinuationCompletionError("scope_revision_not_found")
        await db.refresh(revision)
        if (
            revision.owner_slot_id != authenticated_owner_slot_id
            or revision.owner_member_id != authenticated_owner_member_id
            or revision.phase != "diagnostic"
        ):
            raise ContinuationCompletionError("stale_scope_revision")
        workspace = await github_workspace_service.get_leased_workspace(db, item.id)
        if (
            workspace is None
            or workspace.id != revision.expected_workspace_id
            or workspace.lease_token is None
        ):
            raise ContinuationCompletionError("workspace_lease_changed")
        if not hmac.compare_digest(workspace.lease_token, lease_token):
            raise ContinuationCompletionError("lease_token_mismatch")
        if not github_approval_service.lease_token_matches(
            lease_token,
            revision.expected_lease_token_hash,
        ):
            raise ContinuationCompletionError("workspace_lease_changed")
        if item.pr_number is None:
            raise ContinuationCompletionError("continuation_pr_required")
        token = await github_approval_service.github_read_token(scope)
        pull = await client.get_pull(
            scope.repo_owner,
            scope.repo_name,
            item.pr_number,
            token=token,
        )
        head = pull.get("head")
        github_head_sha = head.get("sha") if isinstance(head, dict) else None
        if pull.get("state") != "open":
            raise ContinuationCompletionError("continuation_pr_not_open")
        if github_head_sha != current_head_sha:
            raise ContinuationCompletionError("continuation_head_changed")
        snapshot = await client.get_commit_snapshot(
            scope.repo_owner,
            scope.repo_name,
            current_head_sha,
            token=token,
        )
        if snapshot.tree_sha != revision.baseline_tree_sha:
            raise ContinuationCompletionError("diagnostic_tree_not_restored")
        confirmed_pull = await client.get_pull(
            scope.repo_owner,
            scope.repo_name,
            item.pr_number,
            token=token,
        )
        confirmed_head = confirmed_pull.get("head")
        confirmed_head_sha = (
            confirmed_head.get("sha") if isinstance(confirmed_head, dict) else None
        )
        if (
            confirmed_pull.get("state") != "open"
            or confirmed_head_sha != current_head_sha
        ):
            raise ContinuationCompletionError("continuation_head_changed")

        envelope = dict(revision.evidence) if isinstance(revision.evidence, dict) else {}
        envelope["version"] = 1
        envelope["diagnostic_completion"] = dict(evidence)
        if (
            revision.status == "completed"
            and item.dispatch_status == "escalated"
            and item.attempt_phase == "implementation"
            and item.active_scope_revision == 0
            and revision.result_summary == result_summary
            and revision.evidence == envelope
        ):
            return False
        completion_from_active = (
            revision.status == "active"
            and item.dispatch_status == "dispatched"
        )
        completion_from_exhaustion = (
            revision.status == "exhausted"
            and item.dispatch_status == "escalated"
            and item.escalation_reason == "continuation_budget_exhausted"
        )
        if not (
            (completion_from_active or completion_from_exhaustion)
            and item.attempt_phase == "diagnostic"
            and item.active_scope_revision == revision.revision
        ):
            raise ContinuationCompletionError("stale_continuation_context")

        now = datetime.utcnow()
        expected_item_status = (
            "escalated" if completion_from_exhaustion else "dispatched"
        )
        expected_revision_status = (
            "exhausted" if completion_from_exhaustion else "active"
        )
        item_conditions = [
            GithubWorkItem.id == item.id,
            GithubWorkItem.dispatch_status == expected_item_status,
            GithubWorkItem.dispatch_nonce == dispatch_nonce,
            GithubWorkItem.owner_slot_id == authenticated_owner_slot_id,
            GithubWorkItem.active_scope_revision == revision.revision,
            GithubWorkItem.attempt_phase == "diagnostic",
            GithubWorkItem.pr_number.is_not(None),
            exists(
                select(GithubWorkspace.id).where(
                    GithubWorkspace.id == workspace.id,
                    GithubWorkspace.leased_item_id == item.id,
                    GithubWorkspace.lease_token == lease_token,
                )
            ),
        ]
        if completion_from_exhaustion:
            item_conditions.append(
                GithubWorkItem.escalation_reason
                == "continuation_budget_exhausted"
            )
        item_result = await db.execute(
            update(GithubWorkItem)
            .where(*item_conditions)
            .values(
                dispatch_status="escalated",
                escalation_reason=restored_escalation_reason(revision),
                status_note=(
                    "Diagnostic restoration verified. Propose the smallest bounded "
                    "implementation continuation supported by the evidence."
                ),
                active_scope_revision=0,
                attempt_phase="implementation",
                continuation_nudged_at=None,
                continuation_activated_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        revision_result = await db.execute(
            update(GithubAttemptScopeRevision)
            .where(
                GithubAttemptScopeRevision.id == revision.id,
                GithubAttemptScopeRevision.status == expected_revision_status,
                GithubAttemptScopeRevision.phase == "diagnostic",
                GithubAttemptScopeRevision.dispatch_nonce == dispatch_nonce,
                GithubAttemptScopeRevision.owner_slot_id
                == authenticated_owner_slot_id,
                GithubAttemptScopeRevision.owner_member_id
                == authenticated_owner_member_id,
                GithubAttemptScopeRevision.expected_workspace_id == workspace.id,
            )
            .values(
                status="completed",
                result_summary=result_summary,
                evidence=envelope,
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if item_result.rowcount != 1 or revision_result.rowcount != 1:
            await db.rollback()
            raise ContinuationCompletionError("stale_continuation_context")
        await self._record_revision_outcome(
            db, item, revision, "completed",
            source="github_verification_service.submit_diagnostic_completion")
        await db.commit()
        await db.refresh(item)
        await github_dispatch_service.notify_owner(
            db,
            item,
            subject="Diagnostic restoration verified",
            body_markdown=(
                f"Diagnostic revision {revision.revision} restored the baseline tree. "
                "Propose the smallest bounded implementation continuation supported "
                "by the recorded evidence."
            ),
            payload={
                "kind": "github_dispatch_diagnostic_completed",
                "work_item_id": item.id,
                "pr_number": item.pr_number,
                "scope_revision": revision.revision,
            },
            delivery_key=f"github-diagnostic:{revision.id}:completed",
        )
        return True

    async def normalize_base_ref(
        self,
        scope: TeamGithubScope,
        client: GithubClient,
        *,
        token: str | None,
        base_ref: str | None = None,
    ) -> str:
        base_ref = scope.base_ref if base_ref is None else base_ref
        if base_ref == "origin/HEAD":
            repository = await client.get_repository(
                scope.repo_owner, scope.repo_name, token=token
            )
            candidate = repository.get("default_branch")
            if not isinstance(candidate, str) or not candidate:
                raise ValueError("GitHub repository has no default branch")
        elif base_ref.startswith("origin/"):
            candidate = base_ref.removeprefix("origin/")
        else:
            candidate = base_ref
        if not candidate or candidate == "HEAD" or candidate.startswith("refs/"):
            raise ValueError(f"Unsupported GitHub base ref: {base_ref}")
        try:
            result = subprocess.run(
                ["git", "check-ref-format", "--branch", candidate],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("Unable to validate GitHub base branch") from exc
        if result.returncode != 0:
            raise ValueError(f"Invalid GitHub base branch: {candidate}")
        return candidate

    @staticmethod
    def pull_title(item: GithubWorkItem, owner_slot: AgentTeamSlot) -> str:
        return (
            f"[{owner_slot.display_name}] {item.issue_title} "
            f"(#{item.issue_number})"
        )

    @staticmethod
    def pull_body(item: GithubWorkItem, *, head_ref: str) -> str:
        return (
            f"Closes #{item.issue_number}\n\n"
            f"{item.issue_title}\n\n"
            "---\n"
            "Albiex Synth City provenance\n"
            f"- Work item: {item.id}\n"
            f"- Owner slot: {item.owner_slot_id}\n"
            f"- Dispatch nonce: {item.dispatch_nonce}\n"
            f"- Head ref: {head_ref}"
        )

    @staticmethod
    def pull_is_draft(item: GithubWorkItem) -> bool:
        return item.issue_type != "design"

    @staticmethod
    def _classify_pull(pull: dict) -> str | None:
        """Classify fields present in both list and single-pull responses."""
        if "merged_at" not in pull:
            return None
        state = pull.get("state")
        merged_at = pull["merged_at"]
        if state == "open" and merged_at is None:
            return "open"
        if state == "closed" and merged_at is not None:
            return "merged"
        if state == "closed" and merged_at is None:
            return "closed_unmerged"
        return None

    @staticmethod
    def _verify_pull_identity(
        pull: dict,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        *,
        expected_base: str,
        verify_author: bool = True,
    ) -> None:
        expected_repo = f"{scope.repo_owner}/{scope.repo_name}"
        base_repo = ((pull.get("base") or {}).get("repo") or {}).get("full_name")
        head = pull.get("head") or {}
        head_repo = (head.get("repo") or {}).get("full_name")
        if base_repo != expected_repo or head_repo != expected_repo:
            raise ValueError(
                f"PR repository does not match {expected_repo}"
            )
        if item.dispatch_head_ref is None or head.get("ref") != item.dispatch_head_ref:
            raise ValueError("PR head does not match the prepared dispatch head")
        if (pull.get("base") or {}).get("ref") != expected_base:
            raise ValueError("PR base does not match the prepared dispatch base")
        if verify_author and scope.github_auth_mode == "app":
            if not settings.github_app_bot_login:
                raise ValueError("app_mode_bot_login_unset")
            if (pull.get("user") or {}).get("login") != settings.github_app_bot_login:
                raise ValueError("PR author does not match the configured GitHub App bot")

    async def report_pr_opened(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        scope: TeamGithubScope,
        pr_number: int,
        client: GithubClient,
    ) -> None:
        recoverable = (
            item.dispatch_status == "escalated"
            and item.escalation_reason in _PR_OPENED_RECOVERABLE_ESCALATIONS
        )
        if item.dispatch_status != "dispatched" and not recoverable:
            raise ValueError(
                f"pr_opened is only valid for dispatched work items, or escalated "
                f"items with a recoverable reason; current status is "
                f"{item.dispatch_status} ({item.escalation_reason})"
            )
        if item.dispatch_base_ref is None:
            raise ValueError("prepared dispatch base is missing")
        expected_base = await self.normalize_base_ref(
            scope,
            client,
            token=None,
            base_ref=item.dispatch_base_ref,
        )
        pull = await client.get_pull(scope.repo_owner, scope.repo_name, pr_number)
        self._verify_pull_identity(
            pull,
            scope,
            item,
            expected_base=expected_base,
        )
        verdict = self._classify_pull(pull)
        if verdict is None:
            raise ValueError("GitHub returned a pull request state Albiex Synth City cannot classify")
        if verdict == "closed_unmerged":
            await github_dispatch_service.escalate_without_notification(
                db,
                item,
                "pr_closed_unmerged",
                f"PR #{pr_number} was closed without being merged.",
            )
            return
        if recoverable:
            item.escalation_reason = None
            item.retry_requested_at = None
        await self._record_selected_pull(db, scope, item, pull, verdict)

    async def report_pr_ready(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        scope: TeamGithubScope,
        head_ref: str,
        lease_token: str,
        client: GithubClient,
    ) -> int:
        lock = self._pr_ready_locks.setdefault(item.id, asyncio.Lock())
        async with lock:
            item = (
                await db.execute(
                    select(GithubWorkItem)
                    .where(GithubWorkItem.id == item.id)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            workspace = (
                await db.execute(
                    select(GithubWorkspace).where(
                        GithubWorkspace.scope_id == scope.id,
                        GithubWorkspace.leased_item_id == item.id,
                        GithubWorkspace.lease_token == lease_token,
                    )
                )
            ).scalar_one_or_none()
            if workspace is None:
                raise ValueError("workspace_lease_changed")
            if item.dispatch_head_ref is None:
                raise ValueError("prepared dispatch head is missing")
            if item.dispatch_base_ref is None:
                raise ValueError("prepared dispatch base is missing")
            if head_ref != item.dispatch_head_ref:
                raise ValueError("reported head does not match the prepared dispatch head")
            if item.pr_number is not None:
                return int(item.pr_number)
            if item.dispatch_status != "dispatched":
                raise ValueError(
                    f"pr_ready is only valid for dispatched work items; current status is "
                    f"{item.dispatch_status}"
                )
            if scope.github_auth_mode != "app":
                raise ValueError("pr_ready requires GitHub App authentication")
            if scope.github_app_installation_id is None:
                raise ValueError("app_installation_id_missing")
            if not settings.github_app_bot_login:
                raise ValueError("app_mode_bot_login_unset")

            github_app_auth_service.require_configuration(require_bot_login=True)
            token = await github_app_auth_service.mint_repository_token(
                scope.github_app_installation_id,
                scope.repo_owner,
                scope.repo_name,
                purpose="pull_request",
                cache_subject="backend",
            )

            remote_ref = await client.get_ref(
                scope.repo_owner,
                scope.repo_name,
                head_ref,
                token=token,
            )
            if remote_ref is None:
                raise ValueError("remote_head_not_found")
            if remote_ref.get("ref") != f"refs/heads/{head_ref}":
                raise ValueError("remote_head_mismatch")

            base = await self.normalize_base_ref(
                scope,
                client,
                token=token,
                base_ref=item.dispatch_base_ref,
            )
            pulls = await self._list_attempt_pulls(
                scope,
                client,
                head_ref=head_ref,
                base=base,
                token=token,
            )
            selected = await self._reconcile_attempt_pulls(
                db,
                scope,
                item,
                pulls,
                expected_base=base,
                verify_author=True,
            )
            if selected is not None:
                return selected

            owner_slot = await db.get(AgentTeamSlot, item.owner_slot_id)
            if owner_slot is None:
                raise ValueError("owner_slot_missing")
            try:
                created = await client.create_pull(
                    scope.repo_owner,
                    scope.repo_name,
                    title=self.pull_title(item, owner_slot),
                    head=head_ref,
                    base=base,
                    body=self.pull_body(item, head_ref=head_ref),
                    draft=self.pull_is_draft(item),
                    token=token,
                )
            except httpx.TimeoutException:
                created = None
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 422:
                    raise
                created = None

            if created is None:
                pulls = await self._list_attempt_pulls(
                    scope,
                    client,
                    head_ref=head_ref,
                    base=base,
                    token=token,
                )
                selected = await self._reconcile_attempt_pulls(
                    db,
                    scope,
                    item,
                    pulls,
                    expected_base=base,
                    verify_author=True,
                )
                if selected is None:
                    raise ValueError("pull_creation_outcome_unresolved")
                return selected

            selected = await self._reconcile_attempt_pulls(
                db,
                scope,
                item,
                [created],
                expected_base=base,
                verify_author=False,
            )
            if selected is None:
                raise ValueError("pull_creation_returned_no_pull")
            return selected

    async def _list_attempt_pulls(
        self,
        scope: TeamGithubScope,
        client: GithubClient,
        *,
        head_ref: str,
        base: str,
        token: str,
    ) -> list[dict]:
        return await client.list_pulls_for_head(
            scope.repo_owner,
            scope.repo_name,
            head=f"{scope.repo_owner}:{head_ref}",
            base=base,
            state="all",
            token=token,
        )

    @staticmethod
    def _pull_number(pull: dict) -> int:
        number = pull.get("number")
        if not isinstance(number, int) or number <= 0:
            raise ValueError("GitHub returned a pull request without a valid number")
        return number

    async def _reconcile_attempt_pulls(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        pulls: list[dict],
        *,
        expected_base: str,
        verify_author: bool,
    ) -> int | None:
        unique_pulls: dict[int, dict] = {}
        for pull in pulls:
            number = self._pull_number(pull)
            previous = unique_pulls.get(number)
            if previous is not None and previous != pull:
                raise ValueError(
                    f"GitHub returned conflicting representations for PR #{number}"
                )
            unique_pulls[number] = pull

        classified: list[tuple[dict, str]] = []
        invalid: list[str] = []
        for pull in unique_pulls.values():
            verdict = self._classify_pull(pull)
            if verdict is None:
                invalid.append(str(pull["number"]))
            else:
                classified.append((pull, verdict))
        if invalid:
            raise ValueError(
                "GitHub returned unclassifiable pull request(s): " + ", ".join(invalid)
            )

        for verdict in ("open", "merged", "closed_unmerged"):
            selected = [pull for pull, state in classified if state == verdict]
            if not selected:
                continue
            for pull in selected:
                self._verify_pull_identity(
                    pull,
                    scope,
                    item,
                    expected_base=expected_base,
                    verify_author=verify_author,
                )
                self._pull_number(pull)

            numbers = sorted(self._pull_number(pull) for pull in selected)
            if verdict == "open":
                if len(selected) > 1:
                    item.status_note = (
                        "Multiple open pull requests match this dispatch head: "
                        + ", ".join(f"#{number}" for number in numbers)
                    )
                    item.updated_at = datetime.utcnow()
                    await db.commit()
                    raise ValueError(item.status_note)
                await self._record_selected_pull(
                    db,
                    scope,
                    item,
                    selected[0],
                    "open",
                )
                return numbers[0]
            if verdict == "merged":
                chosen = max(selected, key=self._pull_number)
                item.pr_number = self._pull_number(chosen)
                await self._mark_merged_with_fact(
                    db, item, chosen, source="github_verification_service._reconcile_attempt_pulls")
                item.status_note = (
                    "Merged pull requests found for this dispatch head: "
                    + ", ".join(f"#{number}" for number in numbers)
                )
                await db.commit()
                await self._notify_blocker_merged(db, scope, item)
                return int(item.pr_number)

            await github_dispatch_service.escalate_without_notification(
                db,
                item,
                "pr_closed_unmerged",
                "Pull requests closed without merge for this dispatch head: "
                + ", ".join(f"#{number}" for number in numbers),
            )
            raise ValueError(item.status_note or "pr_closed_unmerged")
        return None

    async def _record_selected_pull(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        pull: dict,
        verdict: str,
    ) -> None:
        pr_number = self._pull_number(pull)
        head_sha = self._head_sha(pull)
        if (
            item.pr_number != pr_number
            or item.verification_head_sha != head_sha
            or item.verification_started_at is None
        ):
            item.verification_head_sha = head_sha
            item.verification_started_at = datetime.utcnow()
        item.pr_number = pr_number
        item.last_verified_sha = None
        if verdict == "merged":
            await self._mark_merged_with_fact(
                db, item, pull, source="github_verification_service._record_selected_pull")
            await db.commit()
            await self._notify_blocker_merged(db, scope, item)
            return
        if item.issue_type == "design":
            item.dispatch_status = "awaiting_human_review"
            item.status_note = f"Design PR #{pr_number} is ready for human review."
            await github_dispatch_service.notify_team(
                db,
                item=item,
                subject="Design PR ready for review",
                body_markdown=(
                    f"Design PR #{pr_number} is ready for human review for "
                    f"issue #{item.issue_number}: {item.issue_title}\n\n"
                    + HUMAN_REVIEW_SUMMARY_GUIDANCE
                ),
                payload={
                    "kind": "github_dispatch_design_pr_ready",
                    "work_item_id": item.id,
                    "pr_number": pr_number,
                },
            )
        else:
            item.dispatch_status = "verifying"
            item.status_note = None
        item.updated_at = datetime.utcnow()
        await db.commit()

    async def process_scope(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        client: GithubClient | None = None,
        recovery_only_attempt: GithubRecoveryOnlyAttempt | None = None,
    ) -> None:
        client = client or github_client
        scope_id = scope.id
        closure_cutoff = datetime.utcnow()
        self.skipped_item_ids = set()
        await self._repair_exhausted_diagnostic_notifications(
            db, scope, recovery_only_attempt=recovery_only_attempt
        )
        item_ids = (
            await db.execute(
                select(GithubWorkItem.id).where(
                    GithubWorkItem.scope_id == scope.id,
                    GithubWorkItem.pr_number.is_not(None),
                    GithubWorkItem.dispatch_status.in_(
                        (
                            "dispatched",
                            "verifying",
                            "ready_for_review",
                            "awaiting_human_review",
                        )
                    ),
                    *(recovery_only_attempt.item_filters() if recovery_only_attempt else ()),
                )
            )
        ).scalars().all()

        for item_id in item_ids:
            # A rejected terminal claim rolls back and expires ORM objects.
            # Read each next item explicitly, with its current state.
            item = await db.get(GithubWorkItem, item_id, populate_existing=True)
            if item is None or item.dispatch_status not in (
                    "dispatched", "verifying", "ready_for_review", "awaiting_human_review"):
                continue
            if item.ack_received_at is None and await db.scalar(select(exists(
                    select(GithubAttemptScopeRevision.id).where(
                        GithubAttemptScopeRevision.work_item_id == item.id,
                        GithubAttemptScopeRevision.dispatch_nonce == item.dispatch_nonce,
                        GithubAttemptScopeRevision.origin_kind == "scope_amendment")))):
                # A proposed, rejected or approved-but-unacknowledged amendment
                # stops the whole round. It cannot advance old source through CI,
                # but a merged PR is an external fact that the stopped round must not hide.
                await self._settle_merged_revision(
                    db, scope, item, client, statuses=("submitted",),
                    source="github_verification_service._settle_merged_while_round_stopped")
                if inspect(item).unloaded:  # the merge check rolled back and expired the item
                    await db.refresh(item)
                await self._observe_superseded_revision(db, scope, item, client)
                continue
            try:
                if item.dispatch_status in ("dispatched", "verifying"):
                    revision = await self._active_scope_revision(db, item)
                    if item.active_scope_revision > 0:
                        if revision is None:
                            settled = await self._settled_revision(db, item)
                            if settled is not None:
                                await self._observe_settled_revision(db, scope, item, client, settled)
                            elif await self._superseded_revision(db, item) is not None:
                                await self._observe_superseded_revision(db, scope, item, client)
                            else:
                                # #1080: a completed revision that is not a positive settlement still settles a merged
                                # PR; the item counts as skipped only while it stays verifying.
                                snapshot = self._state_snapshot(item)
                                await self._settle_merged_revision(db, scope, item, client, statuses=("completed",))
                                if await db.scalar(select(GithubWorkItem.dispatch_status).where(
                                        GithubWorkItem.id == snapshot[0])) == "verifying":
                                    self._log_inconsistent_once(None, snapshot=snapshot)
                            continue
                        if revision.phase == "diagnostic":
                            if item.dispatch_status != "dispatched":
                                revision.status = "exhausted"
                                await github_dispatch_service.escalate(
                                    db,
                                    item,
                                    "continuation_invalid_state",
                                    "An active diagnostic continuation entered a review state.",
                                )
                                await db.commit()
                                continue
                            await self._observe_diagnostic_checks(
                                db,
                                scope,
                                item,
                                revision,
                                client,
                            )
                            continue
                        if item.dispatch_status == "dispatched":
                            # The revision's work is not submitted, but a merged PR is an external fact (#1078).
                            await self._settle_merged_revision(db, scope, item, client, statuses=("active",))
                            continue
                    await self._verify_item(
                        db,
                        scope,
                        item,
                        client,
                        revision=revision,
                    )
                elif await self._superseded_revision(db, item) is not None:
                    await self._observe_superseded_revision(db, scope, item, client)
                else:
                    await self._process_review_item(db, scope, item, client)
                # Repeated failed-head reads can return without another state write.
                # Persist the successful read even when that path uses no retry.
                if any(inspect(item).attrs[name].history.has_changes() for name in (
                    "checks_unavailable_since", "checks_unavailable_reason"
                )):
                    await db.commit()
            except TerminalRevisionContextChanged:
                await db.rollback()
                await db.refresh(scope)
                logger.info("Terminal attempt changed; no merge transition recorded")
            except GithubRateLimited:
                await db.rollback()
                raise
            except GithubCheckObservationError as exc:
                # Missing or ambiguous CI evidence must not consume a retry or
                # diagnostic budget. Continue polling the other work items.
                required_ci_attention.note_unreadable(item, required_ci_attention.error_reason(exc))
                self._set_failure_note(
                    item, f"GitHub checks are unavailable; will poll again: {exc}"
                )
                item.updated_at = datetime.utcnow()
                await db.commit()
            except httpx.HTTPError as exc:
                logger.exception(
                    "GitHub verification failed for work item %s", item.id
                )
                note = f"GitHub verification failed; will retry: {exc}"
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                    note = f"GitHub authentication or permissions failed (HTTP {exc.response.status_code})."
                self._set_failure_note(item, note)
                item.updated_at = datetime.utcnow()
                await db.commit()

        await self._reconcile_escalated_merges(
            db, scope, client, recovery_only_attempt=recovery_only_attempt
        )
        if recovery_only_attempt is None:
            from app.services.github_issue_closure_service import process_issue_closures

            try:
                await process_issue_closures(db, scope_id, client, due_before=closure_cutoff)
            except GithubRateLimited:
                raise
            except Exception as exc:
                await recover_session(db, exc, discard=True)
                logger.warning("issue_closure_failed error_type=%s", type(exc).__name__)
        # #940: the generated summary comment, only after every decision above has been committed. It reads no gate,
        # changes no status and never raises into the poll.
        try:
            from app.services.github_summary_publication_service import github_summary_publication_service

            if inspect(scope).expired:
                await db.refresh(scope)
            await github_summary_publication_service.publish_scope(db, scope, client)
        except Exception as exc:
            await recover_session(db, exc, discard=True)
            logger.warning("summary_publication_failed error_type=%s", type(exc).__name__)

    async def _reconcile_escalated_merges(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        client: GithubClient,
        *,
        recovery_only_attempt: GithubRecoveryOnlyAttempt | None,
    ) -> None:
        """Observe at most eight preserved PRs without granting recovery authority."""
        # Callers can retain a detached scope across polling sessions. Rollback
        # can also expire its attributes. Read identity without loading fields.
        identity = inspect(scope).identity
        if identity is None:
            return
        scope_id = identity[0]
        scope = await db.get(TeamGithubScope, scope_id, populate_existing=True)
        if scope is None:
            return
        query = select(GithubWorkItem.id).where(
            GithubWorkItem.scope_id == scope_id,
            GithubWorkItem.dispatch_status == "escalated",
            GithubWorkItem.pr_number.is_not(None),
            *(recovery_only_attempt.item_filters() if recovery_only_attempt else ()),
        ).order_by(GithubWorkItem.id).limit(8)
        cursor = self._escalated_merge_cursors.get(scope_id, 0)
        items = (await db.execute(query.where(GithubWorkItem.id > cursor))).scalars().all()
        if not items and cursor:
            items = (await db.execute(query)).scalars().all()
        for item_id in items:
            self._escalated_merge_cursors[scope_id] = item_id
            try:
                # A prior claim/notification can roll back and expire all rows.
                scope = await db.get(TeamGithubScope, scope_id, populate_existing=True)
                if scope is None:
                    return
                item = await db.get(GithubWorkItem, item_id, populate_existing=True)
                if (
                    item is None or item.scope_id != scope_id or item.pr_number is None
                    or (recovery_only_attempt is not None
                        and not recovery_only_attempt.matches_item(item))
                ):
                    continue
                await self._reconcile_escalated_merge(
                    db, scope, item, client,
                    notify=recovery_only_attempt is None,
                )
            except GithubRateLimited:
                raise
            except (httpx.HTTPError, GithubClientResponseError, ValueError):
                # Observation failure is not another implementation failure.
                logger.warning(
                    "Escalated PR merge observation unavailable for work item %s",
                    item_id,
                )

    async def _reconcile_escalated_merge(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        *,
        notify: bool,
    ) -> None:
        if (
            item.dispatch_status != "escalated"
            or item.attempt_phase != "implementation"
            or not item.dispatch_nonce
            or not item.dispatch_head_ref
            or not item.dispatch_base_ref
            or item.handoff_state is not None
        ):
            return
        # Capture the attempt before external reads. The final claim rejects a
        # concurrent retry, approval, handoff, policy pause, or revision change.
        fields = (
            "id", "scope_id", "dispatch_status", "dispatch_nonce", "pr_number",
            "dispatch_head_ref", "dispatch_base_ref", "owner_slot_id",
            "active_scope_revision", "attempt_phase", "handoff_state",
            "retry_count", "diagnostic_retry_count", "approval_round_count",
            "escalation_reason", "last_verified_sha", "updated_at",
        )
        expected = {field: getattr(item, field) for field in fields}
        scope_fields = ("id", "preset_id", "repo_owner", "repo_name", "github_auth_mode")
        expected_scope = {field: getattr(scope, field) for field in scope_fields}
        enabled_scope = exists(
            select(TeamGithubScope.id)
            .join(AgentTeamPreset, AgentTeamPreset.id == TeamGithubScope.preset_id)
            .where(
                *(getattr(TeamGithubScope, key) == value for key, value in expected_scope.items()),
                TeamGithubScope.enabled.is_(True),
                AgentTeamPreset.autonomy_enabled.is_(True),
            )
        )
        if not await db.scalar(select(enabled_scope)):
            return
        pending_approval = exists(select(GithubApprovalRequest.id).where(
            GithubApprovalRequest.work_item_id == item.id,
            GithubApprovalRequest.status == "pending",
        ))
        if await db.scalar(select(pending_approval)):
            return
        # #1079: an escalated attempt's revision may be active, submitted or exhausted when its PR merges.
        revision = await self._escalated_merge_revision(db, item) if item.active_scope_revision else None
        if item.active_scope_revision and revision is None:
            return
        workspace = None
        lease_snapshot = None
        revision_snapshot = ({column.name: getattr(revision, column.name)
            for column in revision.__table__.columns} if revision is not None else {})
        revision_guard = ()
        if revision is not None:  # the exact revision the decision is made on, whatever its status
            revision_guard = (exists(select(GithubAttemptScopeRevision.id).where(
                GithubAttemptScopeRevision.owner_slot_id == expected["owner_slot_id"],
                *(_same_value(getattr(GithubAttemptScopeRevision, key), value)
                  for key, value in revision_snapshot.items()),
            )),)
        closed_policy_guard = policy_context(item)
        if revision is not None:
            workspace = await db.scalar(select(GithubWorkspace).where(
                GithubWorkspace.id == revision.expected_workspace_id,
                GithubWorkspace.leased_item_id == item.id,
                GithubWorkspace.scope_id == scope.id,
            ))
            if (workspace is not None and workspace.lease_token
                    and github_approval_service.lease_token_matches(
                        workspace.lease_token, revision.expected_lease_token_hash)):
                lease_snapshot = (workspace.id, workspace.lease_token, workspace.leased_at)
        pull = await client.get_pull(scope.repo_owner, scope.repo_name, expected["pr_number"])
        verdict = self._classify_pull(pull)
        if verdict == "open":
            if notify and revision is None and expected["active_scope_revision"] == 0:
                await self._consider_escalated_auto_merge(
                    db, scope, item, client, pull, expected=expected,
                    guards=(enabled_scope, ~pending_approval),
                )
            return
        # Only a submitted revision is superseded by a closed PR; an active or exhausted one is left as it is.
        closed_submitted = verdict == "closed_unmerged" and revision is not None and revision.status == "submitted"
        if not closed_submitted and (verdict != "merged" or pull.get("merged") is not True):
            return
        if self._pull_number(pull) != expected["pr_number"]:
            raise ValueError("merged PR number does not match the tracked PR")
        head_sha = self._head_sha(pull)
        if not isinstance(head_sha, str) or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None:
            raise ValueError("merged PR head SHA is unavailable")
        # #1164: a superseded revision never settles by its own head; only an exact active acceptance does.
        submitted_head = None if revision is not None and revision.status == "superseded" else (
            revision.submitted_head_sha if revision is not None else None)
        if revision is not None and head_sha != submitted_head and not (
                verdict == "merged" and await db.scalar(select(exists().where(
                    GithubHeadReview.work_item_id == item.id,
                    GithubHeadReview.dispatch_nonce == expected["dispatch_nonce"],
                    GithubHeadReview.pr_number == expected["pr_number"], GithubHeadReview.head_sha == head_sha,
                    GithubHeadReview.verdict == "accepted", GithubHeadReview.status == "active")))):
            return  # nobody submitted or accepted this head: it stays escalated for the operator (#1038 Q1)
        expected_base = await self.normalize_base_ref(
            scope, client, token=None, base_ref=expected["dispatch_base_ref"]
        )
        self._verify_pull_identity(pull, scope, item, expected_base=expected_base)
        if closed_submitted:
            from app.services.github_coordination_service import hold_code
            if hold_code() or lease_snapshot is None:
                return
            lease_id, lease_token, leased_at = lease_snapshot
            lease_guard = exists(select(GithubWorkspace.id).where(
                GithubWorkspace.id == lease_id, GithubWorkspace.scope_id == expected["scope_id"],
                GithubWorkspace.leased_item_id == expected["id"],
                GithubWorkspace.lease_token == lease_token, GithubWorkspace.leased_at == leased_at,
                GithubWorkspace.enabled.is_(True),
            ))
            no_pause = ~exists(select(GithubOwnerObservationPause.id).where(
                GithubOwnerObservationPause.work_item_id == expected["id"],
                GithubOwnerObservationPause.status.in_(("waiting", "paused"))))
            exact_revision = exists(select(GithubAttemptScopeRevision.id).where(*(
                getattr(GithubAttemptScopeRevision, key) == value for key, value in revision_snapshot.items())))
            # The second remote read cannot turn a reopened or replaced head into
            # a terminal revision. Its await also precedes the final SQL fence.
            current = await client.get_pull(scope.repo_owner, scope.repo_name, expected["pr_number"])
            if (hold_code() or self._classify_pull(current) != "closed_unmerged"
                    or self._pull_number(current) != expected["pr_number"]
                    or self._head_sha(current) != head_sha):
                await db.rollback()
                return
            self._verify_pull_identity(current, scope, item, expected_base=expected_base)
            claim = await db.execute(update(GithubWorkItem).where(
                *(getattr(GithubWorkItem, key) == value for key, value in expected.items()),
                enabled_scope, ~pending_approval, *revision_guard, lease_guard, no_pause, exact_revision,
                *policy_context_conditions(closed_policy_guard),
            ).values(status_note=(
                f"PR #{expected['pr_number']} closed without merge; submitted revision superseded. "
                "No retry, approval or workspace release was granted."
            )).execution_options(synchronize_session=False))
            if claim.rowcount != 1 or hold_code():
                await db.rollback()
                return
            revision.status = "superseded"
            await self._record_revision_outcome(db, item, revision, "superseded",
                source="github_verification_service._reconcile_escalated_merge")
            await db.flush()
            revision_snapshot["status"] = "superseded"
            final = await db.execute(update(GithubWorkItem).where(
                *(getattr(GithubWorkItem, key) == value for key, value in expected.items()),
                enabled_scope, ~pending_approval, lease_guard, no_pause,
                *policy_context_conditions(closed_policy_guard),
                exists(select(GithubAttemptScopeRevision.id).where(*(
                    getattr(GithubAttemptScopeRevision, key) == value
                    for key, value in revision_snapshot.items()))),
            ).values(updated_at=GithubWorkItem.updated_at).execution_options(synchronize_session=False))
            if final.rowcount != 1 or hold_code():
                await db.rollback()
                return
            await db.commit()
            return
        claim = await db.execute(update(GithubWorkItem).where(
            *(getattr(GithubWorkItem, key) == value for key, value in expected.items()),
            enabled_scope, ~pending_approval, *revision_guard,
        ).values(updated_at=GithubWorkItem.updated_at).execution_options(synchronize_session=False))
        if claim.rowcount != 1:
            await db.rollback()
            await db.refresh(scope)
            await db.refresh(item)
            return
        await self._mark_merged_with_fact(
            db, item, pull, source="github_verification_service._reconcile_escalated_merge")
        if revision is not None and revision_snapshot["status"] in ("active", "submitted"):
            revision.status = "completed"  # an exhausted revision stays exhausted (#1038 Q2)
            revision.completed_at = datetime.utcnow()
            await self._record_revision_outcome(
                db, item, revision, "completed",
                source="github_verification_service._reconcile_escalated_merge")
        from app.services import factory_audit_service as _audit
        await _audit.record_merged_settlement(
            db, item_id=item.id, revision_id=revision.id if revision is not None else None,
            reason=("pr_merged_while_escalated" if revision is None
                    else f"pr_merged_while_escalated_with_{revision_snapshot['status']}_revision"),
            source="github_verification_service._reconcile_escalated_merge")
        await db.commit()
        if notify:
            await self._notify_blocker_merged(db, scope, item)

    async def _settled_diagnostic_revision(
        self, db: AsyncSession, item: GithubWorkItem
    ) -> GithubAttemptScopeRevision | None:
        """The newest revision of this attempt when it is a completed diagnostic one that produced the item's escalation."""
        newest = await db.scalar(
            select(GithubAttemptScopeRevision).where(
                GithubAttemptScopeRevision.work_item_id == item.id,
                GithubAttemptScopeRevision.dispatch_nonce == item.dispatch_nonce,
            ).order_by(GithubAttemptScopeRevision.revision.desc()).limit(1)
            .execution_options(populate_existing=True)
        )
        if (
            newest is None or newest.phase != "diagnostic" or newest.status != "completed"
            or not item.escalation_reason
            or newest.originating_escalation_reason != item.escalation_reason
            or not isinstance(newest.evidence, dict)
            or not isinstance(newest.evidence.get("diagnostic_completion"), dict)
        ):
            return None
        return newest

    async def _consider_escalated_auto_merge(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        pull: dict,
        *,
        expected: dict,
        guards: tuple,
    ) -> None:
        """An accepted, ready, unheld, unchanged PR on an escalated item: say why it is not merged, or reopen it.

        Only an item that is escalated because of a settled diagnostic revision can return to ``ready_for_review``,
        and only through a conditional update. The unchanged auto-merge path then applies every gate: the hold label,
        the reviewer's acceptance of the exact head, the Leader approval, the daily cap and the green checks.
        """
        if scope.merge_policy != "auto" or item.issue_type != "code":
            return
        if self._pull_number(pull) != expected["pr_number"]:
            return
        expected_base = await self.normalize_base_ref(
            scope, client, token=None, base_ref=expected["dispatch_base_ref"])
        try:
            self._verify_pull_identity(pull, scope, item, expected_base=expected_base)
        except ValueError:
            return  # not this attempt's PR: nothing here is offered for automatic merge
        head = self._head_sha(pull)
        if not isinstance(head, str) or re.fullmatch(r"[0-9a-f]{40}", head) is None:
            return
        if self._draft_readiness_note(item, pull) is not None:
            return
        identity = (item.pr_number, item.dispatch_nonce, item.scope_id)
        if await self._acceptance_note(db, scope, item, pull, identity) is not None:
            return
        if await self._review_hold_note(scope, item, client, pull) is not None:
            return
        reason = item.escalation_reason or "escalation_reason_missing"  # a legacy empty row (#1139)
        settled = await self._settled_diagnostic_revision(db, item)
        if settled is None:
            await self._record_escalated_not_fired(
                db, scope, item, expected, guards,
                self._not_fired("item_not_eligible", f"escalated: {reason}", head),
                f"Auto-merge not available: the item is escalated ({reason}); a human merges this PR. "
                f"[item_not_eligible: escalated: {reason}]",
            )
            return
        reopened = settled.evidence.get("auto_merge_reopened")
        if head != item.last_verified_sha or (
            isinstance(reopened, dict) and reopened.get("head_sha") == head
        ):
            return  # changed since it was verified, or already given back once at this head
        if await self._approval_gate_reason(db, scope, item, identity) is not None:
            return
        used, cap = await self._auto_merge_usage(db, scope)
        if used >= cap:
            detail = f"{used} of {cap} automatic merges in the last 24 hours"
            await self._record_escalated_not_fired(
                db, scope, item, expected, guards,
                self._not_fired("auto_merge_cap_reached", detail, head),
                f"Auto-merge budget exhausted; PR is ready for human merge. [auto_merge_cap_reached: {detail}]",
            )
            return
        from app.services.github_coordination_service import hold_code
        if hold_code():
            return
        no_pause = ~exists(select(GithubOwnerObservationPause.id).where(
            GithubOwnerObservationPause.work_item_id == expected["id"],
            GithubOwnerObservationPause.status.in_(("waiting", "paused"))))
        still_settled = exists(select(GithubAttemptScopeRevision.id).where(
            GithubAttemptScopeRevision.id == settled.id,
            GithubAttemptScopeRevision.status == "completed",
            GithubAttemptScopeRevision.phase == "diagnostic",
        ))
        evidence = dict(settled.evidence)
        evidence["auto_merge_reopened"] = {"head_sha": head}
        # The final conditional write: every identity field is unchanged, and so is the head the checks read.
        claim = await db.execute(update(GithubWorkItem).where(
            *(getattr(GithubWorkItem, key) == value for key, value in expected.items()),
            GithubWorkItem.last_verified_sha == head,
            *guards, no_pause, still_settled,
        ).values(
            dispatch_status="ready_for_review",
            escalation_reason=None,
            status_note=(
                f"Settled diagnostic escalation given back to automatic merge at {head[:8]}; "
                "every merge gate still applies."
            ),
            updated_at=datetime.utcnow(),
        ).execution_options(synchronize_session=False))
        if claim.rowcount != 1 or hold_code():
            await db.rollback()
            await db.refresh(scope)
            await db.refresh(item)
            return
        marker = await db.execute(update(GithubAttemptScopeRevision).where(
            GithubAttemptScopeRevision.id == settled.id,
            GithubAttemptScopeRevision.status == "completed",
        ).values(evidence=evidence).execution_options(synchronize_session=False))
        if marker.rowcount != 1:
            await db.rollback()
            await db.refresh(scope)
            await db.refresh(item)
            return
        await db.commit()
        await db.refresh(item)

    async def _record_escalated_not_fired(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        expected: dict,
        guards: tuple,
        not_fired: dict,
        note: str,
    ) -> None:
        """Record the reason on an escalated item and send one Leader notice and one operator notice."""
        if item.status_note == note:
            return  # already recorded for this state; later polls stay silent
        try:
            claim = await db.execute(update(GithubWorkItem).where(
                *(getattr(GithubWorkItem, key) == value for key, value in expected.items()),
                *guards,
            ).values(status_note=note).execution_options(synchronize_session=False))
            if claim.rowcount != 1:
                await db.rollback()
                await db.refresh(scope)
                await db.refresh(item)
                return
            await self._add_operator_not_fired(db, scope, item, not_fired)
            # The Leader's notice commits last, so the note and both notices persist together or not at all.
            await agent_mail_service.send_broadcast(
                db,
                audience_type="work_item",
                audience_id=str(item.id),
                subject=f"Auto-merge did not fire: issue #{item.issue_number}",
                body_markdown=(
                    f"PR #{item.pr_number} for issue #{item.issue_number} is accepted but was not merged "
                    f"automatically: {not_fired['code']} ({not_fired['detail']}). "
                    "It needs a manual merge under the configured policy."
                ),
                payload={
                    "kind": "github_auto_merge_not_fired", "work_item_id": item.id,
                    "pr_number": item.pr_number, "reason": not_fired["code"], "detail": not_fired["detail"],
                    "head_sha": not_fired["head_sha"],
                },
                delivery_key=(
                    f"auto-merge-not-fired:{item.id}:{item.pr_number}:{not_fired['head_sha']}:"
                    f"{not_fired['code']}:{not_fired['detail']}:leader"
                ),
            )
        except BaseException:
            await db.rollback()
            raise
        await db.refresh(item)

    async def _settle_merged_revision(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, client: GithubClient, *,
        statuses: tuple[str, ...],
        source: str = "github_verification_service._settle_merged_revision",
    ) -> None:
        """Record a merge on an item whose round does not otherwise read its PR: an unacknowledged amendment stopped
        it, or its active revision is not submitted yet (#1078).

        Only a merged PR changes anything here; an open or closed pull leaves the item as it is. ``statuses`` are the
        revision states this caller may settle. A failure for this item is logged and isolated, and never stops the
        other items.
        """
        item_id = item.id
        try:
            if item.dispatch_status not in ("dispatched", "verifying") or item.active_scope_revision <= 0:
                return
            # The current revision by number, in one of the caller's states (#1080: a completed one too).
            revision = await db.scalar(select(GithubAttemptScopeRevision).where(
                GithubAttemptScopeRevision.work_item_id == item.id,
                GithubAttemptScopeRevision.dispatch_nonce == item.dispatch_nonce,
                GithubAttemptScopeRevision.revision == item.active_scope_revision,
            ).execution_options(populate_existing=True))
            if (revision is None or revision.status not in statuses or revision.phase != "implementation"
                    or item.attempt_phase != revision.phase):
                return
            pull = await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
            if self._classify_pull(pull) != "merged" or pull.get("merged") is not True:
                return
            expected_base = await self.normalize_base_ref(
                scope, client, token=None, base_ref=item.dispatch_base_ref)
            self._verify_pull_identity(pull, scope, item, expected_base=expected_base)
            await self._settle_merged_submitted(db, scope, item, client, revision, pull, source=source)
        except TerminalRevisionContextChanged:
            await db.rollback()
            await db.refresh(scope)
            logger.info("Terminal attempt changed; no merge transition recorded")
        except Exception as caught_error:
            await recover_session(db, caught_error, discard=True)
            logger.warning("Merged-PR check failed for work item %s; the item stays as it is", item_id,
                           exc_info=True)
            await db.refresh(scope)

    async def _settled_revision(
        self, db: AsyncSession, item: GithubWorkItem,
    ) -> GithubAttemptScopeRevision | None:
        """The positively settled implementation revision a verifying item is still on, if it is one.

        Positive settlement completes the revision and leaves the item on it. Such an item waits for its PR to
        merge, so it is not inconsistent. Any other state is.
        """
        if item.dispatch_status != "verifying" or item.active_scope_revision <= 0 or item.pr_number is None:
            return None
        revision = (
            await db.execute(
                select(GithubAttemptScopeRevision)
                .where(
                    GithubAttemptScopeRevision.work_item_id == item.id,
                    GithubAttemptScopeRevision.dispatch_nonce == item.dispatch_nonce,
                    GithubAttemptScopeRevision.revision == item.active_scope_revision,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            revision is None
            or revision.status != "completed"
            or revision.phase != "implementation"
            or item.attempt_phase != revision.phase
            or revision.completed_at is None
            or not revision.submitted_head_sha
        ):
            return None
        return revision

    @staticmethod
    def _state_snapshot(item: GithubWorkItem) -> tuple:
        return (item.id, item.dispatch_nonce, item.active_scope_revision, item.dispatch_status, item.attempt_phase)

    def _log_inconsistent_once(
        self, item: GithubWorkItem | None, reason: str | None = None, *, snapshot: tuple | None = None,
    ) -> None:
        """Log one warning per (item, attempt, revision, status, phase). A snapshot survives a rollback."""
        snapshot = snapshot or self._state_snapshot(item)
        self.skipped_item_ids.add(snapshot[0])  # every poll, even when the log line is already written
        key = (*snapshot, reason is not None)
        if key in self._logged_inconsistent:
            return
        self._logged_inconsistent.add(key)
        logger.warning(
            "Skipping continuation verification for work item %s: %s (logged once for this state)",
            snapshot[0], reason or "active continuation revision is inconsistent",
        )

    async def _observe_settled_revision(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        revision: GithubAttemptScopeRevision,
    ) -> None:
        """A positively settled revision waits for its PR: record a merge, escalate a closed PR, promote an open one.

        Everything here is contained to this item. A GitHub read that fails changes nothing and logs nothing: the
        item simply waits for the next poll. A first response that fails the identity check changes nothing and
        logs one warning per state. Both outcomes then take the same fences: the PR is read again, must pass the
        identity check again, and the terminal writer is taken before the captured item, scope and revision are
        compared again.
        """
        snapshot = self._state_snapshot(item)  # a rollback expires the item
        try:
            pull = await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
            verdict = self._classify_pull(pull)
            if verdict == "merged" and pull.get("merged") is True:
                await self._verify_settled_pull(scope, item, client, pull)
                await self._settle_merged_submitted(
                    db, scope, item, client, revision, pull,
                    source="github_verification_service._observe_settled_revision")
            elif verdict == "closed_unmerged":
                await self._escalate_settled_closed(db, scope, item, client, revision)
            elif verdict == "open":
                await self._promote_settled_revision(db, scope, item, client, revision, pull)
        except TerminalRevisionContextChanged:
            raise  # the caller rolls back and records that the attempt changed
        except GithubRateLimited:
            raise
        except (httpx.HTTPError, GithubClientResponseError):
            # An unreadable or uninterpretable GitHub response is a failed read: nothing changes, nothing is logged.
            await db.rollback()
            await db.refresh(scope)
        except ValueError as exc:
            await db.rollback()
            await db.refresh(scope)
            self._log_inconsistent_once(None, f"a PR response failed the identity check ({exc})", snapshot=snapshot)

    async def _superseded_revision(
        self, db: AsyncSession, item: GithubWorkItem,
    ) -> GithubAttemptScopeRevision | None:
        """The superseded implementation revision an item with an open-PR candidate is still on, if it is one (#972).

        A Leader settlement of a negative review ends the submitted revision as superseded and leaves the item
        'dispatched' with no owner acknowledgement. The owner can then push a head that the reviewer accepts, but
        no amendment is left to carry it. Any other shape, a handoff included, is not this one.
        """
        if (item.dispatch_status not in ("dispatched", "verifying", "ready_for_review", "awaiting_human_review")
                or item.issue_type != "code" or item.active_scope_revision <= 0 or item.pr_number is None
                or item.handoff_state is not None):
            return None
        revision = (
            await db.execute(
                select(GithubAttemptScopeRevision)
                .where(
                    GithubAttemptScopeRevision.work_item_id == item.id,
                    GithubAttemptScopeRevision.dispatch_nonce == item.dispatch_nonce,
                    GithubAttemptScopeRevision.revision == item.active_scope_revision,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (revision is None or revision.status != "superseded" or revision.phase != "implementation"
                or item.attempt_phase != revision.phase):
            return None
        return revision

    async def _observe_superseded_revision(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, client: GithubClient,
    ) -> None:
        """An open PR on a superseded revision: promote it on an exact-head acceptance, or say why not (#972).

        A superseded revision alone grants nothing. The promotion needs a current verifier's active acceptance of
        the exact PR head, every required check green, and the PR ready, and then runs the same fenced path as a
        settled revision, so the hold label, the Leader approval, the cap, the hold and pause codes and the merge
        policy all apply unchanged. A promoted item is then carried by this observer too, because the stopped round
        would otherwise skip it. A merged or closed PR keeps its current handling.
        Everything here is contained to this item; a failed GitHub read changes nothing.
        """
        snapshot = self._state_snapshot(item)  # a rollback expires the item
        try:
            revision = await self._superseded_revision(db, item)
            if revision is None:
                return
            if item.dispatch_status in ("ready_for_review", "awaiting_human_review"):
                await self._process_review_item(db, scope, item, client, superseded_ok=True)
                return
            pull = await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
            if self._classify_pull(pull) == "open":
                await self._promote_settled_revision(db, scope, item, client, revision, pull)
        except TerminalRevisionContextChanged:
            await db.rollback()
            await db.refresh(scope)
            logger.info("Terminal attempt changed; no promotion recorded")
        except GithubRateLimited:
            raise
        except (httpx.HTTPError, GithubClientResponseError):
            await db.rollback()
            await db.refresh(scope)
        except ValueError as exc:
            await db.rollback()
            await db.refresh(scope)
            self._log_inconsistent_once(None, f"a PR response failed the identity check ({exc})", snapshot=snapshot)
        except Exception as caught_error:
            await recover_session(db, caught_error, discard=True)
            logger.warning("Superseded-revision check failed for work item %s; the item stays as it is",
                           snapshot[0], exc_info=True)
            await db.refresh(scope)

    async def _promote_settled_revision(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        revision: GithubAttemptScopeRevision,
        pull: dict,
    ) -> None:
        """An open PR on a positively settled revision: promote it like any verified item, or say why not (#957).

        Settlement completes the revision and leaves the item 'verifying'. Nothing else would read its checks or
        move it to ready_for_review, so an accepted, green, ready and unheld PR never reached the auto-merge gates.
        The promotion needs the PR head to be the settled head, an active acceptance of that exact head from a
        current verifier, and every required check green. Then the existing promotion runs, with no revision
        because this one is already completed, and every merge gate and policy applies unchanged: the auto policy
        goes on to the merge gates, the human policy stops at ready_for_review and never merges.
        When one condition fails the reason is recorded as the item's note, written only when it changes. No retry
        is charged, no status changes and no revision is touched.
        """
        await self._verify_settled_pull(scope, item, client, pull)  # a ValueError is logged once by the caller
        head = self._head_sha(pull)
        if not isinstance(head, str) or not head:
            return
        # A superseded revision (#972) is promoted on the exact-head acceptance alone: it records no submitted head to
        # compare with, and it grants no authority of its own. Its row must stay exactly as it was read.
        superseded = revision.status == "superseded"
        label = "Superseded" if superseded else "Settled"
        # What the decision rests on is captured before any awaited read and compared again under the terminal
        # writer, so a context withdrawn while GitHub was awaited is never promoted.
        item_before = {name: getattr(item, name) for name in _SETTLEMENT_ITEM_IDENTITY}
        scope_before = {name: getattr(scope, name) for name in _SETTLEMENT_SCOPE_IDENTITY}
        revision_before = {column.name: getattr(revision, column.name) for column in revision.__table__.columns}
        reason = None
        if not superseded and head != revision.submitted_head_sha:
            reason = (f"Settled revision not promoted: the PR head {head[:7]} is not the settled head "
                      f"{str(revision.submitted_head_sha)[:7]}.")
        else:
            from app.services import github_head_acceptance_service as acceptance

            acceptance_note = await acceptance.gate_note(db, scope, item, head)
            if acceptance_note is not None:
                reason = f"{label} revision not promoted: {acceptance_note}"
            elif not await self._head_is_green(scope, client, head, item=item):
                failed_statuses = await self._failed_commit_statuses(scope, client, head)
                reason = (
                    f"{label} revision not promoted: commit status failed at {head[:7]}: "
                    f"{', '.join(failed_statuses)}." if failed_statuses else
                    f"{label} revision not promoted: the required checks are not all green at {head[:7]}.")
            elif superseded:
                refusal = await self._capture_superseded_fence(db, scope, item, head)
                if isinstance(refusal, str):
                    reason = f"Superseded revision not promoted: {refusal}"
        context = (item_before, scope_before, revision_before)
        if reason is not None:
            await self._record_settled_note(db, scope, item, revision, context, reason)
            return
        policy_guard = policy_context(item)
        if not await claim_policy_context(db, item, policy_guard):
            return
        fresh = await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
        if not await self._validate_polled_pull_identity(
            db, scope, item, client, fresh, retry_status=item.dispatch_status,
        ):
            return
        if self._head_sha(fresh) != head:
            await self._record_settled_note(
                db, scope, item, revision, context, "Head changed before readiness; verifying the current head.")
            return
        readiness_note = self._draft_readiness_note(item, fresh)
        if readiness_note is not None:
            await self._record_settled_note(db, scope, item, revision, context, readiness_note, last_verified=head)
            return
        # No network call from here to the commit: the writer is held only for the compare and the write.
        await self._fence_settled_context(db, scope, item, revision, context)
        if superseded:
            if revision.status != "superseded" or item.handoff_state is not None:
                raise TerminalRevisionContextChanged("terminal_revision_context_changed")
        elif revision.status != "completed" or revision.completed_at is None or revision.submitted_head_sha != head:
            raise TerminalRevisionContextChanged("terminal_revision_context_changed")
        item.last_verified_sha = head
        item.dispatch_status = "ready_for_review"
        item.status_note = f"PR #{item.pr_number} is ready for review."
        item.updated_at = datetime.utcnow()
        if scope.merge_policy == "human":
            await self._notify_code_pr_ready_for_review(db, item)
        await db.commit()
        await self._process_review_item(db, scope, item, client, pull=fresh, superseded_ok=superseded)

    async def _fence_settled_context(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem,
        revision: GithubAttemptScopeRevision, context: tuple,
    ) -> None:
        """Take the terminal writer and compare the item, scope and revision with what the decision rested on.

        No network call may sit between this and the write that follows. A lost fence raises, so nothing is written.
        """
        item_before, scope_before, revision_before = context
        await lock_terminal_transition(db, item)
        await db.refresh(item)
        await db.refresh(scope)
        await db.refresh(revision)
        if ({name: getattr(item, name) for name in _SETTLEMENT_ITEM_IDENTITY} != item_before
                or {name: getattr(scope, name) for name in _SETTLEMENT_SCOPE_IDENTITY} != scope_before
                or {column.name: getattr(revision, column.name) for column in revision.__table__.columns}
                != revision_before):
            raise TerminalRevisionContextChanged("terminal_revision_context_changed")

    async def _record_settled_note(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem,
        revision: GithubAttemptScopeRevision, context: tuple, note: str, *, last_verified: str | None = None,
    ) -> None:
        """Record why a settled revision was not promoted, only under the fence and only when it changes.

        A note written after an awaited read would otherwise overwrite the note of a new attempt that replaced
        the item meanwhile.
        """
        if item.status_note == note and (last_verified is None or item.last_verified_sha == last_verified):
            return
        await self._fence_settled_context(db, scope, item, revision, context)
        if last_verified is not None:
            item.last_verified_sha = last_verified
        item.status_note = note
        item.updated_at = datetime.utcnow()
        await db.commit()

    @staticmethod
    async def _settled_snapshot(db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem) -> dict | None:
        """The settled revision row, every column, and the scope's enabled flag as they stand now, or ``None``.

        Taken before any awaited read of an operation, so the operation keeps the identity it began with. Only an
        item that has a scoped revision row has one; read as plain columns, never through the identity map.
        """
        if not item.active_scope_revision:
            return None
        row = (await db.execute(
            select(GithubAttemptScopeRevision.__table__).where(
                GithubAttemptScopeRevision.work_item_id == item.id,
                GithubAttemptScopeRevision.dispatch_nonce == item.dispatch_nonce,
                GithubAttemptScopeRevision.revision == item.active_scope_revision)
        )).mappings().first()
        if row is None:
            return None
        enabled = await db.scalar(select(TeamGithubScope.enabled).where(TeamGithubScope.id == scope.id))
        return {"row": dict(row), "enabled": enabled}

    @staticmethod
    async def _settled_context_note(db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem,
                                    head: str, expected: dict | None, *, superseded_ok: bool = False) -> str | None:
        """Why a merge must not go ahead because the settlement or the scope it rested on changed, or ``None``.

        ``expected`` is the snapshot the operation began with. The row is read again, fresh and column by column,
        so a change another connection committed while GitHub was awaited is seen: any column that differs from the
        snapshot blocks, so no changed field can slip through. The row must also still be completed with its
        completion time, an implementation row must carry the very head being merged (a row that began as a settled
        diagnostic restored the baseline and is not compared with the head), and the scope must still be enabled.
        """
        if expected is None:
            return None
        began = expected["row"]
        row = (await db.execute(
            select(GithubAttemptScopeRevision.__table__).where(GithubAttemptScopeRevision.id == began["id"])
        )).mappings().first()
        enabled = await db.scalar(select(TeamGithubScope.enabled).where(TeamGithubScope.id == scope.id))
        if superseded_ok and began["status"] == "superseded" and began["phase"] == "implementation":
            # #972: only the superseded-revision observer asks for this. Its promotion rests on the exact-head
            # acceptance, which the merge gates read again; here the row has to be the very one the promotion saw,
            # and the scope must still be enabled. Any other operation still treats a superseded row as withdrawn.
            ended = row is not None and dict(row) == began and row["status"] == "superseded"
        else:
            ended = (row is not None and dict(row) == began and row["status"] == "completed"
                     and row["completed_at"] is not None
                     and not (began["phase"] == "implementation" and row["submitted_head_sha"] != head))
        if not ended or enabled is not True or enabled != expected["enabled"]:
            return ("Auto-merge blocked: the settled revision or the scope changed since promotion; "
                    "verifying the current state.")
        return None

    @staticmethod
    def _require_pull_shape(pull: object) -> None:
        """Translate a malformed PR response into the identity ValueError before anything reads into it.

        The fields the identity check reads must have the shape GitHub documents: the response, its base and
        head, and their repo objects are objects, and the author is an object. Anything else is a malformed
        response, not a programming error.
        """
        if not isinstance(pull, dict):
            raise ValueError("the PR response is not an object")
        for key in ("base", "head"):
            side = pull.get(key)
            if side is not None and not isinstance(side, dict):
                raise ValueError(f"the PR response has a malformed {key}")
            if isinstance(side, dict) and side.get("repo") is not None and not isinstance(side["repo"], dict):
                raise ValueError(f"the PR response has a malformed {key} repository")
        if pull.get("user") is not None and not isinstance(pull["user"], dict):
            raise ValueError("the PR response has a malformed author")

    async def _verify_settled_pull(
        self, scope: TeamGithubScope, item: GithubWorkItem, client: GithubClient, pull: dict,
    ) -> None:
        """The PR response must be this item's PR in this scope and base; otherwise ValueError."""
        self._require_pull_shape(pull)
        if self._pull_number(pull) != item.pr_number:
            raise ValueError("the PR number does not match the tracked PR")
        expected_base = await self.normalize_base_ref(scope, client, token=None, base_ref=item.dispatch_base_ref)
        self._verify_pull_identity(pull, scope, item, expected_base=expected_base)

    async def _escalate_settled_closed(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        revision: GithubAttemptScopeRevision,
    ) -> None:
        """A settled revision's PR closed without a merge: escalate, fenced like the merge.

        What the decision rests on is captured before the awaited read. The PR is read again and must still be
        this PR, closed without a merge, and pass the identity check. Then the terminal writer is taken and the
        captured item, scope and revision must be unchanged, so a lost fence changes nothing.
        """
        item_before = {name: getattr(item, name) for name in _SETTLEMENT_ITEM_IDENTITY}
        scope_before = {name: getattr(scope, name) for name in _SETTLEMENT_SCOPE_IDENTITY}
        revision_before = {column.name: getattr(revision, column.name) for column in revision.__table__.columns}
        pr_number = item.pr_number
        current = await client.get_pull(scope.repo_owner, scope.repo_name, int(pr_number))
        if self._classify_pull(current) != "closed_unmerged" or self._pull_number(current) != pr_number:
            return  # the next poll reads the new state
        await self._verify_settled_pull(scope, item, client, current)
        await lock_terminal_transition(db, item)
        await db.refresh(item)
        await db.refresh(scope)
        await db.refresh(revision)
        if ({name: getattr(item, name) for name in _SETTLEMENT_ITEM_IDENTITY} != item_before
                or {name: getattr(scope, name) for name in _SETTLEMENT_SCOPE_IDENTITY} != scope_before
                or {column.name: getattr(revision, column.name) for column in revision.__table__.columns} != revision_before
                or revision.status != "completed"):
            raise TerminalRevisionContextChanged("terminal_revision_context_changed")
        await github_dispatch_service.escalate_without_notification(
            db, item, "pr_closed_unmerged", f"PR #{pr_number} was closed without being merged.")

    async def _settle_merged_submitted(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        revision: GithubAttemptScopeRevision,
        pull: dict,
        *,
        source: str,
    ) -> None:
        """A PR merged while a revision is active, submitted or positively settled: settle only at a head someone
        submitted or accepted.

        The merged head must equal the revision's submitted head or the head of an active accepted review; an active
        revision has no submitted head, so only an accepted review authorizes it (#1078). Then an active or submitted
        revision completes (a settled one already is, and keeps its status, its time and its one outcome record), the
        item is merged and one merged_settlement audit event names the state it settled from. Any other merged head escalates with a note naming
        both heads and records no merge; a settled revision stays as it was. Everything the decision rests on is captured before the awaited reads.
        The PR is read again and must pass the full identity check; then the terminal writer is taken and the
        captured item, scope and exact revision must still be what they were, so BOTH outcomes are fenced and a
        lost fence changes nothing. The verified final response is the one that is recorded.
        """
        head = self._head_sha(pull)
        if not isinstance(head, str) or not head:
            raise ValueError("merged PR head SHA is unavailable")
        if self._pull_number(pull) != item.pr_number:
            raise ValueError("merged PR number does not match the tracked PR")
        item_before = {name: getattr(item, name) for name in _SETTLEMENT_ITEM_IDENTITY}
        scope_before = {name: getattr(scope, name) for name in _SETTLEMENT_SCOPE_IDENTITY}
        revision_before = {column.name: getattr(revision, column.name) for column in revision.__table__.columns}
        item_id, pr_number, nonce, base_ref = item.id, item.pr_number, item.dispatch_nonce, item.dispatch_base_ref
        current = await client.get_pull(scope.repo_owner, scope.repo_name, int(pr_number))
        if revision_before["status"] == "completed":  # a settled revision: a malformed shape is a ValueError
            self._require_pull_shape(current)
        if (self._classify_pull(current) != "merged" or self._pull_number(current) != pr_number
                or self._head_sha(current) != head):
            return  # the next poll reads the new state
        try:
            expected_base = await self.normalize_base_ref(scope, client, token=None, base_ref=base_ref)
            self._verify_pull_identity(current, scope, item, expected_base=expected_base)
        except ValueError as exc:
            logger.warning("Merged PR #%s for work item %s failed the identity check on its final read (%s); "
                           "nothing was settled", pr_number, item_id, exc)
            return
        # The terminal writer first: it claims the loaded item identity, and nothing else can commit until this
        # transaction ends. Then the context the decision was made on must be unchanged.
        await lock_terminal_transition(db, item)
        await db.refresh(item)
        await db.refresh(scope)
        await db.refresh(revision)
        if ({name: getattr(item, name) for name in _SETTLEMENT_ITEM_IDENTITY} != item_before
                or {name: getattr(scope, name) for name in _SETTLEMENT_SCOPE_IDENTITY} != scope_before
                or {column.name: getattr(revision, column.name) for column in revision.__table__.columns} != revision_before
                or revision.status not in ("active", "submitted", "completed")):
            raise TerminalRevisionContextChanged("terminal_revision_context_changed")
        settled = revision.status == "completed"
        authorized = head == revision.submitted_head_sha or bool(await db.scalar(select(exists().where(
            GithubHeadReview.work_item_id == item_id, GithubHeadReview.dispatch_nonce == nonce,
            GithubHeadReview.pr_number == pr_number, GithubHeadReview.head_sha == head,
            GithubHeadReview.verdict == "accepted", GithubHeadReview.status == "active"))))
        outcome = "completed" if authorized else "superseded"
        # A settled revision is only fenced here: its status and time stay exactly as the settlement left them.
        values = (
            {"status": "completed"} if settled
            else {"status": outcome, "completed_at": datetime.utcnow()} if authorized else {"status": outcome}
        )
        claim = await db.execute(update(GithubAttemptScopeRevision).where(
            GithubAttemptScopeRevision.id == revision.id,
            GithubAttemptScopeRevision.work_item_id == item_id,
            GithubAttemptScopeRevision.dispatch_nonce == nonce,
            GithubAttemptScopeRevision.revision == item.active_scope_revision,
            GithubAttemptScopeRevision.status == revision_before["status"],
        ).values(**values).execution_options(synchronize_session=False))
        if claim.rowcount != 1:
            raise TerminalRevisionContextChanged("terminal_revision_context_changed")
        await db.refresh(revision)
        if not settled:
            await self._record_revision_outcome(db, item, revision, outcome, source=source)
        if not authorized:
            await github_dispatch_service.escalate_without_notification(
                db, item, "continuation_invalid_state",
                f"PR #{pr_number} merged at head {head}, which is not "
                + (f"the submitted revision head {revision_before['submitted_head_sha']}"
                   if revision_before["submitted_head_sha"] else "a submitted head (the revision was still active)")
                + " or an accepted head; no merge was recorded. An operator must review and finalize this item.")
            return
        await self._mark_merged_with_fact(db, item, current, source=source)
        from app.services import factory_audit_service as _audit
        await _audit.record_merged_settlement(
            db, item_id=item_id, revision_id=revision.id,
            reason=f"pr_merged_with_{revision_before['status']}_revision", source=source)
        await db.commit()
        await self._notify_blocker_merged(db, scope, item)

    async def _escalated_merge_revision(
        self, db: AsyncSession, item: GithubWorkItem,
    ) -> GithubAttemptScopeRevision | None:
        """The escalated attempt's current implementation revision when a merge may settle it (#1079).

        Unlike the active-revision lookup, an exhausted revision is returned too, and so is a superseded one (#1164:
        cancelled through the Leader's tool while escalated). A superseded revision has no submitted head, so it
        settles only by an active acceptance of the merged head. Any other status is not returned.
        """
        revision = await db.scalar(select(GithubAttemptScopeRevision).where(
            GithubAttemptScopeRevision.work_item_id == item.id,
            GithubAttemptScopeRevision.dispatch_nonce == item.dispatch_nonce,
            GithubAttemptScopeRevision.revision == item.active_scope_revision,
        ).execution_options(populate_existing=True))
        if (revision is None or revision.phase != "implementation" or item.attempt_phase != revision.phase
                or revision.status not in ("active", "submitted", "exhausted", "superseded")):
            return None
        return revision

    async def _active_scope_revision(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
    ) -> GithubAttemptScopeRevision | None:
        if item.active_scope_revision == 0:
            return None
        revision = (
            await db.execute(
                select(GithubAttemptScopeRevision)
                .where(
                    GithubAttemptScopeRevision.work_item_id == item.id,
                    GithubAttemptScopeRevision.dispatch_nonce == item.dispatch_nonce,
                    GithubAttemptScopeRevision.revision
                    == item.active_scope_revision,
                )
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if (
            revision is None
            or item.attempt_phase != revision.phase
            or revision.phase not in {"implementation", "diagnostic"}
            or revision.status not in {"active", "submitted"}
        ):
            return None
        return revision

    @staticmethod
    def _diagnostic_check_evidence(checks: list[dict]) -> list[dict]:
        fields = (
            "id", "name", "status", "conclusion", "html_url", "details_url",
            "evidence_source", "workflow_run_id", "run_attempt",
        )
        return [
            {field: check.get(field) for field in fields if check.get(field) is not None}
            for check in checks
        ]

    @staticmethod
    def _diagnostic_status_evidence(status: dict) -> list[dict]:
        fields = ("context", "state", "target_url", "description")
        return [
            {field: context.get(field) for field in fields if context.get(field) is not None}
            for context in status.get("statuses") or []
            if isinstance(context, dict)
        ]

    async def _claim_current_diagnostic_context(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        revision: GithubAttemptScopeRevision,
        *, policy_guard: tuple | None = None,
    ) -> bool:
        current_revision = exists(
            select(GithubAttemptScopeRevision.id).where(
                GithubAttemptScopeRevision.id == revision.id,
                GithubAttemptScopeRevision.work_item_id == item.id,
                GithubAttemptScopeRevision.dispatch_nonce == revision.dispatch_nonce,
                GithubAttemptScopeRevision.revision == revision.revision,
                GithubAttemptScopeRevision.owner_slot_id == revision.owner_slot_id,
                GithubAttemptScopeRevision.phase == "diagnostic",
                GithubAttemptScopeRevision.status == "active",
            )
        )
        claim = await db.execute(
            update(GithubWorkItem)
            .where(
                GithubWorkItem.id == item.id,
                GithubWorkItem.dispatch_status == "dispatched",
                GithubWorkItem.dispatch_nonce == revision.dispatch_nonce,
                GithubWorkItem.owner_slot_id == revision.owner_slot_id,
                GithubWorkItem.active_scope_revision == revision.revision,
                GithubWorkItem.attempt_phase == "diagnostic",
                current_revision,
                *(policy_context_conditions(policy_guard) if policy_guard is not None else ()),
            )
            .values(updated_at=GithubWorkItem.updated_at)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount == 1:
            return True
        await db.rollback()
        return False

    async def _observe_diagnostic_checks(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        revision: GithubAttemptScopeRevision,
        client: GithubClient,
    ) -> None:
        policy_guard = policy_context(item)
        policy = effective_policy(item, scope)
        pull = await client.get_pull(
            scope.repo_owner,
            scope.repo_name,
            int(item.pr_number),
        )
        try:
            if item.dispatch_base_ref is None:
                raise ValueError("prepared dispatch base is missing")
            expected_base = await self.normalize_base_ref(
                scope,
                client,
                token=None,
                base_ref=item.dispatch_base_ref,
            )
            self._verify_pull_identity(
                pull,
                scope,
                item,
                expected_base=expected_base,
            )
        except ValueError as exc:
            if not await self._claim_current_diagnostic_context(db, item, revision, policy_guard=policy_guard):
                return
            revision.status = "exhausted"
            await github_dispatch_service.escalate(
                db,
                item,
                "continuation_pr_identity_invalid",
                f"Diagnostic PR identity verification failed: {exc}",
            )
            await db.commit()
            return

        verdict = self._classify_pull(pull)
        if verdict == "merged":
            if not await self._claim_current_diagnostic_context(db, item, revision, policy_guard=policy_guard):
                return
            revision.status = "completed"
            revision.completed_at = datetime.utcnow()
            await self._mark_merged_with_fact(
                db, item, pull, source="github_verification_service._observe_diagnostic_checks")
            await self._record_revision_outcome(
                db, item, revision, "completed",
                source="github_verification_service._observe_diagnostic_checks")
            await db.commit()
            await self._notify_blocker_merged(db, scope, item)
            return
        if verdict != "open":
            if not await self._claim_current_diagnostic_context(db, item, revision, policy_guard=policy_guard):
                return
            revision.status = "superseded"
            await self._record_revision_outcome(
                db, item, revision, "superseded",
                source="github_verification_service._observe_diagnostic_checks")
            await github_dispatch_service.escalate_without_notification(
                db,
                item,
                "pr_closed_unmerged",
                f"PR #{item.pr_number} was closed without being merged.",
            )
            return

        head_sha = self._head_sha(pull)
        if not head_sha:
            if not await self._claim_current_diagnostic_context(db, item, revision, policy_guard=policy_guard):
                return
            revision.status = "exhausted"
            await github_dispatch_service.escalate(
                db,
                item,
                "continuation_pr_identity_invalid",
                "Diagnostic PR head is missing.",
            )
            await db.commit()
            return
        checks = await observe_tracked_checks(db, scope, item, client, head_sha, policy)
        signal = "check_runs"
        evidence_rows = self._diagnostic_check_evidence(checks)
        if checks:
            pending = [
                check
                for check in checks
                if check.get("status") != "completed"
                or check.get("conclusion") is None
            ]
            failed = [
                check
                for check in checks
                if check not in pending
                and check.get("conclusion") not in _SUCCESS_CONCLUSIONS | _INCONCLUSIVE_CONCLUSIONS
            ]
            inconclusive = [check for check in checks
                            if check.get("conclusion") in _INCONCLUSIVE_CONCLUSIONS]
            blockers = required_check_blockers(policy, checks, head_sha)
            state = "red" if failed else "pending" if pending or blockers or inconclusive else "green"
        elif policy.required_checks:
            state = "pending"
            evidence_rows = [{"name": check.name, "state": "missing"}
                             for check in policy.required_checks]
        else:
            combined = await client.get_combined_status_for_ref(
                scope.repo_owner,
                scope.repo_name,
                head_sha,
            )
            signal = "combined_status"
            evidence_rows = self._diagnostic_status_evidence(combined)
            combined_state = combined.get("state")
            if combined_state in _STATUS_FAILURE_STATES:
                state = "red"
            elif combined_state in _STATUS_SUCCESS_STATES and evidence_rows:
                state = "green"
            else:
                state = "pending"

        if not await self._claim_current_diagnostic_context(db, item, revision, policy_guard=policy_guard):
            return

        required_ci_attention.note_readable(item)
        envelope = dict(revision.evidence) if isinstance(revision.evidence, dict) else {}
        observations = dict(envelope.get("diagnostic_observations") or {})
        observation = {
            "head_sha": head_sha,
            "signal": signal,
            "state": state,
            "checks": evidence_rows,
        }
        if observations.get(head_sha) != observation:
            observations[head_sha] = observation
            envelope["version"] = 1
            envelope["diagnostic_observations"] = observations
            revision.evidence = envelope

        if state == "pending":
            inconclusive_rows = [row for row in evidence_rows
                if row.get("conclusion") in _INCONCLUSIVE_CONCLUSIONS]
            item.status_note = ("Diagnostic checks inconclusive: " + ", ".join(
                f"{row.get('name', 'unnamed')} ({row['conclusion']})" for row in inconclusive_rows)
                + ". No failed-head charge; required CI remains blocked."
                if inconclusive_rows else "Diagnostic checks are still running.")
            item.updated_at = datetime.utcnow()
            await db.commit()
            return
        if state == "green":
            item.status_note = (
                "Diagnostic checks are green; restore the baseline tree before completion."
            )
            item.updated_at = datetime.utcnow()
            await db.commit()
            return
        await db.flush()
        await self._record_diagnostic_failure(
            db,
            scope,
            item,
            revision,
            head_sha,
        )

    async def _record_diagnostic_failure(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        revision: GithubAttemptScopeRevision,
        head_sha: str,
    ) -> None:
        await db.refresh(item)
        await db.refresh(revision)
        if (
            item.dispatch_status != "dispatched"
            or item.active_scope_revision != revision.revision
            or item.dispatch_nonce != revision.dispatch_nonce
            or item.attempt_phase != "diagnostic"
            or revision.phase != "diagnostic"
            or revision.status != "active"
        ):
            raise ContinuationCompletionError("stale_continuation_context")
        if item.diagnostic_last_verified_sha != head_sha:
            total_failed_heads = int(
                (
                    await db.execute(
                        select(
                            func.coalesce(
                                func.sum(GithubAttemptScopeRevision.failed_head_count),
                                0,
                            )
                        ).where(
                            GithubAttemptScopeRevision.work_item_id == item.id,
                            GithubAttemptScopeRevision.dispatch_nonce
                            == item.dispatch_nonce,
                        )
                    )
                ).scalar_one()
            )
            revision.failed_head_count += 1
            revision.last_failed_head_sha = head_sha
            item.diagnostic_retry_count += 1
            # C12: diagnostic retry evidence, in the charge's transaction.
            from app.services import factory_audit_service as _audit
            await _audit.record_retry_charge(
                db, item_id=item.id, scope_id=item.scope_id, revision_id=revision.id,
                retry_class="diagnostic", counter_value=item.diagnostic_retry_count,
                reason_code="diagnostic_failed_head",
                source="github_verification_service._record_diagnostic_failure")
            item.diagnostic_last_verified_sha = head_sha
            item.status_note = "Diagnostic checks produced failure evidence."
            item.updated_at = datetime.utcnow()
            exhausted = (
                revision.failed_head_count >= revision.max_failed_heads
                or total_failed_heads + 1 >= scope.max_continuation_failed_heads
            )
            if exhausted:
                revision.status = "exhausted"
                await github_dispatch_service.escalate(
                    db,
                    item,
                    "continuation_budget_exhausted",
                    (
                        "Diagnostic failed-head budget was exhausted. Stop further "
                        "diagnostic iteration, restore the exact baseline, and report "
                        "`diagnostic_completed`."
                    ),
                )
            await db.commit()
        await self._notify_diagnostic_failure(db, item, revision, head_sha)

    async def _repair_exhausted_diagnostic_notifications(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        recovery_only_attempt: GithubRecoveryOnlyAttempt | None = None,
    ) -> None:
        rows = (
            await db.execute(
                select(GithubWorkItem, GithubAttemptScopeRevision)
                .join(
                    GithubAttemptScopeRevision,
                    (GithubAttemptScopeRevision.work_item_id == GithubWorkItem.id)
                    & (
                        GithubAttemptScopeRevision.dispatch_nonce
                        == GithubWorkItem.dispatch_nonce
                    )
                    & (
                        GithubAttemptScopeRevision.revision
                        == GithubWorkItem.active_scope_revision
                    ),
                )
                .where(
                    GithubWorkItem.scope_id == scope.id,
                    GithubWorkItem.dispatch_status == "escalated",
                    GithubWorkItem.escalation_reason
                    == "continuation_budget_exhausted",
                    GithubWorkItem.attempt_phase == "diagnostic",
                    GithubAttemptScopeRevision.phase == "diagnostic",
                    GithubAttemptScopeRevision.status == "exhausted",
                    GithubAttemptScopeRevision.last_failed_head_sha.is_not(None),
                    *(recovery_only_attempt.item_filters() if recovery_only_attempt else ()),
                )
            )
        ).all()
        for item, revision in rows:
            await self._notify_diagnostic_failure(
                db,
                item,
                revision,
                str(revision.last_failed_head_sha),
            )

    async def _notify_diagnostic_failure(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        revision: GithubAttemptScopeRevision,
        head_sha: str,
    ) -> None:
        restoration_required = (
            revision.status == "exhausted"
            and item.dispatch_status == "escalated"
            and item.escalation_reason == "continuation_budget_exhausted"
        )
        await github_dispatch_service.notify_owner(
            db,
            item,
            subject=(
                "Diagnostic budget exhausted; restore baseline"
                if restoration_required
                else "Diagnostic checks produced evidence"
            ),
            body_markdown=(
                (
                    "Stop further diagnostic iteration. The failed-head budget "
                    "is exhausted. Revert any temporary diagnostic changes, "
                    "restore the exact baseline, and report `diagnostic_completed` "
                    "with the authenticated owner session."
                )
                if restoration_required
                else (
                    f"Diagnostic checks failed for issue #{item.issue_number} / "
                    f"PR #{item.pr_number} at head {head_sha}."
                )
            ),
            payload={
                "kind": (
                    "github_dispatch_diagnostic_restoration_required"
                    if restoration_required
                    else "github_dispatch_diagnostic_check_failed"
                ),
                "work_item_id": item.id,
                "pr_number": item.pr_number,
                "head_sha": head_sha,
                "scope_revision": revision.revision,
                "diagnostic_retry_count": item.diagnostic_retry_count,
                "revision_failed_head_count": revision.failed_head_count,
            },
            delivery_key=(
                f"github-diagnostic:{revision.id}:restoration-required"
                if restoration_required
                else f"github-diagnostic:{revision.id}:check-failure:{head_sha}"
            ),
        )

    async def _preset_slots(
        self, db: AsyncSession, scope: TeamGithubScope
    ) -> list[AgentTeamSlot]:
        return (
            await db.execute(
                select(AgentTeamSlot)
                .where(AgentTeamSlot.preset_id == scope.preset_id)
                .order_by(AgentTeamSlot.position, AgentTeamSlot.id)
            )
        ).scalars().all()

    async def _notify_blocker_merged(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
    ) -> None:
        if configured_recovery_only_attempt() is not None:
            return
        try:
            slots = await self._preset_slots(db, scope)
            await github_dispatch_service.notify_blocker_merged(
                db, scope, item, slots
            )
            await db.commit()
        except Exception as caught_error:
            await recover_session(db, caught_error, discard=True)
            logger.exception(
                "Failed to send blocker-merged notification for work item %s",
                item.id,
            )

    async def _completion_head_is_current(self, db, scope, item, client, head_sha, *, revision=None):
        """Check remote identity again after fetching checks, before publishing a handoff."""
        current = await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
        if self._classify_pull(current) != "open" or self._head_sha(current) != head_sha:
            return False  # The next normal poll handles a changed or closed pull.
        return await self._validate_polled_pull_identity(db, scope, item, client, current,
            retry_status=item.dispatch_status, revision=revision)

    async def _verify_item(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        *,
        revision: GithubAttemptScopeRevision | None = None,
    ) -> None:
        ci_context = await github_ci_watch_service.capture(db, scope, item)
        policy_guard = policy_context(item)
        policy = effective_policy(item, scope)
        pull = await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
        if not await self._validate_polled_pull_identity(
            db,
            scope,
            item,
            client,
            pull,
            retry_status="dispatched",
            revision=revision,
        ):
            return
        verdict = self._classify_pull(pull)
        if verdict is None:
            note = f"PR #{item.pr_number} returned a state Albiex Synth City cannot classify."
            await self._record_product_verification_failure(
                db,
                scope,
                item,
                self._head_sha(pull) if revision is not None else None,
                note,
                subject="GitHub verification could not classify the PR",
                body_markdown=note,
                payload={
                    "kind": "github_dispatch_pr_unclassifiable",
                    "work_item_id": item.id,
                    "pr_number": item.pr_number,
                    "pull_state": pull.get("state"),
                },
                retry_status="dispatched",
                revision=revision,
            )
            return
        if verdict == "merged":
            if revision is not None and revision.status == "submitted" and revision.phase == "implementation":
                await self._settle_merged_submitted(
                    db, scope, item, client, revision, pull,
                    source="github_verification_service._verify_item")
                return
            if revision is not None:
                revision.status = "completed"
                revision.completed_at = datetime.utcnow()
                await self._record_revision_outcome(
                    db, item, revision, "completed",
                    source="github_verification_service._verify_item")
            await self._mark_merged_with_fact(db, item, pull, source="github_verification_service._verify_item")
            await db.commit()
            await self._notify_blocker_merged(db, scope, item)
            return
        if verdict == "closed_unmerged":
            if revision is not None:
                revision.status = "superseded"
                await self._record_revision_outcome(
                    db, item, revision, "superseded",
                    source="github_verification_service._verify_item")
            await github_dispatch_service.escalate_without_notification(
                db,
                item,
                "pr_closed_unmerged",
                f"PR #{item.pr_number} was closed without being merged.",
            )
            return

        if revision is not None and not await self._submitted_head_is_current(
            db,
            item,
            revision,
            self._head_sha(pull),
        ):
            return

        head_sha = self._head_sha(pull)
        if (
            item.verification_head_sha != head_sha
            or item.verification_started_at is None
        ):
            # Existing rows inherit their last pre-upgrade activity once. A new
            # observed head gets a full grace period; polling never renews it.
            if item.verification_head_sha is None:
                started_at = item.updated_at or item.created_at or datetime.utcnow()
            else:
                started_at = datetime.utcnow()
            item.verification_head_sha = head_sha
            item.verification_started_at = started_at
            await db.commit()
        checks = await observe_tracked_checks(db, scope, item, client, head_sha or "", policy)
        if ci_context is not None and policy.required_checks and not await self._completion_head_is_current(
                db, scope, item, client, head_sha, revision=revision):
            return
        if not await claim_policy_context(db, item, policy_guard):
            return
        if ci_context is not None:
            ci_context["verification_head_sha"] = item.verification_head_sha
        owner_notice_sent = await github_ci_watch_service.observe(
            db, scope, item, head_sha, checks, policy, context=ci_context)
        if owner_notice_sent is None:
            return  # The observation fence changed or its database snapshot was contended.
        required_ci_attention.note_readable(item)
        blockers = required_check_blockers(policy, checks, head_sha)
        if not checks and not policy.required_checks:
            if await self._process_combined_status(
                db,
                scope,
                item,
                client,
                pull,
                revision=revision,
                policy_guard=policy_guard,
            ):
                return
            await self._handle_no_check_signal(
                db,
                scope,
                item,
                head_sha,
                revision=revision,
            )
            return

        pending = [
            check
            for check in checks
            if check.get("status") != "completed" or check.get("conclusion") is None
        ]
        failed = [
            check
            for check in checks
            if check not in pending
            and check.get("conclusion") not in _SUCCESS_CONCLUSIONS | _INCONCLUSIVE_CONCLUSIONS
        ]
        if failed:
            await self._record_product_verification_failure(
                db,
                scope,
                item,
                head_sha,
                self._failed_check_note(failed),
                owner_notice_sent=owner_notice_sent,
                subject="GitHub checks failed",
                body_markdown=(
                    f"GitHub checks failed for issue #{item.issue_number} / "
                    f"PR #{item.pr_number}.\n\n{self._failed_check_note(failed)}"
                ),
                payload={
                    "kind": "github_dispatch_check_failed",
                    "work_item_id": item.id,
                    "pr_number": item.pr_number,
                },
                retry_status="dispatched",
                revision=revision,
            )
            return
        inconclusive = [check for check in checks
                        if check.get("conclusion") in _INCONCLUSIVE_CONCLUSIONS]
        if inconclusive:
            names = ", ".join(f"{check.get('name') or check.get('id')} ({check['conclusion']})"
                              for check in inconclusive)
            item.status_note = (
                f"GitHub checks inconclusive: {names}. Required CI remains blocked; "
                "no product failed head was charged."
            )
            item.updated_at = datetime.utcnow()
            await db.commit()
            return
        if pending:
            item.status_note = "GitHub checks are still running."
            item.updated_at = datetime.utcnow()
            await db.commit()
            return
        if blockers:
            item.status_note = "Required GitHub checks blocked: " + "; ".join(blockers)
            item.updated_at = datetime.utcnow()
            await db.commit()
            return
        if checks and all(check.get("conclusion") in _SUCCESS_CONCLUSIONS for check in checks):
            await self._promote_verified_item(
                db,
                scope,
                item,
                client,
                pull,
                head_sha,
                revision=revision,
                policy_guard=policy_guard,
            )

    async def _process_review_item(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        pull: dict | None = None,
        *,
        superseded_ok: bool = False,
    ) -> None:
        # Before any awaited read: the merge boundary compares the settled revision with this, not with its state then.
        settled_began = await self._settled_snapshot(db, scope, item)
        fence = None
        if superseded_ok:
            began = {name: getattr(item, name) for name in _MERGE_FENCE_NOTE_IDENTITY}
            fence = await self._capture_superseded_fence(db, scope, item)
            if isinstance(fence, str):  # a refusal: say why once and stop
                if item.status_note != fence:
                    await self._record_fenced_note(db, item, began, fence)
                return
        ci_context = await github_ci_watch_service.capture(db, scope, item)
        pull = pull or await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
        if not await self._validate_polled_pull_identity(
            db,
            scope,
            item,
            client,
            pull,
            retry_status=item.dispatch_status,
        ):
            return
        verdict = self._classify_pull(pull)
        if verdict is None:
            note = f"PR #{item.pr_number} returned a state Albiex Synth City cannot classify."
            await self._record_failed_verification_attempt(
                db,
                scope,
                item,
                None,
                note,
                subject="GitHub review could not classify the PR",
                body_markdown=note,
                payload={
                    "kind": "github_dispatch_pr_unclassifiable",
                    "work_item_id": item.id,
                    "pr_number": item.pr_number,
                    "pull_state": pull.get("state"),
                },
                retry_status=item.dispatch_status,
            )
            return
        if verdict == "merged":
            await self._mark_merged_with_fact(
                db, item, pull, source="github_verification_service._process_review_item")
            await db.commit()
            await self._notify_blocker_merged(db, scope, item)
            return
        if verdict == "closed_unmerged":
            await github_dispatch_service.escalate_without_notification(
                db,
                item,
                "pr_closed_unmerged",
                f"PR #{item.pr_number} was closed without being merged.",
            )
            return
        if item.issue_type == "code" and scope.merge_policy == "human":
            current_head = self._head_sha(pull)
            if not current_head or current_head != item.last_verified_sha:
                scoped_completion = item.active_scope_revision > 0
                note = (
                    "The PR head changed after scoped completion; request a new "
                    "approved continuation before verifying the changed head."
                    if scoped_completion
                    else "The PR head changed since review readiness; verifying "
                    "the current head before requesting fresh human review."
                )
                notice_context = (
                    item.dispatch_nonce, item.owner_slot_id, item.pr_number,
                    item.active_scope_revision,
                )
                claimed = await db.execute(
                    update(GithubWorkItem)
                    .where(
                        GithubWorkItem.id == item.id,
                        GithubWorkItem.scope_id == scope.id,
                        GithubWorkItem.dispatch_status == item.dispatch_status,
                        GithubWorkItem.dispatch_status.in_(
                            ("ready_for_review", "awaiting_human_review")
                        ),
                        GithubWorkItem.dispatch_nonce == item.dispatch_nonce,
                        GithubWorkItem.owner_slot_id == item.owner_slot_id,
                        GithubWorkItem.pr_number == item.pr_number,
                        GithubWorkItem.active_scope_revision == item.active_scope_revision,
                        GithubWorkItem.last_verified_sha == item.last_verified_sha,
                        exists(select(TeamGithubScope.id).where(
                            TeamGithubScope.id == scope.id,
                            TeamGithubScope.merge_policy == "human",
                        )),
                    )
                    .values(
                        dispatch_status="escalated" if scoped_completion else "verifying",
                        escalation_reason="plan_blocked" if scoped_completion else None,
                        status_note=note,
                        updated_at=datetime.utcnow(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if claimed.rowcount != 1:
                    await db.rollback()
                    await db.refresh(item)
                    return
                await db.commit()
                await db.refresh(item)
                if scoped_completion and item.dispatch_status == "escalated" and (
                    item.dispatch_nonce, item.owner_slot_id, item.pr_number,
                    item.active_scope_revision,
                ) == notice_context:
                    await github_dispatch_service.notify_owner(
                        db, item,
                        subject="Changed PR head requires scoped approval",
                        body_markdown=note,
                        payload={
                            "kind": "github_review_head_requires_continuation",
                            "work_item_id": item.id,
                            "pr_number": item.pr_number,
                            "scope_revision": notice_context[3],
                            "head_sha": current_head,
                        },
                    )
                    await db.commit()
            elif (policy := effective_policy(item, scope)).required_checks:
                # Human review can remain open across reruns. Keep its check view
                # current in the normal monitor, without polling from UI reads.
                checks = await observe_tracked_checks(db, scope, item, client, current_head, policy)
                if ci_context is not None and not await self._completion_head_is_current(
                        db, scope, item, client, current_head):
                    return
                await github_ci_watch_service.observe(db, scope, item, current_head, checks,
                    effective_policy(item, scope), context=ci_context)
            return
        if item.issue_type == "design" or scope.merge_policy != "auto":
            return
        if item.status_note and item.status_note.startswith(_HUMAN_MERGE_NOTE_PREFIXES):
            cleared_note = await self._cleared_fallback_note(db, scope, item, client, pull)
            if cleared_note is None:
                return
            item.status_note = cleared_note
            item.merge_wait_head_sha = None
            item.merge_wait_started_at = None
            item.updated_at = datetime.utcnow()
            await db.commit()
        readiness_note = self._draft_readiness_note(item, pull)
        if readiness_note is not None:
            await self._record_readiness_hold(db, item, readiness_note)
            return
        hold_note = await self._review_hold_note(scope, item, client, pull)
        if hold_note is not None:
            await self._record_readiness_hold(db, item, hold_note)
            return
        # The operation's identity, captured once: a fact for another PR or attempt cannot authorize it later.
        acceptance_identity = (item.pr_number, item.dispatch_nonce, item.scope_id)
        acceptance_note = await self._acceptance_note(db, scope, item, pull, acceptance_identity)
        if acceptance_note is not None:
            await self._record_readiness_hold(db, item, acceptance_note)
            return
        approval_reason = await self._approval_gate_reason(db, scope, item, acceptance_identity)
        if approval_reason is not None:
            await self._fallback_to_human_merge(
                db,
                item,
                "Auto-merge blocked: distinct current-round leader approval is "
                f"required ({approval_reason}).",
            )
            return
        used, cap = await self._auto_merge_usage(db, scope)
        if used >= cap:
            detail = f"{used} of {cap} automatic merges in the last 24 hours"
            not_fired = self._not_fired("auto_merge_cap_reached", detail, self._head_sha(pull))
            await self._fallback_to_human_merge(
                db,
                item,
                f"Auto-merge budget exhausted; PR is ready for human merge. [auto_merge_cap_reached: {detail}]",
                not_fired=not_fired,
                scope=scope,
            )
            return
        current_head = self._head_sha(pull)
        if not current_head or current_head != item.last_verified_sha:
            item.dispatch_status = "verifying"
            item.status_note = "Head changed since promotion; re-verifying before auto-merge."
            item.updated_at = datetime.utcnow()
            await db.commit()
            return
        if await self._wait_for_merge_checks(db, scope, item, client, current_head):
            return
        merge_state = pull.get("mergeable_state")
        if merge_state in _TRANSIENT_MERGE_STATES:
            await self._record_transient_merge_failure(
                db,
                scope,
                item,
                f"mergeable_state={merge_state}",
            )
            return
        if not await self._head_is_green(scope, client, current_head, item=item):
            # Preserve the wait bound if another check started during this read.
            if await self._wait_for_merge_checks(db, scope, item, client, current_head):
                return
            failed_statuses = await self._failed_commit_statuses(scope, client, current_head)
            item.dispatch_status = "verifying"
            item.status_note = (
                f"Auto-merge did not fire: commit status failed on head {current_head}: "
                f"{', '.join(failed_statuses)}. Re-verifying before auto-merge."
                if failed_statuses else
                "Head changed or is no longer green since promotion; re-verifying "
                "before auto-merge."
            )
            item.updated_at = datetime.utcnow()
            await db.commit()
            return
        # Owner readiness and labels can change while checks are read.
        try:
            fresh_pull = await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
        except GithubRateLimited:
            raise
        except (httpx.HTTPError, GithubClientResponseError):
            await self._record_readiness_hold(
                db, item, "PR readiness could not be read; automatic merge is blocked.")
            return
        if not await self._validate_polled_pull_identity(
            db, scope, item, client, fresh_pull, retry_status=item.dispatch_status,
        ):
            return
        if self._classify_pull(fresh_pull) != "open":
            await self._process_review_item(db, scope, item, client, pull=fresh_pull, superseded_ok=superseded_ok)
            return
        if self._head_sha(fresh_pull) != current_head:
            item.dispatch_status = "verifying"
            await self._record_readiness_hold(
                db, item, "Head changed before auto-merge; verifying the current head.")
            return
        readiness_note = self._draft_readiness_note(item, fresh_pull)
        if readiness_note is not None:
            await self._record_readiness_hold(db, item, readiness_note)
            return
        hold_note = await self._review_hold_note(scope, item, client, fresh_pull)
        if hold_note is not None:
            await self._record_readiness_hold(db, item, hold_note)
            return
        acceptance_note = await self._acceptance_note(db, scope, item, fresh_pull, acceptance_identity)
        if acceptance_note is not None:
            await self._record_readiness_hold(db, item, acceptance_note)
            return
        approval_reason = await self._approval_gate_reason(db, scope, item, acceptance_identity)
        if approval_reason is not None:
            await self._fallback_to_human_merge(
                db, item, "Auto-merge blocked: distinct current-round leader approval is "
                f"required ({approval_reason}).")
            return
        withdrawn = await self._settled_context_note(
            db, scope, item, current_head, settled_began, superseded_ok=superseded_ok)
        if withdrawn is not None:
            await self._record_readiness_hold(db, item, withdrawn)
            return
        # #1310: the attempt is durable before each path's final checks, which stay the last before the call.
        attempt_id = await merge_attempts.begin(
            db, scope_id=scope.id, item_id=item.id, pr_number=int(item.pr_number), head_sha=current_head,
            repository=f"{scope.repo_owner}/{scope.repo_name}")
        await db.commit()
        if fence is None:
            if await self._ordinary_final_checks_stop(
                    db, scope, item, fresh_pull, acceptance_identity, current_head, settled_began,
                    superseded_ok=superseded_ok, attempt_id=attempt_id):
                return
        else:
            # The last await before the merge call is this one statement; the HOLD read and the call follow with none.
            if not await db.scalar(select(self._superseded_fence_holds(scope, item, fence))):
                await db.rollback()
                await db.refresh(scope)
                await self._refuse_attempt(db, attempt_id, "authority_changed")
                logger.info("Authority changed before the superseded-revision merge; nothing was merged")
                return
            from app.services.github_coordination_service import hold_code
            if hold_code():
                await db.rollback()
                await self._refuse_attempt(db, attempt_id, "hold")
                return
        try:
            await client.merge_pull(
                scope.repo_owner,
                scope.repo_name,
                int(item.pr_number),
                expected_head_sha=current_head,
            )
        except GithubRateLimited as exc:
            await self._record_attempt_failure(db, attempt_id, exc)
            raise
        except httpx.HTTPStatusError as exc:
            await self._record_attempt_failure(db, attempt_id, exc)
            status_code = exc.response.status_code
            if status_code in _MERGE_TRANSIENT_STATUS_CODES or status_code >= 500:
                # Checks can start after the green read and before the merge.
                if not await self._wait_for_merge_checks(db, scope, item, client, current_head):
                    await self._record_transient_merge_failure(db, scope, item, str(exc))
            elif status_code == 403:
                await self._fallback_to_human_merge(
                    db,
                    item,
                    "Auto-merge blocked by repository policy; requires human merge.",
                )
            else:
                await self._fallback_to_human_merge(
                    db,
                    item,
                    f"Auto-merge failed with GitHub status {status_code}; requires human merge.",
                )
            return
        except BaseException as exc:
            # Transport, malformed answers or a stop during the call: recorded, then handled as before.
            await self._record_attempt_failure(db, attempt_id, exc)
            raise

        # The merge response carries no merge time, so the sourced fact time
        # stays unknown (null); the record time is the observation time.
        await self._mark_merged_with_fact(
            db, item, {"number": item.pr_number},
            source="github_verification_service._process_review_item.auto_merge")
        item.auto_merged_at = datetime.utcnow()
        await merge_attempts.finish(db, attempt_id, merge_attempts.MERGED, None)
        await db.commit()
        await self._notify_blocker_merged(db, scope, item)

    async def _ordinary_final_checks_stop(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, pull: dict, acceptance_identity,
        head: str, settled_began, *, superseded_ok: bool, attempt_id: int,
    ) -> bool:
        """#1310: the ordinary path's final authority checks, run again once the attempt is durable (V1 review
        6074761059). Each keeps its handling; a stop refuses the attempt. Whether the merge must stop."""
        note = await self._acceptance_note(db, scope, item, pull, acceptance_identity)
        if note is not None:
            await self._record_readiness_hold(db, item, note)
            await self._refuse_attempt(db, attempt_id, "acceptance_changed")
            return True
        reason = await self._approval_gate_reason(db, scope, item, acceptance_identity)
        if reason is not None:
            await self._fallback_to_human_merge(
                db, item, "Auto-merge blocked: distinct current-round leader approval is "
                f"required ({reason}).")
            await self._refuse_attempt(db, attempt_id, "approval_changed")
            return True
        withdrawn = await self._settled_context_note(db, scope, item, head, settled_began, superseded_ok=superseded_ok)
        if withdrawn is not None:
            await self._record_readiness_hold(db, item, withdrawn)
            await self._refuse_attempt(db, attempt_id, "settlement_changed")
            return True
        return False

    async def _refuse_attempt(self, db: AsyncSession, attempt_id: int, reason: str) -> None:
        """#1310: a fence stopped the recorded attempt before the call."""
        await merge_attempts.finish(db, attempt_id, merge_attempts.REFUSED, reason)
        await db.commit()

    async def _record_attempt_failure(self, db: AsyncSession, attempt_id: int, error: BaseException) -> None:
        """#1310: what a failed merge call proves, committed at once: an error that propagates may meet a rollback,
        and nothing else is pending since the attempt's own commit."""
        if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            return  # a stop during the call: the attempt stays unfinished, which reads as uncertain
        status, reason = merge_attempts.outcome(error)
        await merge_attempts.finish(db, attempt_id, status, reason)
        await db.commit()

    async def _capture_superseded_fence(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, head: str | None = None,
    ) -> dict | str:
        """What the superseded-revision merge rests on, read before any awaited GitHub read, or why it cannot go ahead (#972).

        The item, the scope target, the preset's Leader slot, the exact revision row, the accepting review and the
        workspace lease are read as plain columns. The canonical Leader settlement keeps the lease and clears the
        escalation reason, so a missing lease or a set escalation reason is not that path and is refused precisely.
        """
        item_row = (await db.execute(
            select(*(getattr(GithubWorkItem, name) for name in _MERGE_FENCE_ITEM),
                   GithubWorkItem.handoff_state, GithubWorkItem.escalation_reason,
                   GithubWorkItem.delivery_policy, GithubWorkItem.delivery_policy_revision)
            .where(GithubWorkItem.id == item.id)
        )).mappings().first()
        if item_row is None:
            return "Auto-merge blocked: the work item could not be read."
        if item_row["handoff_state"] is not None or item_row["escalation_reason"] is not None:
            return "Auto-merge blocked: the item is in a handoff or an escalation, which this path does not cover."
        scope_row = (await db.execute(
            select(*(getattr(TeamGithubScope, name) for name in _MERGE_FENCE_SCOPE))
            .where(TeamGithubScope.id == scope.id))).mappings().first()
        revision = (await db.execute(
            select(GithubAttemptScopeRevision.__table__)
            .where(GithubAttemptScopeRevision.work_item_id == item.id,
                   GithubAttemptScopeRevision.dispatch_nonce == item_row["dispatch_nonce"],
                   GithubAttemptScopeRevision.revision == item_row["active_scope_revision"],
                   GithubAttemptScopeRevision.status == "superseded",
                   GithubAttemptScopeRevision.phase == "implementation")
        )).mappings().first()
        if scope_row is None or revision is None:
            return "Auto-merge blocked: the superseded revision or the scope could not be read."
        lease = (await db.execute(
            select(GithubWorkspace.id, GithubWorkspace.lease_token, GithubWorkspace.leased_at)
            .where(GithubWorkspace.id == revision["expected_workspace_id"], GithubWorkspace.leased_item_id == item.id,
                   GithubWorkspace.scope_id == scope.id, GithubWorkspace.enabled.is_(True))
        )).mappings().first()
        if lease is None or not lease["lease_token"]:
            return ("Auto-merge blocked: the item's workspace lease is missing, released or replaced; "
                    "a superseded revision is promoted only while its owner still holds the lease.")
        if not (isinstance(revision["expected_lease_token_hash"], str)
                and github_approval_service.lease_token_matches(lease["lease_token"],
                                                                revision["expected_lease_token_hash"])):
            return ("Auto-merge blocked: the workspace was acquired again and its lease is not the one this revision "
                    "was approved with; a superseded revision is promoted only while its owner holds that lease.")
        leader_slot_id = await db.scalar(select(AgentTeamPreset.leader_slot_id)
                                         .where(AgentTeamPreset.id == scope_row["preset_id"]))
        review = (await db.execute(
            select(GithubHeadReview.id, GithubHeadReview.reviewer_member_id).where(
                GithubHeadReview.work_item_id == item.id, GithubHeadReview.dispatch_nonce == item_row["dispatch_nonce"],
                GithubHeadReview.pr_number == item_row["pr_number"], GithubHeadReview.status == "active",
                GithubHeadReview.head_sha == (head or item_row["last_verified_sha"]),
                GithubHeadReview.verdict == "accepted",
            ).order_by(GithubHeadReview.id.desc()).limit(1))).mappings().first()
        if review is None:
            return "Auto-merge blocked: no active acceptance of the verified head could be read."
        return {"item": dict(item_row), "scope": dict(scope_row), "revision": dict(revision), "lease": dict(lease),
                "leader_slot_id": leader_slot_id, "review": dict(review)}

    @staticmethod
    def _superseded_fence_holds(scope: TeamGithubScope, item: GithubWorkItem, fence: dict):
        """One statement that is true only while everything captured is unchanged and no pause or approval is open.

        A single SQL statement is coherent where several awaited reads are not, so a change cannot fall between its
        parts. It adds what the captured values do not say: the auto policy and autonomy on, the Leader slot still
        enabled, the accepting reviewer still the current verifier, and no waiting or paused observation or pending
        approval request.
        """
        began = fence["item"]
        item_ok = exists(select(GithubWorkItem.id).where(
            *(_same_value(getattr(GithubWorkItem, name), began[name]) for name in _MERGE_FENCE_ITEM),
            GithubWorkItem.handoff_state.is_(None), GithubWorkItem.escalation_reason.is_(None),
            *policy_context_conditions((began["delivery_policy_revision"], began["delivery_policy"])),
        ))
        scope_ok = exists(select(TeamGithubScope.id).join(
            AgentTeamPreset, AgentTeamPreset.id == TeamGithubScope.preset_id).where(
            *(_same_value(getattr(TeamGithubScope, name), fence["scope"][name]) for name in _MERGE_FENCE_SCOPE),
            TeamGithubScope.enabled.is_(True), TeamGithubScope.merge_policy == "auto",
            AgentTeamPreset.autonomy_enabled.is_(True),
            _same_value(AgentTeamPreset.leader_slot_id, fence["leader_slot_id"]),
        ))
        leader_ok = exists(select(AgentTeamSlot.id).where(
            AgentTeamSlot.id == fence["leader_slot_id"], AgentTeamSlot.enabled.is_(True)))
        revision_ok = exists(select(GithubAttemptScopeRevision.id).where(
            GithubAttemptScopeRevision.status == "superseded",
            *(_same_value(column, fence["revision"][column.name]) for column in GithubAttemptScopeRevision.__table__.columns)))
        lease = fence["lease"]
        lease_ok = exists(select(GithubWorkspace.id).where(
            GithubWorkspace.id == lease["id"], GithubWorkspace.leased_item_id == item.id,
            GithubWorkspace.scope_id == scope.id, GithubWorkspace.enabled.is_(True),
            GithubWorkspace.lease_token == lease["lease_token"], _same_value(GithubWorkspace.leased_at, lease["leased_at"])))
        review_ok = exists(
            select(GithubHeadReview.id)
            .join(MailTeamMember, MailTeamMember.id == GithubHeadReview.reviewer_member_id)
            .join(AgentTeamSlot, AgentTeamSlot.id == MailTeamMember.team_slot_id)
            .where(GithubHeadReview.id == fence["review"]["id"], GithubHeadReview.status == "active",
                   GithubHeadReview.verdict == "accepted", AgentTeamSlot.enabled.is_(True),
                   MailTeamMember.team_preset_id == scope.preset_id, AgentTeamSlot.preset_id == scope.preset_id,
                   func.lower(func.trim(AgentTeamSlot.role)) == "verifier",
                   AgentTeamSlot.id != began["owner_slot_id"],
                   AgentTeamSlot.id != fence["leader_slot_id"],
                   # the accepting member is still the current occupant of its slot
                   MailTeamMember.id == _latest_member(MailTeamMember.team_slot_id)))
        # the approver is the current Leader-slot member, the owner slot is enabled with a current member, and the
        # two are distinct people
        occupants_ok = exists(select(GithubWorkItem.id).where(
            GithubWorkItem.id == item.id,
            GithubWorkItem.ack_approver_member_id == _latest_member(fence["leader_slot_id"]),
            _latest_member(GithubWorkItem.owner_slot_id).is_not(None),
            GithubWorkItem.ack_approver_member_id != _latest_member(GithubWorkItem.owner_slot_id),
            exists(select(AgentTeamSlot.id).where(
                AgentTeamSlot.id == GithubWorkItem.owner_slot_id, AgentTeamSlot.enabled.is_(True),
                AgentTeamSlot.preset_id == scope.preset_id)),
        ))
        quiet = ~exists(select(GithubOwnerObservationPause.id).where(
            GithubOwnerObservationPause.work_item_id == item.id,
            GithubOwnerObservationPause.status.in_(("waiting", "paused"))))
        no_pending = ~exists(select(GithubApprovalRequest.id).where(
            GithubApprovalRequest.work_item_id == item.id, GithubApprovalRequest.status == "pending"))
        return and_(item_ok, scope_ok, leader_ok, revision_ok, lease_ok, review_ok, occupants_ok, quiet, no_pending)

    @staticmethod
    async def _record_fenced_note(db: AsyncSession, item: GithubWorkItem, began: dict, note: str) -> None:
        """Write a refusal note only while the attempt it was read for is still the one on the item (#972).

        The note is decided after awaited reads; a new dispatch that replaced the item meanwhile keeps its own note.
        """
        claim = await db.execute(update(GithubWorkItem).where(
            *(_same_value(getattr(GithubWorkItem, name), value) for name, value in began.items()),
        ).values(status_note=note, updated_at=datetime.utcnow()).execution_options(synchronize_session=False))
        if claim.rowcount != 1:
            await db.rollback()
            return
        await db.commit()
        await db.refresh(item)

    @staticmethod
    def _draft_readiness_note(item: GithubWorkItem, pull: dict) -> str | None:
        if type(pull.get("draft")) is not bool:
            return "PR draft state could not be read; automatic merge is blocked."
        if pull["draft"]:
            return f"PR #{item.pr_number} is draft. The owner must mark it ready."
        return None

    @staticmethod
    async def _record_readiness_hold(db: AsyncSession, item: GithubWorkItem, note: str) -> None:
        item.status_note = note
        item.updated_at = datetime.utcnow()
        await db.commit()

    async def _acceptance_note(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, pull: dict, identity=None,
    ) -> str | None:
        """Auto-merge needs the team's verifier to have accepted this exact head; it fails closed."""
        from app.services import github_head_acceptance_service as acceptance
        head = self._head_sha(pull)
        if not head:
            # An unreadable head is demoted to re-verification by the head check that follows; it cannot merge.
            return None
        return await acceptance.gate_note(db, scope, item, head, identity)

    async def _review_hold_note(
        self, scope: TeamGithubScope, item: GithubWorkItem, client: GithubClient, pull: dict,
    ) -> str | None:
        label = scope.review_hold_label
        if label is None:
            return None
        unknown = "Review hold labels could not be read; automatic merge is blocked."
        if not isinstance(label, str) or not label.strip():
            return "Review hold label is invalid; automatic merge is blocked."
        try:
            issue = await client.get_issue(scope.repo_owner, scope.repo_name, item.issue_number)
        except GithubRateLimited:
            raise
        except (httpx.HTTPError, GithubClientResponseError):
            return unknown
        if not isinstance(issue, dict) or type(issue.get("number")) is not int or issue["number"] != item.issue_number:
            return unknown
        for record in (pull, issue):
            labels = record.get("labels")
            if not isinstance(labels, list) or any(
                not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                for entry in labels
            ):
                return unknown
        if any(entry["name"] == label for record in (pull, issue) for entry in record["labels"]):
            return f"PR #{item.pr_number} is held for independent review. Hold label: {label}."
        return None

    async def _validate_polled_pull_identity(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        pull: dict,
        *,
        retry_status: str,
        revision: GithubAttemptScopeRevision | None = None,
    ) -> bool:
        try:
            if item.dispatch_base_ref is None:
                raise ValueError("prepared dispatch base is missing")
            expected_base = await self.normalize_base_ref(
                scope,
                client,
                token=None,
                base_ref=item.dispatch_base_ref,
            )
            self._verify_pull_identity(
                pull,
                scope,
                item,
                expected_base=expected_base,
            )
        except ValueError as exc:
            note = f"PR #{item.pr_number} identity verification failed: {exc}"
            await self._record_product_verification_failure(
                db,
                scope,
                item,
                self._head_sha(pull) if revision is not None else None,
                note,
                subject="GitHub pull request identity verification failed",
                body_markdown=note,
                payload={
                    "kind": "github_dispatch_pr_identity_invalid",
                    "work_item_id": item.id,
                    "pr_number": item.pr_number,
                },
                retry_status=retry_status,
                revision=revision,
            )
            return False
        return True

    async def _approval_gate_reason(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        identity=None,
    ) -> str | None:
        """Why the Leader approval does not authorize this merge, or ``None`` when it does.

        The approval and owner columns and the preset's Leader slot are read fresh with column-level queries, so a
        change committed by another connection while GitHub was awaited is seen at the merge boundary. ``identity``
        is the (PR number, dispatch nonce, scope id) the operation started with.
        """
        if not settings.mail_capability_tokens_required:
            return "capability token enforcement is disabled"
        fresh = (await db.execute(select(
            GithubWorkItem.ack_enforcement_epoch, GithubWorkItem.ack_approval_round,
            GithubWorkItem.approval_round_count, GithubWorkItem.ack_approver_member_id,
            GithubWorkItem.owner_slot_id, GithubWorkItem.pr_number, GithubWorkItem.dispatch_nonce,
            GithubWorkItem.scope_id).where(GithubWorkItem.id == item.id))).one_or_none()
        if fresh is None:
            return "the work item could not be read"
        if identity is not None and (fresh.pr_number, fresh.dispatch_nonce, fresh.scope_id) != tuple(identity):
            await db.refresh(item)
            return "the pull request or attempt changed during verification"
        if fresh.ack_enforcement_epoch != 1:
            return "approval was not recorded under enforced identity"
        if fresh.ack_approval_round != fresh.approval_round_count:
            return "approval is missing or belongs to a stale round"
        leader_slot_id = (await db.execute(select(AgentTeamPreset.leader_slot_id)
            .where(AgentTeamPreset.id == scope.preset_id))).scalar_one_or_none()
        leader_slot = (
            await db.execute(
                select(AgentTeamSlot)
                .where(
                    AgentTeamSlot.preset_id == scope.preset_id,
                    AgentTeamSlot.id == leader_slot_id,
                    AgentTeamSlot.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        if leader_slot is None:
            return "no enabled leader slot exists"
        leader_member = await github_dispatch_service._slot_member(db, leader_slot.id)
        if leader_member is None or fresh.ack_approver_member_id != leader_member.id:
            return "approver is not the current designated leader"
        owner_member = (await github_dispatch_service._slot_member(db, fresh.owner_slot_id)
                        if fresh.owner_slot_id is not None else None)
        if owner_member is None:
            return "current owner is not registered"
        if owner_member.id == fresh.ack_approver_member_id:
            return "owner and approver are not distinct"
        return None

    async def _wait_for_merge_checks(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        head_sha: str,
    ) -> bool:
        """Wait for head checks without charging implementation retries."""
        ci_context = await github_ci_watch_service.capture(db, scope, item)
        policy = effective_policy(item, scope)
        checks = await observe_tracked_checks(db, scope, item, client, head_sha, policy)
        if ci_context is not None and policy.required_checks and not await self._completion_head_is_current(
                db, scope, item, client, head_sha):
            return True
        observed = await github_ci_watch_service.observe(
            db, scope, item, head_sha, checks, policy, context=ci_context)
        if observed is None:
            return True
        status = await client.get_combined_status_for_ref(
            scope.repo_owner, scope.repo_name, head_sha,
        )
        required_ci_attention.note_readable(item)
        pending = self._pending_merge_checks(checks, status, head_sha)
        if not pending:
            # A later pending period on this head starts its own wait (#1037).
            item.merge_wait_head_sha = None
            item.merge_wait_started_at = None
            return False
        now = datetime.utcnow()
        if item.merge_wait_head_sha != head_sha or item.merge_wait_started_at is None:
            item.merge_wait_head_sha = head_sha
            item.merge_wait_started_at = now
        names = ", ".join(pending)
        if now - item.merge_wait_started_at >= _MERGE_CHECK_WAIT_LIMIT:
            await self._fallback_to_human_merge(
                db,
                item,
                f"Auto-merge blocked: GitHub checks stalled for 60 minutes on head {head_sha}. "
                f"Pending checks: {names}.",
            )
        else:
            item.status_note = f"Auto-merge waiting for checks on head {head_sha}: {names}."
            item.updated_at = now
        await db.commit()
        return True

    @staticmethod
    def _pending_merge_checks(checks: list[dict], status: dict, head_sha: str) -> list[str]:
        """The names of the head's check runs and commit statuses that have not finished."""
        pending = [
            str(check.get("name") or check.get("id") or "unnamed check")
            for check in checks
            if check.get("head_sha") in (None, head_sha)
            and (check.get("status") != "completed" or check.get("conclusion") is None)
        ]
        contexts = status.get("statuses") or []
        pending_contexts = [
            str(context.get("context") or "unnamed commit status")
            for context in contexts
            if context.get("state") == "pending"
        ]
        pending.extend(pending_contexts)
        # Combined success covers every context, including paged-out contexts.
        # Failure can hide pending contexts. Incomplete evidence cannot charge
        # a retry. An empty aggregate pending response means no status exists.
        total_contexts = status.get("total_count", len(contexts))
        if status.get("state") == "pending" and total_contexts and not pending_contexts:
            pending.append("pending commit status contexts")
        elif total_contexts > len(contexts) and status.get("state") != "success":
            pending.append("unobserved commit status contexts")
        return pending

    async def _cleared_fallback_note(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, client: GithubClient, pull: dict,
    ) -> str | None:
        """The note for a human-merge fallback whose cause has cleared, or ``None`` while it stays with a human (#1037).

        Only two causes clear by themselves: the daily cap, once usage is below it, and a check stall, once every check
        and status on the same verified head has finished green. Every other fallback needs a person. A cleared item
        goes back through every gate of the automatic merge path.
        """
        note = item.status_note or ""
        if note.startswith(_CAP_FALLBACK_PREFIX):
            used, cap = await self._auto_merge_usage(db, scope)
            if used < cap:
                return f"Auto-merge offered again: {used} of {cap} automatic merges in the last 24 hours."
            return None
        if note.startswith(_STALL_FALLBACK_PREFIX):
            head = self._head_sha(pull)
            if not head or head != item.merge_wait_head_sha:
                return None
            checks = await observe_tracked_checks(db, scope, item, client, head, effective_policy(item, scope))
            status = await client.get_combined_status_for_ref(scope.repo_owner, scope.repo_name, head)
            if self._pending_merge_checks(checks, status, head):
                return None
            # A finished red status is neither pending nor a check run; any status present must have succeeded.
            if (status.get("total_count") or status.get("statuses")) and status.get("state") != "success":
                return None
            if not await self._head_is_green(scope, client, head, item=item):
                return None
            return f"Auto-merge offered again: the checks on head {head} finished."
        return None

    async def _record_transient_merge_failure(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        note: str,
    ) -> None:
        item.retry_count += 1
        # C12: implementation retry evidence, in the charge's transaction.
        from app.services import factory_audit_service as _audit
        await _audit.record_retry_charge(
            db, item_id=item.id, scope_id=item.scope_id, revision_id=None,
            retry_class="implementation", counter_value=item.retry_count,
            reason_code="transient_merge_failure",
            source="github_verification_service._record_transient_merge_failure")
        if item.retry_count > scope.max_verification_retries:
            await self._fallback_to_human_merge(
                db,
                item,
                f"Auto-merge retry budget exhausted after transient merge failure: {note}",
            )
        else:
            self._set_failure_note(
                item, f"Transient merge failure; will retry: {note}"
            )
            item.updated_at = datetime.utcnow()
        await db.commit()

    async def _process_combined_status(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        pull: dict,
        *,
        revision: GithubAttemptScopeRevision | None = None,
        policy_guard: tuple | None = None,
    ) -> bool:
        policy_guard = policy_guard if policy_guard is not None else policy_context(item)
        head_sha = self._head_sha(pull)
        status = await client.get_combined_status_for_ref(
            scope.repo_owner,
            scope.repo_name,
            head_sha or "",
        )
        if not await claim_policy_context(db, item, policy_guard):
            return True
        required_ci_attention.note_readable(item)
        contexts = status.get("statuses") or []
        if not contexts:
            return False
        state = status.get("state")
        if state in _STATUS_SUCCESS_STATES:
            await self._promote_verified_item(
                db,
                scope,
                item,
                client,
                pull,
                head_sha,
                revision=revision,
                policy_guard=policy_guard,
            )
            return True
        if state in _STATUS_FAILURE_STATES:
            note = f"GitHub commit status failed: {state}"
            await self._record_product_verification_failure(
                db,
                scope,
                item,
                head_sha,
                note,
                subject="GitHub commit status failed",
                body_markdown=(
                    f"GitHub commit status failed for issue #{item.issue_number} / "
                    f"PR #{item.pr_number}.\n\n{note}"
                ),
                payload={
                    "kind": "github_dispatch_status_failed",
                    "work_item_id": item.id,
                    "pr_number": item.pr_number,
                },
                retry_status="dispatched",
                revision=revision,
            )
            return True
        item.status_note = "GitHub commit statuses are still pending."
        item.updated_at = datetime.utcnow()
        await db.commit()
        return True

    async def _handle_no_check_signal(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        head_sha: str | None,
        *,
        revision: GithubAttemptScopeRevision | None = None,
    ) -> None:
        grace_started_at = (
            revision.submitted_at
            if revision is not None and revision.submitted_at is not None
            else item.verification_started_at or item.updated_at or item.created_at
        )
        grace_age = datetime.utcnow() - grace_started_at
        if grace_age < timedelta(seconds=settings.github_check_signal_grace_seconds):
            item.status_note = "Waiting for GitHub check-runs or commit statuses to appear."
            item.updated_at = datetime.utcnow()
            await db.commit()
            return
        if revision is not None:
            await self._record_product_verification_failure(
                db,
                scope,
                item,
                head_sha,
                "No GitHub check-runs or commit statuses found for PR.",
                subject="GitHub verification found no check signal",
                body_markdown=(
                    f"No GitHub check-runs or commit statuses appeared for issue "
                    f"#{item.issue_number} / PR #{item.pr_number}."
                ),
                payload={
                    "kind": "github_dispatch_no_check_signal",
                    "work_item_id": item.id,
                    "pr_number": item.pr_number,
                },
                retry_status="dispatched",
                revision=revision,
            )
            return
        await github_dispatch_service.escalate(
            db,
            item,
            "retry_count_exhausted",
            "No GitHub check-runs or commit statuses found for PR.",
        )
        await db.commit()

    async def _promote_verified_item(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        client: GithubClient,
        pull: dict,
        head_sha: str | None,
        *,
        revision: GithubAttemptScopeRevision | None = None,
        policy_guard: tuple | None = None,
    ) -> None:
        policy_guard = policy_guard if policy_guard is not None else policy_context(item)
        if not await claim_policy_context(db, item, policy_guard):
            return
        readiness_note = self._draft_readiness_note(item, pull)
        if readiness_note is not None:
            item.last_verified_sha = head_sha
            await self._record_readiness_hold(db, item, readiness_note)
            return
        pull = await client.get_pull(scope.repo_owner, scope.repo_name, int(item.pr_number))
        if not await claim_policy_context(db, item, policy_guard):
            return
        if not await self._validate_polled_pull_identity(
            db, scope, item, client, pull, retry_status=item.dispatch_status, revision=revision,
        ):
            return
        if revision is not None and not await self._submitted_head_is_current(
            db, item, revision, self._head_sha(pull),
        ):
            return
        if self._head_sha(pull) != head_sha:
            await self._record_readiness_hold(
                db, item, "Head changed before readiness; verifying the current head.")
            return
        readiness_note = self._draft_readiness_note(item, pull)
        if readiness_note is not None:
            item.last_verified_sha = head_sha
            await self._record_readiness_hold(db, item, readiness_note)
            return
        if revision is not None:
            if not await self._submitted_head_is_current(
                db,
                item,
                revision,
                self._head_sha(pull),
            ):
                return
            revision.status = "completed"
            revision.completed_at = datetime.utcnow()
            await self._record_revision_outcome(
                db, item, revision, "completed",
                source="github_verification_service._promote_verified_item")
        item.last_verified_sha = head_sha
        item.dispatch_status = "ready_for_review"
        item.status_note = f"PR #{item.pr_number} is ready for review."
        item.updated_at = datetime.utcnow()
        if scope.merge_policy == "human":
            await self._notify_code_pr_ready_for_review(db, item)
        await db.commit()
        await self._process_review_item(db, scope, item, client, pull=pull)

    async def _submitted_head_is_current(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        revision: GithubAttemptScopeRevision,
        current_head_sha: str | None,
    ) -> bool:
        if (
            revision.status == "submitted"
            and revision.submitted_head_sha is not None
            and hmac.compare_digest(revision.submitted_head_sha, current_head_sha or "")
        ):
            return True

        now = datetime.utcnow()
        revision.status = "active"
        revision.submitted_head_sha = None
        revision.submitted_at = None
        item.dispatch_status = "dispatched"
        item.continuation_activated_at = now
        item.continuation_nudged_at = None
        item.status_note = (
            "The PR head changed after continuation completion; submit the current "
            "head again so Albiex Synth City can revalidate the approved paths."
        )
        item.updated_at = now
        await github_dispatch_service.notify_owner(
            db,
            item,
            subject="Continuation head changed before verification",
            body_markdown=(
                f"PR #{item.pr_number} changed after scope revision "
                f"{revision.revision} was submitted. Re-run the approved checks and "
                "report continuation_completed for the current head; Albiex Synth City will "
                "revalidate the exact approved paths before verification resumes."
            ),
            payload={
                "kind": "github_continuation_head_changed",
                "work_item_id": item.id,
                "pr_number": item.pr_number,
                "scope_revision": revision.revision,
                "current_head_sha": current_head_sha,
            },
        )
        await db.commit()
        return False

    async def _auto_merge_usage(
        self, db: AsyncSession, scope: TeamGithubScope
    ) -> tuple[int, int]:
        """Automatic merges in the last 24 hours and the scope's daily cap."""
        return await self.auto_merges_last_24h(db, scope.id), scope.max_auto_merges_per_day

    @staticmethod
    async def auto_merges_last_24h(db: AsyncSession, scope_id: int) -> int:
        since = datetime.utcnow() - timedelta(days=1)
        return int(await db.scalar(
            select(func.count(GithubWorkItem.id)).where(
                GithubWorkItem.scope_id == scope_id,
                GithubWorkItem.auto_merged_at.is_not(None),
                GithubWorkItem.auto_merged_at >= since,
            )
        ) or 0)

    async def _auto_merge_budget_exhausted(
        self, db: AsyncSession, scope: TeamGithubScope
    ) -> bool:
        used, cap = await self._auto_merge_usage(db, scope)
        return used >= cap

    @staticmethod
    def _not_fired(code: str, detail: str, head_sha: str | None) -> dict:
        """Why an accepted, ready, unheld PR was not merged automatically (a stable code and the facts)."""
        return {"code": code, "detail": detail, "head_sha": head_sha}

    async def _add_operator_not_fired(
        self, db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, not_fired: dict,
    ) -> None:
        """Add (never commit) the Supervisor's notice: a system row that only the bound Supervisor's inbox shows.

        It is built like the "Leader approval waiting" notice: no member receipts, and a payload shape that
        ``agent_team_supervisor_service`` recognizes. The caller commits it together with the item's note and the
        Leader's notice, so a failure anywhere leaves none of them and the next poll retries the whole set.
        """
        preset = await db.get(AgentTeamPreset, scope.preset_id)
        if preset is None or preset.supervisor_actor_id is None:
            return
        key = (
            f"auto-merge-not-fired:{item.id}:{item.pr_number}:{not_fired['head_sha']}:"
            f"{not_fired['code']}:{not_fired['detail']}:operator"
        )
        if await db.scalar(select(MailMessage.id).where(MailMessage.delivery_key == key)) is not None:
            return
        db.add(MailMessage(
            kind="message",
            sender_type="system",
            audience_type="team_preset",
            audience_id=str(preset.id),
            subject="Auto-merge did not fire",
            body_markdown=(
                f"PR #{item.pr_number} for issue #{item.issue_number} is accepted but was not merged "
                f"automatically: {not_fired['code']} ({not_fired['detail']}). "
                "It needs a manual merge under the configured policy. This notice grants no merge authority."
            ),
            payload={
                "notice": "auto_merge_not_fired", "work_item_id": item.id, "pr_number": item.pr_number,
                "reason": not_fired["code"], "detail": not_fired["detail"], "head_sha": not_fired["head_sha"],
                "supervisor_actor_id": preset.supervisor_actor_id,
            },
            delivery_key=key,
        ))

    def _mark_merged(self, item: GithubWorkItem) -> None:
        item.dispatch_status = "merged"
        item.escalation_reason = None
        item.status_note = None
        item.updated_at = datetime.utcnow()

    async def _mark_merged_with_fact(
        self, db: AsyncSession, item: GithubWorkItem, pull: dict | None, *, source: str
    ) -> None:
        # R02: the sourced merge outcome is recorded in the merge transaction,
        # bound to the attempt, revision and PR artifact.
        from app.services import factory_audit_service as _audit
        from app.services.github_issue_closure_service import queue_issue_closure

        await lock_terminal_transition(db, item)
        await _audit.record_merged_delivery(db, item, pull, source=source)
        self._mark_merged(item)
        await complete_current_revision(db, item, source=source, terminal_status="merged")
        await queue_issue_closure(db, item)

    async def _record_revision_outcome(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        revision: GithubAttemptScopeRevision,
        status: str,
        *,
        source: str,
    ) -> None:
        # R08: the preserved revision result, in the result's transaction.
        from app.services import factory_audit_service as _audit
        await _audit.record_revision_outcome(
            db, item_id=item.id, revision_id=revision.id, status=status, source=source)

    async def _fallback_to_human_merge(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        note: str,
        *,
        not_fired: dict | None = None,
        scope: TeamGithubScope | None = None,
    ) -> None:
        item.dispatch_status = "ready_for_review"
        item.escalation_reason = None
        item.status_note = note
        item.updated_at = datetime.utcnow()
        try:
            if not_fired is not None and scope is not None:
                await self._add_operator_not_fired(db, scope, item, not_fired)
            # The Leader's notice commits last: the note, the operator notice and this notice persist together.
            await self._notify_code_pr_ready_for_review(
                db, item, fallback_note=note, not_fired=not_fired)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise

    async def _notify_code_pr_ready_for_review(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        *,
        fallback_note: str | None = None,
        not_fired: dict | None = None,
    ) -> None:
        body = (
            f"Code PR #{item.pr_number} is ready for human review for "
            f"issue #{item.issue_number}: {item.issue_title}\n\n"
            f"Verified head: {item.last_verified_sha or 'unknown'}. "
            "CI readiness does not establish independent review acceptance.\n\n"
            + github_dispatch_service.review_rework_guidance(item)
            + "\n\n" + HUMAN_REVIEW_SUMMARY_GUIDANCE
        )
        payload = {
            "kind": "github_dispatch_code_pr_ready",
            "work_item_id": item.id,
            "pr_number": item.pr_number,
            "verified_head_sha": item.last_verified_sha,
        }
        if fallback_note:
            body = f"{body}\n\nAuto-merge fell back to human merge: {fallback_note}"
            payload["auto_merge_fallback"] = True
            payload["fallback_note"] = fallback_note
            if not_fired is not None:
                payload["auto_merge_not_fired"] = not_fired
        completion_id = await github_ci_watch_service.completion_id(db, item)
        if completion_id is not None and fallback_note is None:
            # The durable completion already notified the Leader and reviewer.
            # Keep the owner's ready-state notice without another team-wide wake.
            await github_dispatch_service.notify_owner(db, item, subject="Code PR ready for review",
                body_markdown=body, payload=payload, delivery_key=f"required-checks:{completion_id}:owner-ready")
            return
        await github_dispatch_service.notify_team(
            db,
            item=item,
            subject="Code PR ready for review",
            body_markdown=body,
            payload=payload,
        )

    def _failed_check_note(self, checks: list[dict]) -> str:
        names = ", ".join(str(check.get("name") or check.get("id")) for check in checks)
        return f"GitHub check failed: {names}"

    @staticmethod
    def _set_failure_note(item: GithubWorkItem, note: str) -> str:
        if item.status_note and item.status_note.startswith(_HUMAN_MERGE_NOTE_PREFIXES):
            return item.status_note
        item.status_note = note
        return note

    async def _escalate_implementation_exhaustion(
        self,
        db: AsyncSession,
        item: GithubWorkItem,
        revision: GithubAttemptScopeRevision,
        note: str,
        *,
        attempt_exhausted: bool,
    ) -> None:
        revision.status = "exhausted"
        if attempt_exhausted:
            await github_dispatch_service.escalate(
                db,
                item,
                "continuation_budget_exhausted",
                note,
            )
            return
        await github_dispatch_service.escalate(
            db,
            item,
            "continuation_revision_exhausted",
            note,
        )

    async def _record_product_verification_failure(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        head_sha: str | None,
        note: str,
        *,
        subject: str,
        body_markdown: str,
        payload: dict,
        retry_status: str,
        revision: GithubAttemptScopeRevision | None,
        owner_notice_sent: bool = False,
    ) -> None:
        if revision is None:
            await self._record_failed_verification_attempt(
                db,
                scope,
                item,
                head_sha,
                note,
                owner_notice_sent=owner_notice_sent,
                subject=subject,
                body_markdown=body_markdown,
                payload=payload,
                retry_status=retry_status,
            )
            return

        await db.refresh(item)
        await db.refresh(revision)
        if (
            item.active_scope_revision != revision.revision
            or item.dispatch_nonce != revision.dispatch_nonce
            or item.attempt_phase != "implementation"
            or revision.phase != "implementation"
            or revision.status not in {"active", "submitted"}
        ):
            raise ContinuationCompletionError("stale_continuation_context")

        same_head = (
            revision.failed_head_count > 0
            and revision.last_failed_head_sha == head_sha
        )
        revision_count, total_failed_heads = (
            await github_approval_service.continuation_budget_usage(
                db,
                item.id,
                item.dispatch_nonce,
            )
        )
        if same_head:
            revision_exhausted = (
                revision.failed_head_count >= revision.max_failed_heads
            )
            attempt_exhausted = (
                total_failed_heads >= scope.max_continuation_failed_heads
                or (
                    revision_exhausted
                    and revision_count >= scope.max_continuation_revisions
                )
            )
            if revision_exhausted or attempt_exhausted:
                await self._escalate_implementation_exhaustion(
                    db,
                    item,
                    revision,
                    note,
                    attempt_exhausted=attempt_exhausted,
                )
                await db.commit()
            elif item.dispatch_status != retry_status or revision.status != "active":
                now = datetime.utcnow()
                item.dispatch_status = retry_status
                item.continuation_activated_at = now
                item.continuation_nudged_at = None
                item.updated_at = now
                revision.status = "active"
                revision.submitted_head_sha = None
                revision.submitted_at = None
                self._set_failure_note(item, note)
                await db.commit()
            if not owner_notice_sent:
                await github_dispatch_service.notify_owner(
                    db,
                    item,
                    subject=subject,
                    body_markdown=body_markdown,
                    payload={
                        **payload,
                        "retry_count": item.retry_count,
                        "head_sha": head_sha,
                        "scope_revision": revision.revision,
                        "revision_failed_head_count": revision.failed_head_count,
                    },
                    delivery_key=(
                        f"github-continuation:{revision.id}:verification-failure:"
                        f"{head_sha or 'no-head'}"
                    ),
                )
            return
        revision.failed_head_count += 1
        revision.last_failed_head_sha = head_sha
        item.retry_count += 1
        # C12: implementation retry evidence, in the charge's transaction.
        from app.services import factory_audit_service as _audit
        await _audit.record_retry_charge(
            db, item_id=item.id, scope_id=item.scope_id, revision_id=revision.id,
            retry_class="implementation", counter_value=item.retry_count,
            reason_code="product_verification_failure",
            source="github_verification_service._record_product_verification_failure")
        item.last_verified_sha = head_sha
        self._set_failure_note(item, note)
        revision_exhausted = revision.failed_head_count >= revision.max_failed_heads
        attempt_exhausted = (
            total_failed_heads + 1 >= scope.max_continuation_failed_heads
            or (
                revision_exhausted
                and revision_count >= scope.max_continuation_revisions
            )
        )
        if revision_exhausted or attempt_exhausted:
            await self._escalate_implementation_exhaustion(
                db,
                item,
                revision,
                note,
                attempt_exhausted=attempt_exhausted,
            )
        else:
            now = datetime.utcnow()
            revision.status = "active"
            revision.submitted_head_sha = None
            revision.submitted_at = None
            item.dispatch_status = retry_status
            item.continuation_activated_at = now
            item.continuation_nudged_at = None
            item.updated_at = now
        await db.commit()
        if not owner_notice_sent:
            await github_dispatch_service.notify_owner(
                db,
                item,
                subject=subject,
                body_markdown=body_markdown,
                payload={
                    **payload,
                    "retry_count": item.retry_count,
                    "head_sha": head_sha,
                    "scope_revision": revision.revision,
                    "revision_failed_head_count": revision.failed_head_count,
                },
                delivery_key=(
                    f"github-continuation:{revision.id}:verification-failure:"
                    f"{head_sha or 'no-head'}"
                ),
            )

    async def _record_failed_verification_attempt(
        self,
        db: AsyncSession,
        scope: TeamGithubScope,
        item: GithubWorkItem,
        head_sha: str | None,
        note: str,
        *,
        subject: str,
        body_markdown: str,
        payload: dict,
        retry_status: str,
        owner_notice_sent: bool = False,
    ) -> None:
        if head_sha and item.last_verified_sha == head_sha:
            if item.dispatch_status != retry_status:
                item.dispatch_status = retry_status
                self._set_failure_note(item, note)
                item.updated_at = datetime.utcnow()
                await db.commit()
            return

        item.last_verified_sha = head_sha
        item.retry_count += 1
        # C12: implementation retry evidence, in the charge's transaction.
        from app.services import factory_audit_service as _audit
        await _audit.record_retry_charge(
            db, item_id=item.id, scope_id=item.scope_id, revision_id=None,
            retry_class="implementation", counter_value=item.retry_count,
            reason_code="failed_verification_attempt",
            source="github_verification_service._record_failed_verification_attempt")
        self._set_failure_note(item, note)
        if not owner_notice_sent:
            await github_dispatch_service.notify_owner(
                db,
                item,
                subject=subject,
                body_markdown=body_markdown,
                payload={
                    **payload,
                    "retry_count": item.retry_count,
                    "head_sha": head_sha,
                },
            )
        if item.retry_count > scope.max_verification_retries:
            await github_dispatch_service.escalate(
                db,
                item,
                "retry_count_exhausted",
                note,
            )
        else:
            item.dispatch_status = retry_status
            item.updated_at = datetime.utcnow()
        await db.commit()

    def _head_sha(self, pull: dict) -> str | None:
        sha = (pull.get("head") or {}).get("sha")
        return str(sha) if sha else None

    async def _head_is_green(
        self, scope: TeamGithubScope, client: GithubClient, head_sha: str | None,
        *, item: GithubWorkItem | None = None
    ) -> bool:
        if not head_sha:
            return False
        checks = await client.list_check_runs_for_ref(
            scope.repo_owner, scope.repo_name, head_sha
        )
        policy = effective_policy(item, scope) if item is not None else scope_policy(scope)
        if not checks or required_check_blockers(policy, checks, head_sha):
            return False
        if any(
            check.get("status") != "completed" or check.get("conclusion") is None
            for check in checks
        ):
            return False
        if not all(check.get("conclusion") in _SUCCESS_CONCLUSIONS for check in checks):
            return False
        # #1076: this one status read decides. A failed, errored, pending or unobserved commit status is not green,
        # even when an earlier read saw success; an empty aggregate (no statuses at all) is.
        status = await client.get_combined_status_for_ref(scope.repo_owner, scope.repo_name, head_sha)
        return not (self._failed_status_contexts(status) or self._pending_merge_checks([], status, head_sha))

    async def _failed_commit_statuses(self, scope: TeamGithubScope, client: GithubClient, head_sha: str) -> list[str]:
        """Read the head's commit statuses and name the contexts that ended in failure or error, for a note (#1076)."""
        return self._failed_status_contexts(
            await client.get_combined_status_for_ref(scope.repo_owner, scope.repo_name, head_sha))

    @staticmethod
    def _failed_status_contexts(status: dict) -> list[str]:
        """The contexts in failure or error (#1076); a failed aggregate with no listed context fails closed."""
        contexts = status.get("statuses") or []
        failed = [
            str(context.get("context") or "unnamed commit status")
            for context in contexts
            if isinstance(context, dict) and context.get("state") in _STATUS_FAILURE_STATES
        ]
        if not failed and status.get("state") in _STATUS_FAILURE_STATES:
            failed.append("failed commit status contexts")  # the aggregate failed, but its contexts were not listed
        return failed


github_verification_service = GithubVerificationService()

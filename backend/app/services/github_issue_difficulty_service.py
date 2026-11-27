"""Authenticated, repository-scoped difficulty changes during triage."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Literal

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import AgentTeamPreset, GithubWorkItem, MailAgentSession, MailExternalActor, TeamGithubScope
from app.models.schemas import CapabilityTier, IssueDifficultyProvenance
from app.services import factory_audit_service as audit
from app.services.github_app_auth_service import GithubAppAuthError, github_app_auth_service
from app.services.github_client import GithubClientResponseError, github_client
from app.services.github_coordination_service import CoordinationError, github_coordination_service, hold_code
from app.services.github_dispatch_difficulty import difficulty_from_labels, difficulty_labels, observe_issue_difficulty
from app.services.github_terminal_revision_service import TERMINAL_ITEMS
from app.services.github_watcher_service import _lock_repository


def _target(item: GithubWorkItem, scope: TeamGithubScope) -> tuple:
    return (scope.id, scope.preset_id, scope.repo_owner, scope.repo_name, scope.github_auth_mode,
            scope.github_app_installation_id, item.issue_number, item.issue_url, item.dispatch_nonce)


async def _read_labels(owner: str, repo: str, number: int, token: str | None) -> set[str]:
    issue = await github_client.get_issue(owner, repo, number, token=token)
    if issue is None or issue.get("state") != "open":
        raise GithubClientResponseError("Difficulty lookup requires an open issue")
    labels = issue.get("labels")
    if not isinstance(labels, list) or any(
        not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels
    ):
        raise GithubClientResponseError("GitHub issue labels were invalid")
    return difficulty_labels(label["name"] for label in labels)


async def _repository_token(owner: str, repo: str, auth_mode: str, installation: int | None) -> str | None:
    if auth_mode == "ambient":
        return None
    if auth_mode == "unknown":
        if not settings.github_app_id and not settings.github_app_private_key_path:
            return None
        github_app_auth_service.require_configuration(require_bot_login=True)
        installation = await github_app_auth_service.resolve_installation(owner, repo)
        if installation is None:
            return None
    elif auth_mode != "app" or installation is None:
        raise GithubClientResponseError("Issue difficulty requires a configured GitHub credential")
    return await github_app_auth_service.mint_repository_token(
        installation, owner, repo, purpose="issue_difficulty",
    )


async def _actor(
    db: AsyncSession, source: str, scope: TeamGithubScope, preset: AgentTeamPreset,
    session_id: int | None, session_hash: str | None, external_id: int | None, external_hash: str | None,
    via: str | None,
) -> dict:
    if source == "leader":
        principal = await db.get(MailAgentSession, session_id, populate_existing=True) if session_id is not None else None
        if principal is None or principal.capability_token_hash != session_hash:
            raise CoordinationError("current_leader_required", 403)
        leader = await github_coordination_service.require_leader(db, scope.id, principal)
        return audit.derive_actor(actor_kind="member", member_id=leader.member_id, session_id=leader.id)
    if source == "supervisor":
        supervisor = await db.get(MailExternalActor, external_id, populate_existing=True) if external_id is not None else None
        if (supervisor is None or supervisor.id != preset.supervisor_actor_id
                or supervisor.revoked_at is not None or supervisor.token_hash != external_hash):
            raise CoordinationError("supervisor_actor_not_bound", 403)
        return audit.derive_actor(actor_kind="external_actor", external_actor_id=supervisor.id)
    return audit.derive_actor(actor_kind="operator", via=via)


async def set_issue_difficulty(
    db: AsyncSession, work_item_id: int, difficulty: CapabilityTier, *,
    source: Literal["leader", "director", "supervisor"],
    session: MailAgentSession | None = None, external_actor: MailExternalActor | None = None,
    via: str | None = None,
) -> IssueDifficultyProvenance:
    session_id = session.id if session else None
    session_hash = session.capability_token_hash if session else None
    external_id = external_actor.id if external_actor else None
    external_hash = external_actor.token_hash if external_actor else None

    item = await db.get(GithubWorkItem, work_item_id)
    scope = await db.get(TeamGithubScope, item.scope_id) if item else None
    if item is None or scope is None:
        raise CoordinationError("work_item_not_found", 404)
    snapshot = _target(item, scope)
    _, _, owner, repo, auth_mode, installation, number, url, _ = snapshot
    if url.lower() != f"https://github.com/{owner}/{repo}/issues/{number}".lower():
        raise CoordinationError("difficulty_issue_mismatch")

    # Drop the read snapshot before taking the same repository lock as the watcher.
    await db.rollback()
    uncertain = False
    async with db.begin():
        if not await _lock_repository(db, SimpleNamespace(repo_owner=owner, repo_name=repo)):
            raise CoordinationError("difficulty_target_changed")
        item = await db.get(GithubWorkItem, work_item_id)
        scope = await db.get(TeamGithubScope, item.scope_id) if item else None
        if item is None or scope is None or _target(item, scope) != snapshot:
            raise CoordinationError("difficulty_target_changed")
        preset = await db.get(AgentTeamPreset, scope.preset_id)
        if preset is None or not scope.enabled or not preset.autonomy_enabled:
            raise CoordinationError("autonomy_off")
        if preset.dispatch_paused:
            raise CoordinationError("dispatch_paused")
        if code := hold_code():
            raise CoordinationError(code)
        if (item.dispatch_status not in {"pending", "dispatched"} or item.ack_received_at
                or item.pr_number is not None or item.milestone_replaced_at):
            raise CoordinationError("difficulty_triage_required")
        actor = await _actor(
            db, source, scope, preset, session_id, session_hash, external_id, external_hash, via,
        )

        peers = (await db.scalars(select(GithubWorkItem).join(TeamGithubScope).where(
            func.lower(TeamGithubScope.repo_owner) == owner.lower(),
            func.lower(TeamGithubScope.repo_name) == repo.lower(),
            GithubWorkItem.issue_number == number, func.lower(GithubWorkItem.issue_url) == url.lower(),
            GithubWorkItem.milestone_replaced_at.is_(None),
            GithubWorkItem.dispatch_status.not_in(TERMINAL_ITEMS),
        ))).all()
        requested = f"difficulty:{difficulty}"
        proof = IssueDifficultyProvenance(difficulty=difficulty, source=source, actor_reference=actor["actor_reference"])
        try:
            async with asyncio.timeout(4):
                token = await _repository_token(owner, repo, auth_mode, installation)
                previous = await _read_labels(owner, repo, number, token)
        except (GithubAppAuthError, GithubClientResponseError, httpx.HTTPError, TimeoutError):
            raise CoordinationError("difficulty_github_unavailable", 502) from None

        # Commit fresh observations even when the Leader encounters an external override.
        for peer in peers:
            observe_issue_difficulty(peer, previous)
        proofs = [peer.issue_difficulty_provenance for peer in peers if peer.issue_difficulty_provenance]
        protected = source == "leader" and bool(previous) and (
            not proofs or any(p["source"] != "leader" for p in proofs)
        )
        if not protected and (
            previous != {requested} or not proofs
            or any(p["source"] != source or p["actor_reference"] != proof.actor_reference for p in proofs)
        ):
            # Authentication and HOLD can change while GitHub serves the read.
            actor = await _actor(
                db, source, scope, preset, session_id, session_hash, external_id, external_hash, via,
            )
            if code := hold_code():
                raise CoordinationError(code)
            if previous != {requested}:
                try:
                    async with asyncio.timeout(4):
                        try:
                            await github_client.update_issue_difficulty_labels(
                                owner, repo, number, previous=previous, requested=requested, token=token,
                            )
                        except (GithubClientResponseError, httpx.HTTPError):
                            uncertain = True
                        observed = await _read_labels(owner, repo, number, token)
                        for peer in peers:
                            observe_issue_difficulty(peer, observed)
                        uncertain = uncertain or observed != {requested}
                except (GithubClientResponseError, httpx.HTTPError, TimeoutError):
                    uncertain = True
            confirmed_proof = {**proof.model_dump(), "confirmed_at": datetime.utcnow().isoformat()}
            for peer in peers:
                peer.issue_difficulty_provenance = None if uncertain else confirmed_proof
                if not uncertain:
                    peer.issue_difficulty = difficulty
            before = difficulty_from_labels(previous) or "unset"
            await audit.record_event(
                db, event_kind="issue_difficulty_changed", source="issue_difficulty", occurred_at=datetime.utcnow(),
                item_id=item.id, actor=actor, action_outcome="uncertain" if uncertain else "applied",
                sanitized_reason=(f"GitHub could not confirm the change from {before} to {difficulty}."
                                  if uncertain else f"Difficulty changed from {before} to {difficulty}."),
            )
    if protected:
        raise CoordinationError("difficulty_override_protected")
    if uncertain:
        raise CoordinationError("difficulty_write_uncertain", 502)
    return proof

"""#1185: a revision's prohibited_actions apply only while it is active or submitted.

Live: after WI286's PR merged, its completed revision still showed `release_workspace` in prohibited_actions in the
owner context, with nothing saying it no longer applied, so the workspace stayed leased. The recorded terms stay in
the revision history; the owner's current view drops them once the revision ends, every revision response says
whether its prohibitions are in force, and the guidance says that releasing a merged item's workspace is never
prohibited.
"""
from __future__ import annotations

import pytest
from app.api.v1.deps import mail_session
from app.main import app
from app.models.database import GithubAttemptScopeRevision
from app.services.github_dispatch_service import github_dispatch_service
from app.services.github_terminal_revision_service import TERMINAL_REVISIONS
from tests.agent_teams.test_github_workspace_api import _active_completion_context, client  # noqa: F401

ENDED_TERMS = ["release_workspace", "mark_ready"]


async def _context(client, item, session):
    async def authenticated_session():
        return session

    app.dependency_overrides[mail_session] = authenticated_session
    try:
        response = await client.post(f"/api/v1/agent-teams/github-work-items/{item.id}/claim-continuation")
    finally:
        app.dependency_overrides.pop(mail_session, None)
    assert response.status_code == 200, response.text
    return response.json()


async def _with_revision(db, tmp_path, *, item_status, revision_status):
    scope, item, workspace, revision, session = await _active_completion_context(db, tmp_path)
    item.dispatch_status = item_status
    revision.status = revision_status
    revision.prohibited_actions = ENDED_TERMS
    await db.commit()
    return item, workspace, revision, session


@pytest.mark.asyncio
@pytest.mark.parametrize("item_status", ["merged", "ready_for_review"])
async def test_an_ended_revision_reports_no_prohibitions_in_the_owner_context(client, db, tmp_path, item_status):
    item, _workspace, revision, session = await _with_revision(
        db, tmp_path, item_status=item_status, revision_status="completed")
    body = await _context(client, item, session)
    assert body["active_revision"]["status"] == "completed"
    assert body["active_revision"]["prohibited_actions"] == []  # neither release nor mark-ready is blocked
    assert body["active_revision"]["prohibitions_in_force"] is False
    stored = await db.get(GithubAttemptScopeRevision, revision.id)
    assert stored.prohibited_actions == ENDED_TERMS  # the recorded terms are kept as history


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "submitted"])
async def test_an_active_or_submitted_revision_keeps_its_prohibitions_in_force(client, db, tmp_path, status):
    item, _workspace, _revision, session = await _with_revision(
        db, tmp_path, item_status="dispatched" if status == "active" else "verifying", revision_status=status)
    body = await _context(client, item, session)
    assert body["active_revision"]["prohibited_actions"] == ENDED_TERMS
    assert body["active_revision"]["prohibitions_in_force"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status", TERMINAL_REVISIONS)
async def test_the_revision_history_keeps_the_recorded_terms_marked_not_in_force(client, db, tmp_path, status):
    from app.api.v1.agent_teams import _scope_revision_response

    _item, _workspace, revision, _session = await _with_revision(
        db, tmp_path, item_status="merged", revision_status=status)
    response = _scope_revision_response(revision)
    assert response.prohibited_actions == ENDED_TERMS and response.prohibitions_in_force is False


def test_the_guidance_says_a_merged_items_release_is_never_prohibited():
    from types import SimpleNamespace

    terminal = github_dispatch_service.review_rework_guidance(SimpleNamespace(dispatch_status="merged"))
    assert "Releasing a merged item's workspace is never prohibited" in terminal
    assert "release authority" not in terminal
    scoped = github_dispatch_service.review_rework_guidance(SimpleNamespace(
        dispatch_status="verifying", active_scope_revision=1, attempt_phase="implementation"))
    assert "only while it is active or submitted" in scoped


def test_the_workspace_release_path_reads_no_prohibitions():
    # Prohibitions are guidance for agents; the release path never consults them, so an ended revision's
    # "release_workspace" term cannot hold a merged item's workspace.
    import inspect

    from app.services import github_workspace_service

    assert "prohibited_actions" not in inspect.getsource(github_workspace_service)

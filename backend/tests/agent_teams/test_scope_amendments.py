"""Healthy amendments and submitted-revision exits use disposable authority."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import subprocess

import pytest
import pytest_asyncio
from sqlalchemy import event, select, update
from sqlalchemy.sql.dml import Update

from app.api.v1 import agent_mail
from app.config import settings
from app.models.database import (
    AgentTeamPreset, GithubApprovalRequest, GithubWorkItem, MailAgentSession, TeamGithubScope,
    GithubWorkspace, MailMessage,
)
from app.models.schemas import MailApprovalRequestCreate, MailMessageCreate
from app.services.agent_mail_service import agent_mail_service
from app.services.github_approval_service import GithubApprovalError, github_approval_service
from app.services.github_app_auth_service import github_app_auth_service
from app.services.github_client import GithubClientResponseError, GithubCommitSnapshot, GithubTreeEntry, github_client
from app.services.github_verification_service import github_verification_service
from app.services.github_dispatch_service import github_dispatch_service
from app.services.github_work_item_projection import _work_item_response
from app.utils import peer_process
from app.database import Base, configure_sqlite_connection
from tests.agent_mail.test_api import _dispatch_approval_fixture
from tests.agent_teams.test_github_verification_service import (
    _Client, _implementation_revision, _item, _owner, _scope,
)


@pytest_asyncio.fixture
async def db(request, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    mode = getattr(request, 'param', None)
    url = (f"sqlite+aiosqlite:///{tmp_path / 'authority.sqlite'}"
        if mode in {'concurrent', 'concurrent_wal'} else 'sqlite+aiosqlite:///:memory:')
    engine = create_async_engine(url)
    if mode == 'concurrent_wal':
        @event.listens_for(engine.sync_engine, "connect")
        def configure(dbapi_conn, _):
            configure_sqlite_connection(dbapi_conn)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def healthy_attempt(db, monkeypatch, tmp_path, request):
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr(peer_process, "pane_is_alive", lambda *_args: True)
    monkeypatch.setattr(agent_mail_service, "auto_nudge_members", AsyncMock())
    item, members, _tokens = await _dispatch_approval_fixture(db)
    scope = await db.get(TeamGithubScope, item.scope_id)
    preset = await db.get(AgentTeamPreset, scope.preset_id)
    preset.autonomy_enabled = scope.enabled = True
    scope.continuation_enabled = True
    scope.github_auth_mode = "ambient"
    repo = tmp_path / "leased"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "src").mkdir()
    (repo / "src/base.py").write_text("BASE = True\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "baseline"], check=True)
    subprocess.run(["git", "-C", str(repo), "switch", "-qc", item.dispatch_head_ref], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    tree = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], text=True).strip()
    scope.repo_path = str(repo)
    workspace = GithubWorkspace(scope_id=scope.id, path=str(repo), leased_item_id=item.id,
        leased_at=datetime.utcnow(), lease_token="synthetic-lease")
    db.add(workspace)
    await db.commit()
    session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == members[1].id))
    plan_metadata = {"allowed_paths": ["src/base.py"]}
    variant = getattr(request, "param", None)
    if variant == "with_companion":
        from app.services import companion_paths
        monkeypatch.setattr(companion_paths, "load_map", lambda: {
            "src/base.py": [{"path": "src/added.py", "kind": "test"}],
        })
    elif variant == "without_paths":
        plan_metadata = {}
    response = await agent_mail.request_work_item_approval(MailApprovalRequestCreate(
        work_item_id=item.id, dispatch_nonce=item.dispatch_nonce, summary="Initial closed scope",
        plan_metadata=plan_metadata), session, db)
    approval = await db.get(GithubApprovalRequest, response.id)
    await github_approval_service.decide(db, item, authenticated_leader_member_id=members[0].id,
        decision="approved", reason="Fixture initial decision", request_id=approval.id)
    item.ack_received_at = datetime.utcnow()
    item.ack_approver_member_id = members[0].id
    item.ack_enforcement_epoch = item.ack_approval_round = 1
    await db.commit()
    pull = {"number": 5, "state": "open", "merged_at": None, "draft": True,
        "head": {"sha": head, "ref": item.dispatch_head_ref, "repo": {"full_name": "o/approval"}},
        "base": {"ref": "release/v4.1", "repo": {"full_name": "o/approval"}}}
    monkeypatch.setattr(github_client, "get_pull", AsyncMock(return_value=pull))
    monkeypatch.setattr(github_client, "get_commit_snapshot", AsyncMock(
        return_value=GithubCommitSnapshot(sha=head, tree_sha=tree)))
    return SimpleNamespace(item=item, scope=scope, preset=preset, owner=members[1], leader=members[0],
        workspace=workspace, head=head, tree=tree, pull=pull, repo=repo, approval=approval)


def amendment(a, paths=None):
    return dict(authenticated_owner_member_id=a.owner.id, authenticated_owner_slot_id=a.item.owner_slot_id,
        dispatch_nonce=a.item.dispatch_nonce, phase="implementation", execution_target="workspace",
        summary="Add bounded regression coverage", allowed_paths=paths or ["src/base.py", "src/added.py"],
        allowed_actions=["edit_tests", "push_pr_head", "request_verification"],
        allowed_commands=["pytest -q"], prohibited_actions=["No live factory"], max_failed_heads=1,
        tool_fallbacks={}, lease_token="synthetic-lease")


@pytest.mark.asyncio
async def test_healthy_amendment_before_pr_is_a_new_pending_round_and_replays(db, healthy_attempt):
    a = healthy_attempt
    before = (a.item.dispatch_nonce, a.item.owner_slot_id, a.workspace.lease_token)
    first = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    replay = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    revision, approval, created = first
    assert created and not replay[2]
    assert replay[0].id == revision.id and replay[1].id == approval.id
    assert approval.request_kind == "continuation" and approval.status == "pending"
    assert revision.origin_kind == "scope_amendment"
    assert approval.approval_round == a.item.approval_round_count == 2
    assert revision.baseline_head_sha == a.head and revision.baseline_tree_sha == a.tree
    assert revision.allowed_paths == ["src/added.py", "src/base.py"]
    assert revision.status == "proposed" and a.item.active_scope_revision == 0
    assert a.item.dispatch_status == "dispatched" and a.item.escalation_reason is None
    assert a.item.pr_number is None
    assert before == (a.item.dispatch_nonce, a.item.owner_slot_id, a.workspace.lease_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,count,allowed", [(16, 16, True), (16, 17, False), (1, 1, True), (1, 2, False)])
async def test_added_path_cap_is_enforced_on_unique_normalized_paths(db, healthy_attempt, limit, count, allowed):
    a = healthy_attempt
    a.scope.max_amendment_added_paths = limit
    await db.commit()
    paths = ["src/base.py"] + [f"src/add-{n}.py" for n in range(count)]
    paths += paths  # Duplicate spellings consume no extra path budget.
    if allowed:
        revision, _, _ = await github_approval_service.create_scope_amendment_request(
            db, a.item, a.scope, **amendment(a, paths))
        assert len(revision.allowed_paths) == limit + 1
    else:
        with pytest.raises(GithubApprovalError, match="amendment_added_path_limit"):
            await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a, paths))
        assert a.item.approval_round_count == 1


@pytest.mark.asyncio
async def test_amendment_approval_needs_separate_delivered_owner_activation_and_preserves_held_edits(
    db, healthy_attempt
):
    a = healthy_attempt
    held = "BASE = 'uncommitted approved-path work'\n"
    (a.repo / "src/base.py").write_text(held)
    revision, approval, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)
    await github_approval_service.decide_continuation(db, a.item,
        authenticated_leader_member_id=a.leader.id, decision="approved", reason="Reviewed amendment",
        request_id=approval.id)
    assert revision.status == "approved" and a.item.active_scope_revision == 0
    assert a.item.ack_received_at is None and (a.repo / "src/base.py").read_text() == held
    await github_approval_service.ensure_continuation_decision_message(db, a.item, approval, revision)
    await github_approval_service.deliver_approved_continuation(db, a.item, approval, revision)
    changed = await github_dispatch_service.activate_continuation_revision(db, a.item, a.scope, revision,
        authenticated_owner_member_id=a.owner.id, authenticated_owner_slot_id=a.owner.team_slot_id,
        dispatch_nonce=a.item.dispatch_nonce, lease_token=a.workspace.lease_token)
    await db.refresh(revision)
    await db.refresh(a.item)
    assert changed and revision.status == "active" and a.item.active_scope_revision == revision.revision
    assert a.item.dispatch_status == "dispatched" and a.item.escalation_reason is None
    assert a.item.ack_received_at and a.item.ack_approval_round == 2
    await db.refresh(a.workspace)
    assert a.workspace.lease_last_owner_contact_at is not None
    assert (a.repo / "src/base.py").read_text() == held and a.workspace.leased_item_id == a.item.id


@pytest.mark.asyncio
async def test_scope_amendment_cannot_be_approved_after_local_head_moves(db, healthy_attempt):
    a = healthy_attempt
    revision, approval, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)
    (a.repo / "src/base.py").write_text("BASE = 'new commit'\n")
    subprocess.run(["git", "-C", str(a.repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(a.repo), "-c", "user.name=Fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "-qm", "moved"], check=True)
    with pytest.raises(GithubApprovalError, match="amendment_head_changed"):
        await github_approval_service.decide_continuation(db, a.item,
            authenticated_leader_member_id=a.leader.id, decision="approved", reason="Stale review",
            request_id=approval.id)
    await db.refresh(approval)
    assert approval.status == "pending" and a.item.active_scope_revision == 0


async def initial_path_merge_case(db, a, monkeypatch, *, extra_file=False):
    from app.services import github_head_acceptance_service

    # This fixture exercises the ordinary auto-merge path after a green, accepted head.
    monkeypatch.setattr(github_head_acceptance_service, "gate_note", AsyncMock(return_value=None))
    a.scope.merge_policy = "auto"
    a.item.dispatch_status = "ready_for_review"
    a.item.dispatch_base_ref = "origin/release/v4.1"
    a.item.pr_number = 5
    a.item.last_verified_sha = a.head
    base_tip, common_sha, common_tree = "a" * 40, "b" * 40, "c" * 40
    a.pull["base"]["sha"] = base_tip
    a.pull["draft"] = False
    client = _Client(pull=a.pull, check_runs=green_runs(a.head), combined_status={"state": "success", "statuses": []})
    # The PR already inherited integration.py, and the base tip has moved again. Only the
    # merge base has the right contents for this comparison.
    inherited = GithubTreeEntry("src/integration.py", "100644", "blob", "d" * 40)
    owned = GithubTreeEntry("src/base.py", "100644", "blob", "e" * 40)
    common = {inherited.path: inherited}
    head = {**common, owned.path: owned}
    if extra_file:
        extra = GithubTreeEntry("src/added.py", "100644", "blob", "f" * 40)
        head[extra.path] = extra
    snapshots = {a.head: a.tree, common_sha: common_tree}
    trees = {a.tree: head, common_tree: common}
    client.get_merge_base = AsyncMock(return_value=common_sha)
    client.get_commit_snapshot = AsyncMock(side_effect=lambda _, __, sha, **kw: GithubCommitSnapshot(sha, snapshots[sha]))
    client.get_recursive_tree = AsyncMock(side_effect=lambda _, __, sha, **kw: trees[sha])
    await db.commit()
    return client, base_tip


async def register_initial_pull(db, a, client, monkeypatch, route):
    a.item.pr_number = a.item.last_verified_sha = None
    a.item.dispatch_status = "dispatched"
    client.pull["draft"] = True
    if route == "pr_ready":
        a.scope.github_auth_mode = "app"
        a.scope.github_app_installation_id = 55
        client.pull["user"] = {"login": "deck[bot]"}
        monkeypatch.setattr(settings, "github_app_bot_login", "deck[bot]")
        monkeypatch.setattr(github_app_auth_service, "require_configuration", lambda **kw: None)
        monkeypatch.setattr(github_app_auth_service, "mint_repository_token", AsyncMock(return_value="app-token"))
        client.get_ref = AsyncMock(return_value={"ref": f"refs/heads/{a.item.dispatch_head_ref}"})
        client.list_pulls_for_head = AsyncMock(return_value=[])
        client.create_pull = AsyncMock(return_value=client.pull)
        client.get_pull = AsyncMock(return_value=client.pull)
        await db.commit()
        await github_verification_service.report_pr_ready(
            db, a.item, a.scope, a.item.dispatch_head_ref, a.workspace.lease_token, client)
    else:
        await db.commit()
        await github_verification_service.report_pr_opened(db, a.item, a.scope, client.pull["number"], client)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["pr_opened", "pr_ready"])
async def test_pr_registration_shows_initial_paths_next_to_ci(db, healthy_attempt, monkeypatch, route):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    await register_initial_pull(db, a, client, monkeypatch, route)
    await db.refresh(a.item)
    assert a.item.dispatch_status == "verifying"
    assert _work_item_response(a.item, a.scope).status_note == "initial_paths_out_of_scope: 'src/added.py'"
    a.item.status_note = "GitHub checks are still running."
    note = _work_item_response(a.item, a.scope).status_note
    assert "GitHub checks are still running." in note and "src/added.py" in note


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_attempt", ["with_companion"], indirect=True)
async def test_registration_groups_changed_files_by_approved_first_plan(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    client, base = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    read_tree = client.get_recursive_tree.side_effect
    outside = GithubTreeEntry("src/outside.py", "100644", "blob", "c" * 40)

    def tree_with_outside(owner, repo, sha, **kwargs):
        tree = read_tree(owner, repo, sha, **kwargs)
        return {**tree, outside.path: outside} if sha == a.tree else tree

    client.get_recursive_tree.side_effect = tree_with_outside
    await register_initial_pull(db, a, client, monkeypatch, "pr_opened")
    expected = {
        "head_sha": a.head, "base_sha": base, "in_scope": ["src/base.py"],
        "approved_companions": ["src/added.py"], "outside_scope": ["src/outside.py"],
        "note": "initial_paths_out_of_scope: 'src/outside.py'",
    }
    assert a.item.initial_paths_readiness["comparison"] == expected
    # A record written before comparisons were stored must refresh at the same head.
    a.item.initial_paths_readiness = {"head": a.head, "base": base, "note": expected["note"]}
    await db.commit()
    await github_verification_service.process_scope(db, a.scope, client=client)
    await db.refresh(a.item)
    assert a.item.initial_paths_readiness["comparison"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["head", "base"])
async def test_pr_registration_reports_an_inconclusive_comparison_if_the_pr_changes(
    db, healthy_attempt, monkeypatch, side,
):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    changed = {**client.pull, side: {**client.pull[side], "sha": "f" * 40}}
    client.get_pull = AsyncMock(side_effect=[client.pull, changed])
    await register_initial_pull(db, a, client, monkeypatch, "pr_opened")
    assert _work_item_response(a.item, a.scope).status_note == (
        "initial_paths_inconclusive: the PR changed during the comparison."
    )
    assert a.item.initial_paths_readiness.get("comparison") is None


@pytest.mark.asyncio
async def test_pr_registration_cannot_publish_the_note_after_the_item_head_moves(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    original_pull = client.get_pull

    async def advance_head(*args):
        pull = await original_pull(*args)
        if client.pull_calls == 2:
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == a.item.id)
                             .values(verification_head_sha="1" * 40).execution_options(synchronize_session=False))
            await db.commit()
        return pull

    client.get_pull = advance_head
    await register_initial_pull(db, a, client, monkeypatch, "pr_opened")
    await db.refresh(a.item)
    assert a.item.verification_head_sha == "1" * 40
    assert a.item.initial_paths_readiness is None


@pytest.mark.asyncio
async def test_pr_registration_shows_inconclusive_note_for_unverifiable_plan(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    root = await db.get(MailMessage, a.approval.request_message_id)
    root.payload = {**root.payload, "summary": "Changed after approval"}
    await register_initial_pull(db, a, client, monkeypatch, "pr_opened")
    assert _work_item_response(a.item, a.scope).status_note == (
        "initial_paths_inconclusive: the approved first plan could not be verified."
    )
    client.get_merge_base.assert_not_awaited()


@pytest.mark.asyncio
async def test_work_item_note_hides_readiness_for_an_older_verification_head(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    await register_initial_pull(db, a, client, monkeypatch, "pr_opened")
    assert "src/added.py" in _work_item_response(a.item, a.scope).status_note
    a.item.verification_head_sha = "1" * 40
    a.item.status_note = "GitHub checks are still running."
    assert _work_item_response(a.item, a.scope).status_note == "GitHub checks are still running."


@pytest.mark.asyncio
async def test_verification_refreshes_the_readiness_note_for_the_new_head(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    await register_initial_pull(db, a, client, monkeypatch, "pr_opened")
    new_head, new_tree = "1" * 40, "2" * 40
    snapshot, tree = client.get_commit_snapshot.side_effect, client.get_recursive_tree.side_effect
    client.get_commit_snapshot.side_effect = lambda owner, repo, sha, **kw: (
        GithubCommitSnapshot(new_head, new_tree) if sha == new_head else snapshot(owner, repo, sha, **kw))
    client.get_recursive_tree.side_effect = lambda owner, repo, sha, **kw: (
        {p: e for p, e in tree(owner, repo, a.tree, **kw).items() if p != "src/added.py"}
        if sha == new_tree else tree(owner, repo, sha, **kw))
    client.pull["head"]["sha"] = new_head
    await github_verification_service.process_scope(db, a.scope, client=client)
    await db.refresh(a.item)
    assert a.item.verification_head_sha == a.item.initial_paths_readiness["head"] == new_head
    assert a.item.initial_paths_readiness["note"] is None


@pytest.mark.asyncio
async def test_verification_rechecks_a_moved_base_at_the_same_head(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    await register_initial_pull(db, a, client, monkeypatch, "pr_opened")
    assert "src/added.py" in _work_item_response(a.item, a.scope).status_note

    # The base now includes this commit, so there are no new PR paths to report.
    client.pull["base"]["sha"] = "1" * 40
    client.get_merge_base.return_value = a.head
    await github_verification_service.process_scope(db, a.scope, client=client)
    await db.refresh(a.item)
    assert a.item.initial_paths_readiness == {
        "head": a.head, "base": "1" * 40, "note": None,
        "comparison": {"head_sha": a.head, "base_sha": "1" * 40, "note": None,
                       "in_scope": [], "approved_companions": [], "outside_scope": []},
    }
    assert "initial_paths_out_of_scope" not in (_work_item_response(a.item, a.scope).status_note or "")


@pytest.mark.asyncio
async def test_initial_path_gate_merges_in_scope_head_with_base_only_files(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    client, base_tip = await initial_path_merge_case(db, a, monkeypatch)
    await github_verification_service.process_scope(db, a.scope, client=client)
    client.get_merge_base.assert_awaited_once_with("o", "approval", base_tip, a.head, token=None)
    assert a.item.dispatch_status == "merged" and client.merge_head_shas == [a.head]


@pytest.mark.asyncio
async def test_initial_path_gate_holds_extra_file_until_approved_amendment(db, healthy_attempt, monkeypatch):
    from app.services.github_ci_watch_service import github_ci_watch_service

    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)
    await register_initial_pull(db, a, client, monkeypatch, "pr_opened")
    first_plan_note = a.item.initial_paths_readiness.copy()
    assert "src/added.py" in _work_item_response(a.item, a.scope).status_note
    a.item.dispatch_status = "ready_for_review"
    a.item.last_verified_sha = a.head
    client.pull["draft"] = False
    await db.commit()
    await github_verification_service.process_scope(db, a.scope, client=client)
    assert client.merge_calls == 0 and a.item.dispatch_status == "ready_for_review"
    assert a.item.status_note == "initial_paths_out_of_scope: 'src/added.py'"

    revision, approval, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)
    await github_approval_service.decide_continuation(db, a.item, authenticated_leader_member_id=a.leader.id,
        decision="approved", reason="Approve adding src/added.py", request_id=approval.id)
    await github_approval_service.ensure_continuation_decision_message(db, a.item, approval, revision)
    await github_approval_service.deliver_approved_continuation(db, a.item, approval, revision)
    await github_dispatch_service.activate_continuation_revision(db, a.item, a.scope, revision,
        authenticated_owner_member_id=a.owner.id, authenticated_owner_slot_id=a.owner.team_slot_id,
        dispatch_nonce=a.item.dispatch_nonce, lease_token=a.workspace.lease_token)
    assert "initial_paths_out_of_scope" not in _work_item_response(a.item, a.scope).status_note
    monkeypatch.setattr(client, "get_pull", AsyncMock(return_value=client.pull))
    await github_verification_service.submit_continuation_completion(
        db, a.item, a.scope, authenticated_owner_member_id=a.owner.id,
        authenticated_owner_slot_id=a.owner.team_slot_id, revision_number=revision.revision,
        dispatch_nonce=a.item.dispatch_nonce, current_head_sha=a.head, result_summary="Reviewed the amended paths",
        evidence={}, lease_token=a.workspace.lease_token, client=client)
    assert a.item.initial_paths_readiness is None
    # A stored first-plan result must stay hidden after the amendment submits for verification.
    a.item.initial_paths_readiness = first_plan_note
    assert "initial_paths_out_of_scope" not in (_work_item_response(a.item, a.scope).status_note or "")
    await db.refresh(a.item)
    await bind_submitted_review(db, a, revision)
    await require_jobs(db, a, monkeypatch)
    await settle_accepted(db, a, await record_accepted(db, a))

    monkeypatch.setattr(github_ci_watch_service, "_wake", AsyncMock(return_value=True))
    await github_verification_service.process_scope(db, a.scope, client=client)
    await db.refresh(a.item)
    await db.refresh(revision)
    assert a.item.active_scope_revision == revision.revision
    assert a.item.dispatch_status == "merged", a.item.status_note
    assert client.merge_head_shas == [a.head] and client.get_merge_base.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("healthy_attempt", ["with_companion", "without_paths"], indirect=True)
async def test_initial_path_gate_uses_only_the_boundary_in_the_approved_plan(db, healthy_attempt, monkeypatch):
    from app.services import companion_paths

    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch, extra_file=True)

    def map_unavailable():
        pytest.fail("Merge authority must come from the approved copy of the plan")

    monkeypatch.setattr(companion_paths, "load_map", map_unavailable)
    await github_verification_service.process_scope(db, a.scope, client=client)
    assert a.item.dispatch_status == "merged" and client.merge_head_shas == [a.head]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["base", "tree", "plan", "changed_base", "changed_plan"])
async def test_initial_path_gate_holds_when_the_comparison_cannot_be_proved(db, healthy_attempt, monkeypatch, missing):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch)
    if missing == "base":
        del client.pull["base"]["sha"]
    elif missing == "tree":
        client.get_recursive_tree.side_effect = GithubClientResponseError("recursive tree truncated")
    elif missing in {"plan", "changed_plan"}:
        root = await db.get(MailMessage, a.approval.request_message_id)
        tampered = {**root.payload, "plan_metadata": {"allowed_paths": ["src/added.py"]}}
        if missing == "plan":
            root.payload = tampered
            await db.commit()
        else:
            read_pull = client.get_pull

            async def change_plan_on_final_read(*args):
                if client.pull_calls == 1:
                    await db.execute(update(MailMessage).where(MailMessage.id == root.id)
                                     .values(payload=tampered).execution_options(synchronize_session=False))
                    await db.commit()
                return await read_pull(*args)

            client.get_pull = change_plan_on_final_read
    else:
        moved = {**client.pull, "base": {**client.pull["base"], "sha": "1" * 40}}
        client.get_pull = AsyncMock(side_effect=[client.pull, moved])
    await github_verification_service.process_scope(db, a.scope, client=client)
    assert a.item.dispatch_status == "ready_for_review" and client.merge_calls == 0
    assert a.item.status_note.startswith("initial_paths_inconclusive:")
    if missing == "plan":
        assert client.pull_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["status", "round", "owner", "leader"])
async def test_initial_path_gate_refuses_plan_with_unverifiable_authority(db, healthy_attempt, monkeypatch, mismatch):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch)
    approval = a.approval
    root = await db.get(MailMessage, approval.request_message_id)
    if mismatch == "status":
        approval.status = "rejected"
    elif mismatch == "round":
        approval.approval_round += 1
        root.payload = {**root.payload, "approval_round": approval.approval_round}
    elif mismatch == "owner":
        approval.owner_member_id = root.sender_member_id = a.leader.id
    else:
        approval.leader_member_id = root.recipient_member_id = a.owner.id
    # The Mail still describes this persisted request. Each invalidity is in its merge authority.
    assert github_approval_service.matches_linked_request_message(
        approval, root, delivery_key=f"github-approval:{approval.id}:request")
    await db.commit()

    await github_verification_service.process_scope(db, a.scope, client=client)

    assert client.merge_calls == 0 and a.item.dispatch_status == "ready_for_review"
    assert a.item.status_note == "initial_paths_inconclusive: the approved first plan could not be verified."
    assert client.pull_calls == 1
    client.get_merge_base.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_path_gate_refuses_a_different_valid_plan_on_the_final_read(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch)
    root = await db.get(MailMessage, a.approval.request_message_id)
    read_pull = client.get_pull

    async def read_with_changed_plan(*args):
        pull = await read_pull(*args)
        if client.pull_calls == 2:
            metadata = {"allowed_paths": ["src/base.py", "src/added.py"]}
            root.payload = {**root.payload, "plan_metadata": metadata}
            a.approval.request_fingerprint = github_approval_service.initial_request_fingerprint(
                summary=root.payload["summary"], plan_metadata=metadata)
            await db.commit()
        return pull

    client.get_pull = read_with_changed_plan
    await github_verification_service.process_scope(db, a.scope, client=client)

    assert client.merge_calls == 0 and a.item.dispatch_status == "ready_for_review"
    assert a.item.status_note == "initial_paths_inconclusive: the approved file list changed during the comparison."
    client.get_merge_base.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["item", "scope"])
async def test_initial_path_gate_fences_readiness_when_context_changes_during_pr_read(
    db, healthy_attempt, monkeypatch, changed,
):
    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch)
    a.item.status_note = "Current readiness note"
    await db.commit()
    read_pull = client.get_pull

    async def read_with_changed_context(*args):
        pull = await read_pull(*args)
        if client.pull_calls == 2:
            change = (update(GithubWorkItem).where(GithubWorkItem.id == a.item.id).values(last_verified_sha="1" * 40)
                      if changed == "item"
                      else update(TeamGithubScope).where(TeamGithubScope.id == a.scope.id).values(enabled=False))
            await db.execute(change.execution_options(synchronize_session=False))
            await db.commit()
            pull["draft"] = True
        return pull

    client.get_pull = read_with_changed_context
    await github_verification_service.process_scope(db, a.scope, client=client)
    await db.refresh(a.item)

    # The late draft observation cannot rewrite readiness after the attempt or scope changed.
    assert client.merge_calls == 0 and client.pull_calls == 2
    assert a.item.status_note == "Current readiness note"


@pytest.mark.asyncio
async def test_initial_path_gate_observes_hold_after_the_final_pr_read(db, healthy_attempt, monkeypatch):
    from app.services import github_coordination_service

    a = healthy_attempt
    client, _ = await initial_path_merge_case(db, a, monkeypatch)
    monkeypatch.setattr(github_coordination_service, "hold_code",
                        lambda: "test_hold" if client.pull_calls >= 2 else None)

    await github_verification_service.process_scope(db, a.scope, client=client)

    assert client.merge_calls == 0 and a.item.dispatch_status == "ready_for_review"
    assert client.pull_calls == 2
    client.get_merge_base.assert_awaited_once()


async def prior_amendment(a, db, number, added, *, status="rejected", approved=False):
    """A past amendment row. ``approved`` marks one the Leader approved at some point, whatever it became since."""
    from app.models.database import GithubAttemptScopeRevision
    row = GithubAttemptScopeRevision(work_item_id=a.item.id, dispatch_nonce=a.item.dispatch_nonce,
        revision=number, owner_slot_id=a.owner.team_slot_id, owner_member_id=a.owner.id,
        origin_kind="scope_amendment", amendment_base_paths=["src/base.py"],
        amendment_added_paths=added, phase="implementation", execution_target="workspace",
        summary="Past audited amendment", allowed_paths=["src/base.py", *added], allowed_actions=[],
        allowed_commands=[], prohibited_actions=[], tool_fallbacks={}, baseline_head_sha=a.head,
        baseline_tree_sha=a.tree, originating_escalation_reason="", expected_workspace_id=a.workspace.id,
        expected_lease_token_hash=github_approval_service.lease_token_hash(a.workspace.lease_token),
        max_failed_heads=1, failed_head_count=0, status=status,
        approved_at=datetime.utcnow() if approved else None)
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,prior,allowed", [(3, 2, True), (3, 3, False), (1, 0, True), (1, 1, False)])
async def test_per_attempt_amendment_count_has_an_exact_limit_and_no_replay_charge(
    db, healthy_attempt, limit, prior, allowed
):
    a = healthy_attempt
    a.scope.max_scope_amendments = limit
    await db.commit()
    for number in range(1, prior + 1):
        await prior_amendment(a, db, number, [], status="completed", approved=True)
    if allowed:
        revision, approval, _ = await github_approval_service.create_scope_amendment_request(
            db, a.item, a.scope, **amendment(a))
        assert revision.revision == limit
        replay = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
        assert not replay[2] and replay[1].id == approval.id
    else:
        with pytest.raises(GithubApprovalError, match="amendment_count_limit"):
            await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))


@pytest.mark.asyncio
@pytest.mark.parametrize("readd", [True, False])
async def test_added_path_history_is_cumulative_and_removal_does_not_refund_it(db, healthy_attempt, readd):
    a = healthy_attempt
    await prior_amendment(a, db, 1, [f"src/historical-{n}.py" for n in range(16)])
    wanted = ["src/base.py", "src/historical-0.py" if readd else "src/new.py"]
    if readd:
        revision, _, _ = await github_approval_service.create_scope_amendment_request(
            db, a.item, a.scope, **amendment(a, wanted))
        assert revision.allowed_paths == wanted
    else:
        with pytest.raises(GithubApprovalError, match="amendment_added_path_limit"):
            await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a, wanted))


@pytest.mark.asyncio
async def test_amendments_require_a_closed_file_set_instead_of_unbounded_directory_grants(db, healthy_attempt):
    a = healthy_attempt
    with pytest.raises(GithubApprovalError, match="amendment_closed_file_paths_required"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope,
            **amendment(a, ["src/"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion", ["cancelled", "timed_out"])
@pytest.mark.parametrize("continuation", [False, True])
async def test_inconclusive_execution_never_charges_a_product_failed_head(db, conclusion, continuation):
    scope = await _scope(db)
    slot, member = await _owner(db, scope)
    item = await _item(db, scope, dispatch_status="verifying", pr_number=5, owner_slot_id=slot.id,
        dispatch_nonce="inconclusive", active_scope_revision=int(continuation), retry_count=0)
    revision = None
    if continuation:
        revision, _ = await _implementation_revision(db, scope, item, slot, member, max_failed_heads=1)
    client = _Client(check_runs=[{"name": "backend", "status": "completed", "conclusion": conclusion}])
    for _ in range(2):
        await github_verification_service.process_scope(db, scope, client=client)
    await db.refresh(item)
    assert item.retry_count == 0
    assert item.last_verified_sha is None
    assert item.dispatch_status == "verifying"
    assert conclusion in item.status_note
    if revision:
        await db.refresh(revision)
        assert revision.failed_head_count == 0 and revision.status == "submitted"
    assert client.ready_calls == client.merge_calls == 0


@pytest.mark.asyncio
async def test_mixed_real_failure_and_cancelled_execution_charges_only_once(db):
    scope = await _scope(db)
    slot, member = await _owner(db, scope)
    item = await _item(db, scope, dispatch_status="verifying", pr_number=5, owner_slot_id=slot.id,
        dispatch_nonce="mixed", active_scope_revision=1)
    revision, _ = await _implementation_revision(db, scope, item, slot, member)
    client = _Client(check_runs=[{"name": n, "status": "completed", "conclusion": c}
        for n, c in [("backend", "failure"), ("e2e", "cancelled")]])
    for _ in range(2):
        await github_verification_service.process_scope(db, scope, client=client)
    await db.refresh(revision)
    assert revision.failed_head_count == 1 and revision.last_failed_head_sha == "sha"
    assert client.merge_calls == 0


@pytest.mark.asyncio
async def test_escalated_closed_unmerged_supersedes_submitted_revision_without_retry(db):
    scope = await _scope(db)
    (await db.get(AgentTeamPreset, scope.preset_id)).autonomy_enabled = True
    scope.enabled = True
    slot, member = await _owner(db, scope)
    item = await _item(db, scope, dispatch_status="escalated", escalation_reason="plan_blocked",
        pr_number=5, owner_slot_id=slot.id, dispatch_nonce="closed", active_scope_revision=1)
    revision, workspace = await _implementation_revision(db, scope, item, slot, member)
    revision.submitted_head_sha = "a" * 40
    revision.expected_lease_token_hash = github_approval_service.lease_token_hash(workspace.lease_token)
    await db.commit()
    client = _Client(pull={"number": 5, "state": "closed", "merged": False, "merged_at": None,
        "head": {"sha": "a" * 40}})
    for _ in range(2):
        await github_verification_service.process_scope(db, scope, client=client)
    await db.refresh(revision)
    await db.refresh(workspace)
    assert revision.status == "superseded"
    assert workspace.leased_item_id == item.id and item.retry_count == 0
    assert client.ready_calls == client.merge_calls == 0


async def submitted(a, db, *, diagnostic=False):
    from app.models.database import GithubAttemptScopeRevision
    item = a.item
    item.pr_number = 5
    item.dispatch_status = "verifying"
    item.active_scope_revision = 1
    item.attempt_phase = "diagnostic" if diagnostic else "implementation"
    approval = GithubApprovalRequest(work_item_id=item.id, request_kind="continuation",
        dispatch_nonce=item.dispatch_nonce, approval_round=1, owner_member_id=a.owner.id,
        leader_member_id=a.leader.id, request_fingerprint="submitted-fixture", status="approved")
    db.add(approval)
    await db.flush()
    revision = GithubAttemptScopeRevision(work_item_id=item.id, dispatch_nonce=item.dispatch_nonce,
        revision=1, owner_slot_id=item.owner_slot_id, owner_member_id=a.owner.id, phase=item.attempt_phase,
        execution_target="workspace", summary="Submitted fixture", allowed_paths=["src/base.py"],
        allowed_actions=["revert_diagnostic_changes"] if diagnostic else ["push_pr_head", "request_verification"],
        allowed_commands=[], prohibited_actions=[], tool_fallbacks={}, baseline_head_sha=a.head,
        baseline_tree_sha=a.tree, originating_escalation_reason="plan_blocked",
        expected_workspace_id=a.workspace.id,
        expected_lease_token_hash=github_approval_service.lease_token_hash(a.workspace.lease_token),
        max_failed_heads=1, failed_head_count=0, status="submitted", submitted_head_sha=a.head,
        submitted_at=datetime.utcnow(), approval_request_id=approval.id)
    db.add(revision)
    await db.flush()
    approval.scope_revision_id = revision.id
    await db.commit()
    return revision


@pytest.mark.asyncio
async def test_operator_can_cancel_submitted_draft_and_preserve_attempt(db, healthy_attempt):
    a = healthy_attempt
    revision = await submitted(a, db)
    _, ended, changed = await github_approval_service.cancel_active_continuation(db, a.item,
        revision_number=1, dispatch_nonce=a.item.dispatch_nonce, reason="Submitted review needs a new scope")
    assert changed and ended.status == "superseded"
    assert a.item.active_scope_revision == 0 and a.item.dispatch_status == "escalated"
    assert a.item.escalation_reason == "plan_blocked"
    assert a.workspace.leased_item_id == a.item.id and revision.failed_head_count == 0


@pytest.mark.asyncio
async def test_diagnostic_restoration_uses_tree_even_when_restore_commit_differs_and_budget_exhausted(
    db, healthy_attempt, monkeypatch
):
    a = healthy_attempt
    revision = await submitted(a, db, diagnostic=True)
    revision.status = "exhausted"
    revision.failed_head_count = 1
    revision.last_failed_head_sha = "f" * 40
    a.item.dispatch_status = "escalated"
    a.item.escalation_reason = "continuation_budget_exhausted"
    a.pull["head"]["sha"] = "f" * 40
    monkeypatch.setattr(github_client, "get_commit_snapshot", AsyncMock(return_value=
        GithubCommitSnapshot(sha="f" * 40, tree_sha=a.tree)))
    await db.commit()
    _, ended, changed = await github_approval_service.cancel_active_continuation(db, a.item,
        revision_number=1, dispatch_nonce=a.item.dispatch_nonce, reason="Exact diagnostic baseline restored")
    assert changed and ended.status == "superseded"
    assert ended.failed_head_count == 1 and a.workspace.leased_item_id == a.item.id


@pytest_asyncio.fixture
async def submitted_attempt(db, healthy_attempt, request):
    a = healthy_attempt
    for number in range(1, getattr(request, "param", 1)):
        await prior_amendment(a, db, number, [], status="completed", approved=True)
    await submit_amendment_for_review(db, a)
    return a


async def submit_amendment_for_review(db, a):
    revision, approval, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)
    await github_approval_service.decide_continuation(db, a.item,
        authenticated_leader_member_id=a.leader.id, decision="approved", reason="Reviewed amendment",
        request_id=approval.id)
    await github_approval_service.ensure_continuation_decision_message(db, a.item, approval, revision)
    await github_approval_service.deliver_approved_continuation(db, a.item, approval, revision)
    await github_dispatch_service.activate_continuation_revision(db, a.item, a.scope, revision,
        authenticated_owner_member_id=a.owner.id, authenticated_owner_slot_id=a.owner.team_slot_id,
        dispatch_nonce=a.item.dispatch_nonce, lease_token=a.workspace.lease_token)
    revision.status = "submitted"
    revision.submitted_head_sha = a.head
    revision.submitted_at = datetime.utcnow()
    a.item.pr_number = 5
    a.item.dispatch_status = "verifying"
    await bind_submitted_review(db, a, revision)


async def bind_submitted_review(db, a, revision):
    from app.models.database import AgentTeamSlot, MailTeamMember, GithubRequiredCheckWatch
    from app.services.factory_delivery_policy import effective_policy
    from app.services.github_ci_watch_service import _digest

    reviewer = getattr(a, "reviewer", None)
    if reviewer is None:
        slot = AgentTeamSlot(preset_id=a.preset.id, position=2, display_name="Reviewer", provider="codex-cli",
            repo_id="approval", repo_path=str(a.repo), repo_name="approval", launch_mode="plain",
            launch_options={}, enabled=True)
        db.add(slot)
        await db.flush()
        reviewer = MailTeamMember(identity_key=f"reviewer:{slot.id}", repo_id="approval", repo_path=str(a.repo),
            repo_name="approval", display_name="Reviewer", participant_kind="team_slot",
            team_preset_id=a.preset.id, team_slot_id=slot.id)
        db.add(reviewer)
        await db.flush()
    watch = GithubRequiredCheckWatch(work_item_id=a.item.id, dispatch_nonce=a.item.dispatch_nonce,
        scope_revision=revision.revision, pr_number=5, head_sha=a.head, owner_slot_id=a.owner.team_slot_id,
        reviewer_member_id=reviewer.id, policy_digest=_digest(effective_policy(a.item, a.scope)),
        observed_at=datetime.utcnow(), changed_at=datetime.utcnow())
    db.add(watch)
    await db.commit()
    a.revision, a.reviewer, a.watch = revision, reviewer, watch


@pytest_asyncio.fixture
async def submitted_continuation(db, healthy_attempt, monkeypatch, request):
    a = healthy_attempt
    approved, rejected = getattr(request, "param", (0, 0))
    for number in range(1, approved + 1):
        await prior_amendment(a, db, number, [], status="completed", approved=True)
    if rejected:
        a.scope.max_continuation_revisions = 10
        for number in range(approved + 1, approved + rejected + 1):
            await prior_amendment(a, db, number, [])
    monkeypatch.setattr(github_client, "get_recursive_tree", AsyncMock(return_value={}))
    a.item.pr_number = 5
    await github_dispatch_service.escalate_without_notification(db, a.item, "plan_blocked")
    revision, approval, _ = await github_approval_service.create_continuation_request(
        db, a.item, a.scope, **amendment(a, ["src/base.py"]))
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)
    await github_approval_service.decide_continuation(db, a.item,
        authenticated_leader_member_id=a.leader.id, decision="approved", reason="Review recovery",
        request_id=approval.id)
    await github_approval_service.ensure_continuation_decision_message(db, a.item, approval, revision)
    await github_approval_service.deliver_approved_continuation(db, a.item, approval, revision)
    await github_dispatch_service.activate_continuation_revision(
        db, a.item, a.scope, revision, authenticated_owner_member_id=a.owner.id,
        authenticated_owner_slot_id=a.owner.team_slot_id, dispatch_nonce=a.item.dispatch_nonce,
        lease_token=a.workspace.lease_token)
    await github_verification_service.submit_continuation_completion(
        db, a.item, a.scope, authenticated_owner_member_id=a.owner.id,
        authenticated_owner_slot_id=a.owner.team_slot_id, revision_number=revision.revision,
        dispatch_nonce=a.item.dispatch_nonce, current_head_sha=a.head,
        result_summary="Submitted implementation for review", evidence={}, lease_token=a.workspace.lease_token)
    await db.refresh(revision)
    await bind_submitted_review(db, a, revision)
    return a


async def record_changes(db, a, **overrides):
    from app.services.github_terminal_revision_service import record_revision_review
    args = dict(revision_number=a.revision.revision, dispatch_nonce=a.item.dispatch_nonce,
        head_sha=a.head, reviewer_member_id=a.reviewer.id, verdict="changes_required",
        summary="Current independent review requires bounded corrections",
        evidence_url="https://github.com/o/approval/pull/5#pullrequestreview-123")
    args.update(overrides)
    return await record_revision_review(db, a.item, **args)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission", "approval", "activation"])
@pytest.mark.parametrize("during_write", [False, True], ids=["before_read", "at_write"])
async def test_tightened_continuation_reasons_are_enforced_at_each_grant(
    db, healthy_attempt, monkeypatch, stage, during_write,
):
    a = healthy_attempt
    a.item.pr_number = 5
    await github_dispatch_service.escalate_without_notification(db, a.item, "plan_blocked")
    monkeypatch.setattr(github_client, "get_recursive_tree", AsyncMock(return_value={}))

    refusal = "stale_continuation_context" if during_write else "continuation_reason_not_allowed"

    async def tighten():
        reasons = ["retry_count_exhausted"]
        if not during_write:
            a.scope.continuation_allowed_reasons = reasons
            await db.commit()
            return
        execute = db.execute
        scope_id = a.scope.id

        async def execute_with_policy_change(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == GithubWorkItem.__tablename__:
                await execute(update(TeamGithubScope).where(TeamGithubScope.id == scope_id)
                    .values(continuation_allowed_reasons=reasons).execution_options(synchronize_session=False))
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", execute_with_policy_change)

    async def request():
        return await github_approval_service.create_continuation_request(
            db, a.item, a.scope, **amendment(a, ["src/base.py"]))

    if stage == "admission":
        await tighten()
        with pytest.raises(GithubApprovalError, match=refusal):
            await request()
        return
    revision, approval, _ = await request()
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)

    async def approve():
        return await github_approval_service.decide_continuation(
            db, a.item, authenticated_leader_member_id=a.leader.id, decision="approved", reason="Reviewed",
            request_id=approval.id)

    if stage == "approval":
        await tighten()
        with pytest.raises(GithubApprovalError, match=refusal):
            await approve()
        return
    await approve()
    await github_approval_service.ensure_continuation_decision_message(db, a.item, approval, revision)
    await github_approval_service.deliver_approved_continuation(db, a.item, approval, revision)
    await tighten()
    with pytest.raises(ValueError, match=refusal):
        await github_dispatch_service.activate_continuation_revision(
            db, a.item, a.scope, revision, authenticated_owner_member_id=a.owner.id,
            authenticated_owner_slot_id=a.owner.team_slot_id, dispatch_nonce=a.item.dispatch_nonce,
            lease_token=a.workspace.lease_token)


@pytest.mark.asyncio
async def test_explicit_verifier_roles_constrain_named_revision_verdicts(db, submitted_attempt):
    from app.models.database import AgentTeamSlot

    a = submitted_attempt
    a.scope.verifier_roles = ["reviewer"]
    await db.commit()
    with pytest.raises(GithubApprovalError, match="current_independent_reviewer_required"):
        await record_changes(db, a)
    (await db.get(AgentTeamSlot, a.reviewer.team_slot_id)).role = "Reviewer"
    await db.commit()
    assert (await record_changes(db, a)).reviewer_member_id == a.reviewer.id
    (await db.get(AgentTeamSlot, a.owner.team_slot_id)).role = "Reviewer"
    a.watch.reviewer_member_id = a.owner.id
    await db.commit()
    with pytest.raises(GithubApprovalError, match="current_independent_reviewer_required"):
        await record_changes(db, a, reviewer_member_id=a.owner.id)


@pytest.mark.asyncio
async def test_current_independent_changes_required_needs_leader_settlement_and_fresh_amendment(db, submitted_attempt):
    from app.services.github_terminal_revision_service import end_submitted_revision
    a = submitted_attempt
    original = (a.item.retry_count, a.revision.failed_head_count, a.workspace.lease_token)
    review = await record_changes(db, a)
    assert a.revision.status == "submitted"  # Reviewer fact alone grants no source authority.
    await end_submitted_revision(db, a.item, review_id=review.id,
        leader_member_id=a.leader.id, dispatch_nonce=a.item.dispatch_nonce, reason="Accept negative disposition")
    await db.refresh(a.revision)
    assert a.revision.status == "superseded" and a.item.dispatch_status == "dispatched"
    assert a.item.active_scope_revision == a.revision.revision and a.item.ack_received_at is None
    assert a.item.escalation_reason is None
    assert original == (a.item.retry_count, a.revision.failed_head_count, a.workspace.lease_token)
    await db.refresh(review)
    assert review.settled_by_member_id == a.leader.id and review.settlement_reason == 'Accept negative disposition'
    replay = await end_submitted_revision(db, a.item, review_id=review.id,
        leader_member_id=a.leader.id, dispatch_nonce=a.item.dispatch_nonce, reason='Accept negative disposition')
    assert replay.id == a.revision.id and replay.status == 'superseded'
    with pytest.raises(GithubApprovalError):
        await end_submitted_revision(db, a.item, review_id=review.id, leader_member_id=a.leader.id,
            dispatch_nonce=a.item.dispatch_nonce, reason='A different settlement is not a replay')
    with pytest.raises(GithubApprovalError, match="continuation_not_escalated"):
        await github_approval_service.create_continuation_request(db, a.item, a.scope, **amendment(a))
    next_revision, next_request, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a, ["src/base.py", "src/added.py", "src/followup.py"]))
    assert next_revision.status == "proposed" and next_revision.previous_scope_revision == a.revision.revision
    await github_approval_service.ensure_continuation_request_message(db, a.item, next_request, next_revision)
    await github_approval_service.decide_continuation(db, a.item,
        authenticated_leader_member_id=a.leader.id, decision='approved', reason='Fresh correction authority',
        request_id=next_request.id)
    await github_approval_service.ensure_continuation_decision_message(db, a.item, next_request, next_revision)
    await github_approval_service.deliver_approved_continuation(db, a.item, next_request, next_revision)
    assert await github_dispatch_service.activate_continuation_revision(db, a.item, a.scope, next_revision,
        authenticated_owner_member_id=a.owner.id, authenticated_owner_slot_id=a.owner.team_slot_id,
        dispatch_nonce=a.item.dispatch_nonce, lease_token=a.workspace.lease_token)
    with pytest.raises(ValueError):
        await github_dispatch_service.activate_continuation_revision(db, a.item, a.scope, a.revision,
            authenticated_owner_member_id=a.owner.id, authenticated_owner_slot_id=a.owner.team_slot_id,
            dispatch_nonce=a.item.dispatch_nonce, lease_token=a.workspace.lease_token)
    assert a.item.active_scope_revision == next_revision.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_attempt", [3], indirect=True)
async def test_third_amendment_settlement_opens_the_normal_continuation_route(db, submitted_attempt, monkeypatch):
    from app.services.github_terminal_revision_service import end_submitted_revision

    a = submitted_attempt
    assert a.revision.revision == 3 and a.revision.status == "submitted"
    a.item.retry_count = 2
    a.item.effort_limit_minutes = 55
    a.item.effort_elapsed_seconds = 1200
    a.revision.failed_head_count = 1
    await db.commit()
    before = (a.item.dispatch_nonce, a.item.pr_number, a.item.approval_round_count,
              a.item.retry_count, a.item.effort_limit_minutes, a.item.effort_elapsed_seconds,
              a.revision.failed_head_count, a.workspace.lease_token)
    fact = await record_changes(db, a)
    decision = dict(review_id=fact.id, leader_member_id=a.leader.id, dispatch_nonce=a.item.dispatch_nonce,
                    reason="Settle the capped amendment")
    await end_submitted_revision(db, a.item, **decision)
    await db.refresh(a.revision)
    await db.refresh(fact)
    assert a.item.dispatch_status == "escalated"
    assert a.item.escalation_reason == "amendment_budget_exhausted"
    assert a.item.active_scope_revision == 3 and a.item.ack_received_at is None
    assert a.revision.status == "superseded" and fact.status == "consumed"
    assert before == (a.item.dispatch_nonce, a.item.pr_number, a.item.approval_round_count,
                      a.item.retry_count, a.item.effort_limit_minutes, a.item.effort_elapsed_seconds,
                      a.revision.failed_head_count, a.workspace.lease_token)
    settled_at = a.revision.completed_at
    replay = await end_submitted_revision(db, a.item, **decision)
    assert replay.id == a.revision.id and replay.completed_at == settled_at
    with pytest.raises(GithubApprovalError, match="current_leader_and_review_required"):
        await end_submitted_revision(db, a.item, **{**decision, "reason": "A different disposition"})

    monkeypatch.setattr(github_client, "get_recursive_tree", AsyncMock(return_value={}))
    a.scope.max_continuation_revisions = 3
    await db.commit()
    with pytest.raises(GithubApprovalError, match="continuation_budget_exhausted"):
        await github_approval_service.create_continuation_request(db, a.item, a.scope, **amendment(a))
    a.scope.max_continuation_revisions = 6
    await db.commit()
    revision, request, created = await github_approval_service.create_continuation_request(
        db, a.item, a.scope, **amendment(a))
    assert created and revision.revision == 4 and revision.status == "proposed"
    assert revision.originating_escalation_reason == "amendment_budget_exhausted"
    await github_approval_service.ensure_continuation_request_message(db, a.item, request, revision)
    await github_approval_service.decide_continuation(db, a.item,
        authenticated_leader_member_id=a.leader.id, decision="approved", reason="Normal bounded recovery",
        request_id=request.id)
    await github_approval_service.ensure_continuation_decision_message(db, a.item, request, revision)
    await github_approval_service.deliver_approved_continuation(db, a.item, request, revision)
    assert a.item.ack_received_at is None
    assert await github_dispatch_service.activate_continuation_revision(
        db, a.item, a.scope, revision, authenticated_owner_member_id=a.owner.id,
        authenticated_owner_slot_id=a.owner.team_slot_id, dispatch_nonce=a.item.dispatch_nonce,
        lease_token=a.workspace.lease_token)
    assert a.item.active_scope_revision == revision.revision and a.item.dispatch_status == "dispatched"
    assert a.item.effort_limit_minutes == 55 and a.item.effort_elapsed_seconds == 1200


@pytest.mark.asyncio
@pytest.mark.parametrize("request_count,excluded", [
    (8, None),
    (7, None),
    (7, "other_item"),
    (7, "prior_attempt"),
    (7, "continuation"),
], ids=["cap", "below_cap", "other_item", "prior_attempt", "continuation"])
async def test_settlement_uses_the_amendment_request_cap(
    db, healthy_attempt, monkeypatch, request_count, excluded,
):
    from app.services.github_terminal_revision_service import end_submitted_revision

    a = healthy_attempt
    a.scope.max_continuation_revisions = 10
    outcomes = ("rejected", "expired", "cancelled", "superseded")
    for number in range(1, request_count):
        await prior_amendment(a, db, number, [], status=outcomes[(number - 1) % len(outcomes)])
    if excluded:
        earlier = await prior_amendment(a, db, request_count, [], status="completed")
        if excluded == "other_item":
            other = await _item(db, a.scope, issue_number=99, dispatch_nonce=a.item.dispatch_nonce,
                                ack_approver_member_id=a.leader.id, owner_slot_id=a.owner.team_slot_id)
            earlier.work_item_id = other.id
        elif excluded == "prior_attempt":
            earlier.dispatch_nonce = "previous-attempt"
        else:
            earlier.origin_kind = "continuation"
            earlier.approved_at = datetime.utcnow()
        await db.commit()

    await submit_amendment_for_review(db, a)
    fact = await record_changes(db, a)
    decision = dict(review_id=fact.id, leader_member_id=a.leader.id, dispatch_nonce=a.item.dispatch_nonce,
                    reason="Settle after the amendment request history")
    await end_submitted_revision(db, a.item, **decision)
    await db.refresh(fact)
    assert a.revision.status == "superseded" and fact.status == "consumed"
    assert a.item.active_scope_revision == a.revision.revision and a.item.ack_received_at is None
    assert a.item.pr_number == 5 and a.workspace.leased_item_id == a.item.id

    if request_count == 8:
        assert a.item.dispatch_status == "escalated"
        assert a.item.escalation_reason == "amendment_budget_exhausted"
        assert (await end_submitted_revision(db, a.item, **decision)).id == a.revision.id

        monkeypatch.setattr(github_client, "get_recursive_tree", AsyncMock(return_value={}))
        revision, _, created = await github_approval_service.create_continuation_request(
            db, a.item, a.scope, **amendment(a))
        assert created and revision.revision == 9 and revision.status == "proposed"
        assert revision.originating_escalation_reason == "amendment_budget_exhausted"
    else:
        assert a.item.dispatch_status == "dispatched" and a.item.escalation_reason is None
        with pytest.raises(GithubApprovalError, match="continuation_not_escalated"):
            await github_approval_service.create_continuation_request(db, a.item, a.scope, **amendment(a))
        revision, _, created = await github_approval_service.create_scope_amendment_request(
            db, a.item, a.scope, **amendment(a, ["src/base.py", "src/added.py", "src/followup.py"]))
        assert created and revision.origin_kind == "scope_amendment" and revision.status == "proposed"


@pytest.mark.asyncio
async def test_approved_continuation_does_not_count_towards_amendment_cap(db, submitted_continuation):
    from app.services.github_terminal_revision_service import end_submitted_revision

    a = submitted_continuation
    assert a.revision.origin_kind == "continuation" and a.revision.approved_at is not None
    for reason in ("Settle the approved continuation", "Settle the first amendment"):
        fact = await record_changes(db, a)
        await end_submitted_revision(db, a.item, review_id=fact.id, leader_member_id=a.leader.id,
            dispatch_nonce=a.item.dispatch_nonce, reason=reason)
        await submit_amendment_for_review(db, a)

    assert a.revision.revision == 3 and a.revision.origin_kind == "scope_amendment"
    fact = await record_changes(db, a)
    await end_submitted_revision(db, a.item, review_id=fact.id, leader_member_id=a.leader.id,
        dispatch_nonce=a.item.dispatch_nonce, reason="Settle the second approved amendment")
    assert a.item.dispatch_status == "dispatched" and a.item.escalation_reason is None


@pytest.mark.asyncio
async def test_capped_item_rejects_replay_of_an_older_consumed_review(db, submitted_attempt):
    from app.services.github_terminal_revision_service import end_submitted_revision

    a = submitted_attempt
    old_fact = await record_changes(db, a)
    old_decision = dict(review_id=old_fact.id, leader_member_id=a.leader.id,
        dispatch_nonce=a.item.dispatch_nonce, reason="Settle the first amendment")
    await end_submitted_revision(db, a.item, **old_decision)
    await db.refresh(old_fact)
    assert old_fact.status == "consumed"

    for reason in ("Settle the second amendment", "Settle the capped amendment"):
        await submit_amendment_for_review(db, a)
        fact = await record_changes(db, a)
        await end_submitted_revision(db, a.item, review_id=fact.id, leader_member_id=a.leader.id,
            dispatch_nonce=a.item.dispatch_nonce, reason=reason)

    assert a.item.active_scope_revision == 3 and a.item.dispatch_status == "escalated"
    assert a.item.escalation_reason == "amendment_budget_exhausted"
    with pytest.raises(GithubApprovalError, match="current_leader_and_review_required"):
        await end_submitted_revision(db, a.item, **old_decision)


@pytest.mark.asyncio
@pytest.mark.parametrize("ignored", ["rejected", "prior_attempt"])
async def test_settlement_counts_only_approved_amendments_of_this_attempt(db, submitted_attempt, ignored):
    from app.services.github_terminal_revision_service import end_submitted_revision

    a = submitted_attempt
    for number in (2, 3):
        if ignored == "rejected":
            await prior_amendment(a, db, number, [])
        else:
            earlier = await prior_amendment(a, db, number, [], status="completed", approved=True)
            earlier.dispatch_nonce = "previous-attempt"
            await db.commit()
    fact = await record_changes(db, a)
    await end_submitted_revision(db, a.item, review_id=fact.id, leader_member_id=a.leader.id,
        dispatch_nonce=a.item.dispatch_nonce, reason="Settle an amendment below its cap")
    assert a.item.dispatch_status == "dispatched" and a.item.escalation_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_continuation,capped", [
    ((3, 0), True),
    ((2, 0), False),
    ((0, 8), True),
    ((0, 7), False),
], indirect=["submitted_continuation"], ids=["approval_cap", "below_approval_cap", "request_cap", "below_request_cap"])
async def test_settling_a_continuation_uses_the_amendment_caps(db, submitted_continuation, capped):
    from app.services.github_terminal_revision_service import end_submitted_revision

    a = submitted_continuation
    fact = await record_changes(db, a)
    decision = dict(review_id=fact.id, leader_member_id=a.leader.id,
                    dispatch_nonce=a.item.dispatch_nonce, reason="Settle the submitted continuation")
    await end_submitted_revision(db, a.item, **decision)
    await db.refresh(fact)
    assert a.revision.origin_kind == "continuation"
    assert a.revision.status == "superseded" and fact.status == "consumed"
    assert a.item.active_scope_revision == a.revision.revision and a.item.ack_received_at is None
    assert a.workspace.leased_item_id == a.item.id

    if capped:
        assert a.item.dispatch_status == "escalated"
        assert a.item.escalation_reason == "amendment_budget_exhausted"
        settled_at = a.revision.completed_at
        replay = await end_submitted_revision(db, a.item, **decision)
        assert replay.id == a.revision.id and replay.completed_at == settled_at
        revision, _, created = await github_approval_service.create_continuation_request(
            db, a.item, a.scope, **amendment(a))
        assert created and revision.status == "proposed" and revision.revision == a.revision.revision + 1
        assert revision.originating_escalation_reason == "amendment_budget_exhausted"
    else:
        assert a.item.dispatch_status == "dispatched" and a.item.escalation_reason is None
        with pytest.raises(GithubApprovalError, match="continuation_not_escalated"):
            await github_approval_service.create_continuation_request(db, a.item, a.scope, **amendment(a))
        revision, _, created = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
        assert created and revision.origin_kind == "scope_amendment" and revision.status == "proposed"


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_attempt", [3], indirect=True)
async def test_cap_escalation_rolls_back_if_hold_arrives_during_settlement(db, submitted_attempt, monkeypatch):
    from app.services import github_coordination_service as coordination
    from app.services import github_terminal_revision_service as terminal

    a = submitted_attempt
    fact = await record_changes(db, a)
    original_audit = terminal._review_audit

    async def audit_then_hold(*args, **kwargs):
        await original_audit(*args, **kwargs)
        monkeypatch.setattr(coordination, "hold_code", lambda: "factory_hold")

    monkeypatch.setattr(terminal, "_review_audit", audit_then_hold)
    with pytest.raises(GithubApprovalError, match="stale_revision_review"):
        await terminal.end_submitted_revision(db, a.item, review_id=fact.id, leader_member_id=a.leader.id,
            dispatch_nonce=a.item.dispatch_nonce, reason="Settle with a concurrent hold")
    await db.refresh(a.item)
    await db.refresh(a.revision)
    await db.refresh(fact)
    assert a.item.dispatch_status == "verifying" and a.item.escalation_reason is None
    assert a.item.ack_received_at is not None
    assert a.revision.status == "submitted" and fact.status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["owner", "old_head", "wrong_verdict", "unassigned"])
async def test_review_fact_rejects_unbound_or_positive_authority(db, submitted_attempt, invalid):
    a = submitted_attempt
    kwargs = {"owner": {"reviewer_member_id": a.owner.id}, "old_head": {"head_sha": "a" * 40},
        "wrong_verdict": {"verdict": "approved"}, "unassigned": {"reviewer_member_id": a.leader.id}}[invalid]
    with pytest.raises(GithubApprovalError):
        await record_changes(db, a, **kwargs)
    assert a.revision.status == "submitted"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["withdraw", "reviewer", "head", "lease", "leader"])
async def test_review_settlement_revalidates_exact_current_disposition(db, submitted_attempt, change):
    from app.services.github_terminal_revision_service import end_submitted_revision, withdraw_revision_review
    a = submitted_attempt
    review = await record_changes(db, a)
    if change == "withdraw":
        await withdraw_revision_review(db, a.item, review_id=review.id,
            reviewer_member_id=a.reviewer.id, dispatch_nonce=a.item.dispatch_nonce)
    elif change == "reviewer":
        a.watch.reviewer_member_id = a.leader.id
    elif change == "head":
        a.pull["head"]["sha"] = "a" * 40
    elif change == "lease":
        a.workspace.lease_token = "replacement-lease"
    await db.commit()
    with pytest.raises(GithubApprovalError):
        await end_submitted_revision(db, a.item, review_id=review.id,
            leader_member_id=a.owner.id if change == "leader" else a.leader.id,
            dispatch_nonce=a.item.dispatch_nonce, reason="Cannot use stale disposition")
    await db.refresh(a.revision)
    assert a.revision.status == "submitted"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["approved", "rejected", "active"])
async def test_amendment_round_replay_never_consumes_another_unit(db, healthy_attempt, state):
    a = healthy_attempt
    revision, approval, _ = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)
    await github_approval_service.decide_continuation(db, a.item,
        authenticated_leader_member_id=a.leader.id, decision="rejected" if state == "rejected" else "approved",
        reason="Decision", request_id=approval.id)
    if state == "active":
        await github_approval_service.ensure_continuation_decision_message(db, a.item, approval, revision)
        await github_approval_service.deliver_approved_continuation(db, a.item, approval, revision)
        await github_dispatch_service.activate_continuation_revision(db, a.item, a.scope, revision,
            authenticated_owner_member_id=a.owner.id, authenticated_owner_slot_id=a.owner.team_slot_id,
            dispatch_nonce=a.item.dispatch_nonce, lease_token=a.workspace.lease_token)
    replay = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    assert replay[0].id == revision.id and not replay[2] and a.item.approval_round_count == 2


@pytest.mark.asyncio
async def test_operator_cancellation_of_amendment_leaves_supported_recovery_reason(db, submitted_attempt):
    a = submitted_attempt
    await github_approval_service.cancel_active_continuation(db, a.item, revision_number=a.revision.revision,
        dispatch_nonce=a.item.dispatch_nonce, reason="Operator stops submitted amendment")
    assert a.item.escalation_reason == "plan_blocked"


@pytest.mark.asyncio
async def test_already_charged_diagnostic_head_still_requires_restored_tree(db, healthy_attempt, monkeypatch):
    a = healthy_attempt
    revision = await submitted(a, db, diagnostic=True)
    revision.failed_head_count = 1
    revision.last_failed_head_sha = a.head
    a.item.diagnostic_retry_count = 1
    a.item.diagnostic_last_verified_sha = a.head
    await db.commit()
    monkeypatch.setattr(github_client, "get_commit_snapshot", AsyncMock(return_value=
        GithubCommitSnapshot(sha=a.head, tree_sha="b" * 40)))
    with pytest.raises(GithubApprovalError, match="diagnostic_head_unrestored"):
        await github_approval_service.cancel_active_continuation(db, a.item, revision_number=1,
            dispatch_nonce=a.item.dispatch_nonce, reason="Budget is not restoration")
    assert revision.status == "submitted"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["leader", "owner", "lease", "scope", "team", "pause", "hold", "approval"])
async def test_review_record_rechecks_authority_after_github_await(db, submitted_attempt, monkeypatch, change):
    from app.models.database import GithubOwnerObservationPause
    from app.services import github_coordination_service
    a = submitted_attempt
    state = {"hold": None}
    monkeypatch.setattr(github_coordination_service, "hold_code", lambda: state["hold"])
    async def mutate(*args, **kwargs):
        if change == "leader":
            a.preset.leader_slot_id = a.reviewer.team_slot_id
        elif change == "owner":
            a.item.owner_slot_id = a.reviewer.team_slot_id
        elif change == "lease":
            a.workspace.lease_token = "rotated"
        elif change == "scope":
            a.scope.enabled = False
        elif change == "team":
            a.preset.autonomy_enabled = False
        elif change == "pause":
            db.add(GithubOwnerObservationPause(work_item_id=a.item.id, wait_key="synthetic-wait",
                authority_sha256="synthetic-authority", deadline=datetime.utcnow(), status="paused"))
        elif change == "approval":
            stored = await db.get(GithubApprovalRequest, a.revision.approval_request_id)
            stored.status = "superseded"
        else:
            state["hold"] = "factory_hold"
        await db.commit()
        return a.pull
    monkeypatch.setattr(github_client, "get_pull", mutate)
    with pytest.raises(GithubApprovalError):
        await record_changes(db, a)
    await db.refresh(a.revision)
    assert a.revision.status == "submitted"


@pytest.mark.asyncio
async def test_review_http_routes_derive_identity_and_hide_private_binding(db, submitted_attempt):
    from app.api.v1 import agent_teams
    from app.models.schemas import GithubRevisionReviewCreate, GithubSubmittedRevisionEnd
    from app.models.database import MailAgentSession
    a = submitted_attempt
    reviewer_session = MailAgentSession(member_id=a.reviewer.id, provider="codex-cli", source="mcp",
        session_key="synthetic-reviewer", cwd=str(a.repo), team_preset_id=a.preset.id,
        team_slot_id=a.reviewer.team_slot_id, mailbox_status="connected",
        capability_token_hash="synthetic-reviewer-token", bound_pane_pid=1003, bound_pane_proc_start="start-3")
    db.add(reviewer_session)
    await db.commit()
    response = await agent_teams.record_github_revision_review(a.item.id, GithubRevisionReviewCreate(
        dispatch_nonce=a.item.dispatch_nonce, revision=a.revision.revision, head_sha=a.head,
        summary="Changes required", evidence_url="https://github.com/o/approval/pull/5#pullrequestreview-123"),
        reviewer_session, db)
    assert response.reviewer_member_id == a.reviewer.id
    assert not {"expected_lease_token_hash", "dispatch_nonce", "watch_id"} & response.model_dump().keys()
    leader_session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == a.leader.id))
    ended = await agent_teams.end_github_submitted_revision(a.item.id, GithubSubmittedRevisionEnd(
        review_id=response.id, dispatch_nonce=a.item.dispatch_nonce, reason="Settle negative review"), leader_session, db)
    assert ended.status == "superseded" and ended.origin_kind == "scope_amendment"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["owner", "slot", "nonce", "lease", "phase", "disabled", "team", "hold"])
async def test_amendment_entry_point_retains_authority_guards(db, healthy_attempt, monkeypatch, invalid):
    from app.services import github_coordination_service
    a = healthy_attempt
    args = amendment(a)
    if invalid == "owner": args['authenticated_owner_member_id'] = a.leader.id
    elif invalid == "slot": args['authenticated_owner_slot_id'] = a.leader.team_slot_id
    elif invalid == "nonce": args['dispatch_nonce'] = "stale"
    elif invalid == "lease": args['lease_token'] = "stale"
    elif invalid == "phase": args['phase'] = "diagnostic"
    elif invalid == "disabled": a.scope.enabled = False
    elif invalid == "team": a.preset.autonomy_enabled = False
    else: monkeypatch.setattr(github_coordination_service, "hold_code", lambda: "factory_hold")
    await db.commit()
    with pytest.raises(GithubApprovalError):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **args)
    assert a.item.approval_round_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["lease", "scope", "round", "head", "cap"])
async def test_scope_amendment_fences_changes_during_remote_read(db, healthy_attempt, monkeypatch, change):
    a = healthy_attempt
    a.item.pr_number = 5
    await db.commit()
    async def mutate(*args, **kwargs):
        if change == "lease": a.workspace.lease_token = "replacement"
        elif change == "scope": a.scope.enabled = False
        elif change == "round": a.item.approval_round_count += 1
        elif change == "cap":
            await db.execute(update(TeamGithubScope).where(TeamGithubScope.id == a.scope.id)
                .values(max_scope_amendments=0).execution_options(synchronize_session=False))
        else:
            (a.repo / "src/base.py").write_text("BASE = 'raced'\n")
            subprocess.run(["git", "-C", str(a.repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(a.repo), "-c", "user.name=Fixture", "-c",
                "user.email=fixture@example.invalid", "commit", "-qm", "raced"], check=True)
        await db.commit()
        return a.pull
    monkeypatch.setattr(github_client, "get_pull", mutate)
    item_id = a.item.id
    with pytest.raises(GithubApprovalError, match="amendment_count_limit" if change == "cap" else None):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    assert not await db.scalar(select(GithubApprovalRequest.id).where(
        GithubApprovalRequest.work_item_id == item_id, GithubApprovalRequest.request_kind == 'continuation'))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tree", "lease", "owner", "hold", "scope"])
async def test_submitted_cancellation_rejects_late_authority_or_tree_change(db, healthy_attempt, monkeypatch, change):
    from app.services import github_coordination_service
    a = healthy_attempt
    revision = await submitted(a, db, diagnostic=True)
    state = {"hold": None}
    monkeypatch.setattr(github_coordination_service, 'hold_code', lambda: state['hold'])
    calls = 0
    async def mutate(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            if change == 'tree': revision.baseline_tree_sha = 'b' * 40
            elif change == 'lease': a.workspace.lease_token = 'replaced'
            elif change == 'owner': a.item.owner_slot_id = a.leader.team_slot_id
            elif change == 'scope': a.scope.enabled = False
            else: state['hold'] = 'factory_hold'
            await db.commit()
        return a.pull
    monkeypatch.setattr(github_client, 'get_pull', mutate)
    # A tree changed during the read must not become a new restoration authority.
    monkeypatch.setattr(github_client, 'get_commit_snapshot', AsyncMock(return_value=
        GithubCommitSnapshot(sha=a.head, tree_sha='b' * 40 if change == 'tree' else a.tree)))
    with pytest.raises(GithubApprovalError):
        await github_approval_service.cancel_active_continuation(db, a.item,
            revision_number=1, dispatch_nonce=a.item.dispatch_nonce, reason='Late state must refuse')
    await db.refresh(revision)
    assert revision.status == 'submitted'


@pytest.mark.asyncio
async def test_submitted_verification_replaces_stale_cached_head_with_observed_current_head(db):
    scope = await _scope(db)
    slot, member = await _owner(db, scope)
    item = await _item(db, scope, dispatch_status="verifying", pr_number=5, owner_slot_id=slot.id,
        dispatch_nonce="stale-observation", active_scope_revision=1, verification_head_sha="old-head")
    revision, _ = await _implementation_revision(db, scope, item, slot, member)
    client = _Client(check_runs=[{"name": "backend", "status": "completed", "conclusion": "cancelled"}])
    await github_verification_service.process_scope(db, scope, client=client)
    await db.refresh(item)
    assert item.verification_head_sha == revision.submitted_head_sha == "sha"
    assert item.verification_started_at and item.retry_count == 0


@pytest.mark.asyncio
async def test_amendment_http_entry_and_decided_replay_preserve_server_origin(db, healthy_attempt):
    from app.api.v1 import agent_teams
    from app.models.schemas import GithubScopeAmendmentCreate
    a = healthy_attempt
    args = amendment(a)
    args.pop('authenticated_owner_member_id')
    args.pop('authenticated_owner_slot_id')
    request = GithubScopeAmendmentCreate(**args)
    session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == a.owner.id))
    first = await agent_teams.request_github_scope_amendment(a.item.id, request, session, db)
    assert first.revision.origin_kind == 'scope_amendment'
    await github_approval_service.decide_continuation(db, a.item,
        authenticated_leader_member_id=a.leader.id, decision='rejected', reason='Keep a bounded record',
        request_id=first.approval.id)
    replay = await agent_teams.request_github_scope_amendment(a.item.id, request, session, db)
    assert replay.revision.id == first.revision.id and replay.approval.status == 'rejected'
    assert a.item.approval_round_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', ['paths', 'commands', 'revisions', 'failures'])
async def test_amendments_retain_all_stricter_shared_budgets(db, healthy_attempt, limit):
    a = healthy_attempt
    args = amendment(a)
    if limit == 'paths':
        a.scope.max_scope_paths = 1
    elif limit == 'commands':
        a.scope.max_scope_commands = 1
        args['allowed_commands'] = ['pytest -q', 'npm test']
    else:
        past = await prior_amendment(a, db, 1, ['src/past.py'])
        if limit == 'revisions': a.scope.max_continuation_revisions = 1
        else:
            past.failed_head_count = 1
            a.scope.max_continuation_failed_heads = 1
    await db.commit()
    with pytest.raises(GithubApprovalError):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **args)
    assert a.item.approval_round_count == 1


@pytest.mark.asyncio
async def test_review_audit_retains_safe_disposition_identity(db, submitted_attempt):
    from app.models.database import FactoryAuditEvent
    a = submitted_attempt
    fact = await record_changes(db, a)
    event = await db.scalar(select(FactoryAuditEvent).where(FactoryAuditEvent.event_kind == 'revision_review'))
    assert event.after_values['review_id'] == fact.id
    assert event.after_values['reviewer_member_id'] == a.reviewer.id
    assert event.after_values['head_sha'] == a.head
    assert event.after_values['verdict'] == 'changes_required'
    assert 'expected_lease_token_hash' not in event.after_values


@pytest.mark.asyncio
@pytest.mark.parametrize('conclusion', ['cancelled', 'timed_out'])
async def test_diagnostic_inconclusive_runs_are_visible_without_failure_charge(db, conclusion):
    from tests.agent_teams.test_github_verification_service import _diagnostic_revision
    scope = await _scope(db)
    slot, member = await _owner(db, scope)
    item = await _item(db, scope, dispatch_status='dispatched', pr_number=5, owner_slot_id=slot.id,
        dispatch_nonce='inconclusive-diagnostic', active_scope_revision=1, attempt_phase='diagnostic')
    revision, _ = await _diagnostic_revision(db, scope, item, slot, member)
    client = _Client(check_runs=[{'name': 'diagnostic', 'status': 'completed', 'conclusion': conclusion}])
    for _ in range(2):
        await github_verification_service.process_scope(db, scope, client=client)
    await db.refresh(revision)
    assert revision.failed_head_count == item.diagnostic_retry_count == item.retry_count == 0
    assert revision.status == 'active' and conclusion in item.status_note
    assert revision.evidence['diagnostic_observations']['sha']['state'] == 'pending'
    assert client.merge_calls == client.ready_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['pause', 'hold', 'reopened', 'head', 'lease'])
async def test_escalated_closed_pr_settlement_rechecks_after_second_read(db, monkeypatch, change):
    from app.models.database import GithubOwnerObservationPause
    from app.services import github_coordination_service
    scope = await _scope(db)
    (await db.get(AgentTeamPreset, scope.preset_id)).autonomy_enabled = True
    scope.enabled = True
    slot, member = await _owner(db, scope)
    item = await _item(db, scope, dispatch_status='escalated', escalation_reason='plan_blocked',
        pr_number=5, owner_slot_id=slot.id, dispatch_nonce='closed-race', active_scope_revision=1)
    revision, workspace = await _implementation_revision(db, scope, item, slot, member)
    revision.submitted_head_sha = 'a' * 40
    revision.expected_lease_token_hash = github_approval_service.lease_token_hash(workspace.lease_token)
    await db.commit()
    client = _Client(pull={'number': 5, 'state': 'closed', 'merged': False, 'merged_at': None,
        'head': {'sha': 'a' * 40}})
    state = {'hold': None}
    monkeypatch.setattr(github_coordination_service, 'hold_code', lambda: state['hold'])
    original = client.get_pull
    async def mutate(*args, **kwargs):
        if client.pull_calls == 1:
            if change == 'pause':
                db.add(GithubOwnerObservationPause(work_item_id=item.id, wait_key='closed-wait',
                    authority_sha256='synthetic-authority', deadline=datetime.utcnow(), status='paused'))
            elif change == 'hold': state['hold'] = 'factory_hold'
            elif change == 'reopened': client.pull['state'] = 'open'
            elif change == 'head': client.pull['head']['sha'] = 'b' * 40
            else: workspace.lease_token = 'rotated'
            await db.commit()
        return await original(*args, **kwargs)
    client.get_pull = mutate
    await github_verification_service.process_scope(db, scope, client=client)
    await db.refresh(revision)
    await db.refresh(item)
    assert revision.status == 'submitted'
    assert item.dispatch_status == 'escalated' and item.retry_count == 0
    assert client.merge_calls == client.ready_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('db', ['concurrent'], indirect=True)
@pytest.mark.parametrize('operation', ['request', 'activate'])
async def test_concurrent_amendment_writers_share_the_last_budget_unit(db, healthy_attempt, monkeypatch, operation):
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models.database import GithubAttemptScopeRevision, GithubWorkItem
    from app.services.github_workspace_service import github_workspace_service
    a = healthy_attempt
    await prior_amendment(a, db, 1, ['src/past1.py'])
    await prior_amendment(a, db, 2, ['src/past2.py'])
    revision_id = None
    if operation == 'activate':
        revision, approval, _ = await github_approval_service.create_scope_amendment_request(
            db, a.item, a.scope, **amendment(a))
        revision_id = revision.id
        await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)
        await github_approval_service.decide_continuation(db, a.item,
            authenticated_leader_member_id=a.leader.id, decision='approved', reason='Last bounded unit',
            request_id=approval.id)
        await github_approval_service.ensure_continuation_decision_message(db, a.item, approval, revision)
        await github_approval_service.deliver_approved_continuation(db, a.item, approval, revision)
    item_id, scope_id = a.item.id, a.scope.id
    args = amendment(a)
    await db.commit()
    ready, arrived = asyncio.Event(), 0
    original = github_workspace_service.attest_head
    async def barrier(*args, **kwargs):
        nonlocal arrived
        result = await original(*args, **kwargs)
        arrived += 1
        if arrived == 2: ready.set()
        await asyncio.wait_for(ready.wait(), timeout=10)
        return result
    monkeypatch.setattr(github_workspace_service, 'attest_head', barrier)
    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    async def run_one():
        async with maker() as isolated:
            item = await isolated.get(GithubWorkItem, item_id)
            scope = await isolated.get(TeamGithubScope, scope_id)
            try:
                if operation == 'request':
                    return await github_approval_service.create_scope_amendment_request(isolated, item, scope, **args)
                row = await isolated.get(GithubAttemptScopeRevision, revision_id)
                return await github_dispatch_service.activate_continuation_revision(isolated, item, scope, row,
                    authenticated_owner_member_id=args['authenticated_owner_member_id'],
                    authenticated_owner_slot_id=args['authenticated_owner_slot_id'],
                    dispatch_nonce=args['dispatch_nonce'], lease_token=args['lease_token'])
            except ValueError as exc:
                await isolated.rollback()
                return exc
    results = await asyncio.gather(run_one(), run_one())
    assert sum(not isinstance(row, Exception) for row in results) == 1
    rows = list((await db.scalars(select(GithubAttemptScopeRevision).where(
        GithubAttemptScopeRevision.work_item_id == item_id).execution_options(populate_existing=True))).all())
    assert len(rows) == 3 and sum(row.failed_head_count for row in rows) == 0
    await db.refresh(a.item)
    assert a.item.approval_round_count == 2
    if operation == 'activate':
        assert sum(row.status == 'active' for row in rows) == 1
        assert a.item.active_scope_revision == 3 and a.item.ack_received_at


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['session', 'pane', 'launch'])
async def test_amendment_http_rechecks_authenticated_session_after_await(db, healthy_attempt, monkeypatch, change):
    from app.api.v1 import agent_teams
    from app.models.database import AgentTeamSlot
    from app.models.schemas import GithubScopeAmendmentCreate
    from fastapi import HTTPException
    a = healthy_attempt
    a.item.pr_number = 5
    await db.commit()
    args = amendment(a)
    args.pop('authenticated_owner_member_id')
    args.pop('authenticated_owner_slot_id')
    request = GithubScopeAmendmentCreate(**args)
    session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == a.owner.id))
    async def mutate(*args, **kwargs):
        if change == 'session': session.closed_at = datetime.utcnow()
        elif change == 'pane': monkeypatch.setattr(peer_process, 'pane_is_alive', lambda *_: False)
        else:
            slot = await db.get(AgentTeamSlot, a.owner.team_slot_id)
            slot.launch_options = {'changed': True}
        await db.commit()
        return a.pull
    monkeypatch.setattr(github_client, 'get_pull', mutate)
    item_id = a.item.id
    with pytest.raises(HTTPException):
        await agent_teams.request_github_scope_amendment(item_id, request, session, db)
    assert not await db.scalar(select(GithubApprovalRequest.id).where(
        GithubApprovalRequest.work_item_id == item_id, GithubApprovalRequest.request_kind == 'continuation'))


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', [None, 'approved', 'rejected'])
async def test_verification_does_not_advance_a_stopped_amendment_round(db, healthy_attempt, decision):
    a = healthy_attempt
    a.item.pr_number = 5
    a.item.dispatch_base_ref = 'release/v4.1'
    a.item.dispatch_status = 'verifying'
    await db.commit()
    revision, request, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    if decision:
        await github_approval_service.ensure_continuation_request_message(db, a.item, request, revision)
        await github_approval_service.decide_continuation(db, a.item,
            authenticated_leader_member_id=a.leader.id, decision=decision, reason='Stopped round',
            request_id=request.id)
    client = _Client(pull=a.pull, check_runs=[{'name': 'backend', 'status': 'completed', 'conclusion': 'success'}])
    await github_verification_service.process_scope(db, a.scope, client=client)
    await db.refresh(a.item)
    assert a.item.dispatch_status == 'verifying' and a.item.ack_received_at is None
    assert a.item.active_scope_revision == 0 and a.item.retry_count == 0
    assert client.pull_calls == client.ready_calls == client.merge_calls == 0


@pytest.mark.asyncio
async def test_amendment_request_rolls_back_if_hold_arrives_during_audit(db, healthy_attempt, monkeypatch):
    from app.services import github_approval_service as module, github_coordination_service
    a = healthy_attempt
    state = {'hold': None}
    monkeypatch.setattr(github_coordination_service, 'hold_code', lambda: state['hold'])
    original = module._record_decision_fact
    async def held(*args, **kwargs):
        await original(*args, **kwargs)
        state['hold'] = 'factory_hold'
    monkeypatch.setattr(module, '_record_decision_fact', held)
    item_id = a.item.id
    with pytest.raises(GithubApprovalError):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    await db.refresh(a.item)
    assert a.item.approval_round_count == 1
    assert not await db.scalar(select(GithubApprovalRequest.id).where(
        GithubApprovalRequest.work_item_id == item_id, GithubApprovalRequest.request_kind == 'continuation'))


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['decision', 'activation'])
async def test_amendment_expiry_is_rechecked_after_remote_await(db, healthy_attempt, monkeypatch, phase):
    from datetime import timedelta
    from app.services import github_approval_service as approvals, github_dispatch_service as dispatch
    a = healthy_attempt
    a.item.pr_number = 5
    await db.commit()
    revision, request, _ = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    await github_approval_service.ensure_continuation_request_message(db, a.item, request, revision)
    if phase == 'activation':
        await github_approval_service.decide_continuation(db, a.item,
            authenticated_leader_member_id=a.leader.id, decision='approved', reason='Bounded lifetime',
            request_id=request.id)
        await github_approval_service.ensure_continuation_decision_message(db, a.item, request, revision)
        await github_approval_service.deliver_approved_continuation(db, a.item, request, revision)
    now = datetime.utcnow()
    revision.expires_at = now + timedelta(seconds=1)
    await db.commit()
    class Clock:
        @staticmethod
        def utcnow(): return now
    monkeypatch.setattr(approvals, 'datetime', Clock)
    monkeypatch.setattr(dispatch, 'datetime', Clock)
    async def expire(*args, **kwargs):
        nonlocal now
        now += timedelta(seconds=2)
        return a.pull
    monkeypatch.setattr(github_client, 'get_pull', expire)
    with pytest.raises(ValueError):
        if phase == 'decision':
            await github_approval_service.decide_continuation(db, a.item,
                authenticated_leader_member_id=a.leader.id, decision='approved', reason='Expired during read',
                request_id=request.id)
        else:
            await github_dispatch_service.activate_continuation_revision(db, a.item, a.scope, revision,
                authenticated_owner_member_id=a.owner.id, authenticated_owner_slot_id=a.owner.team_slot_id,
                dispatch_nonce=a.item.dispatch_nonce, lease_token=a.workspace.lease_token)
    await db.refresh(revision)
    assert revision.status == ('proposed' if phase == 'decision' else 'approved')
    await db.refresh(a.item)
    assert a.item.active_scope_revision == 0


async def acknowledge_initial_root(db, a):
    """Drive the normal lifecycle: the Leader answers the plan request, then the owner acknowledges it."""
    root = await db.get(MailMessage, a.approval.request_message_id)
    answer = await agent_mail_service.send_message(db, MailMessageCreate(
        kind="answer", sender_member_id=a.leader.id, thread_root_id=root.id, body_markdown="Plan approved."))
    await db.refresh(root)
    assert root.request_status == "answered"
    await agent_mail_service.ack_message(db, answer.id, a.owner.id)
    await db.refresh(root)
    assert root.request_status == "acknowledged"
    return root


async def open_pull(db, a, base):
    a.item.pr_number = 5
    a.item.dispatch_base_ref = base
    a.item.dispatch_status = "verifying"
    await db.commit()


@pytest.mark.asyncio
async def test_acknowledged_initial_plan_is_a_valid_amendment_baseline(db, healthy_attempt):
    a = healthy_attempt
    await acknowledge_initial_root(db, a)
    revision, request, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    assert created and request.status == "pending" and revision.status == "proposed"
    assert revision.allowed_paths == ["src/added.py", "src/base.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["superseded", "other_sender", "other_recipient", "other_nonce", "other_round"])
async def test_acknowledged_root_still_needs_every_existing_baseline_check(db, healthy_attempt, change):
    a = healthy_attempt
    root = await acknowledge_initial_root(db, a)
    if change == "superseded":
        root.request_status = "superseded"
    elif change == "other_sender":
        root.sender_member_id = a.leader.id
    elif change == "other_recipient":
        root.recipient_member_id = a.owner.id
    elif change == "other_nonce":
        root.payload = {**root.payload, "dispatch_nonce": "0" * 16}
    else:
        root.payload = {**root.payload, "approval_round": 9}
    await db.commit()
    with pytest.raises(GithubApprovalError, match="amendment_baseline_unverifiable"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))


@pytest.mark.asyncio
async def test_pending_request_still_accepts_only_a_pending_root(db):
    from app.models.database import GithubApprovalRequest as Request
    request = Request(status="pending", owner_member_id=2, leader_member_id=1, work_item_id=3,
        dispatch_nonce="n", approval_round=1,
        request_fingerprint=github_approval_service.initial_request_fingerprint(summary="", plan_metadata={}))
    def root(status):
        return MailMessage(kind="context_request", thread_root_id=None, request_status=status,
            sender_member_id=2, recipient_member_id=1, delivery_key=None,
            payload={"work_item_id": 3, "dispatch_nonce": "n", "approval_round": 1}, body_markdown="")
    assert github_approval_service.matches_linked_request_message(request, root("pending"), delivery_key="k")
    for status in ("answered", "acknowledged", "superseded"):
        assert not github_approval_service.matches_linked_request_message(request, root(status), delivery_key="k")
    request.status = "approved"
    for status in ("pending", "answered", "acknowledged"):
        assert github_approval_service.matches_linked_request_message(request, root(status), delivery_key="k")
    assert not github_approval_service.matches_linked_request_message(request, root("superseded"), delivery_key="k")


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["origin/release/v4.1", "release/v4.1"])
async def test_amendment_on_an_open_pr_accepts_the_stored_base_with_or_without_the_remote_prefix(
        db, healthy_attempt, base):
    a = healthy_attempt
    await open_pull(db, a, base)
    _revision, request, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    assert created and request.status == "pending"


@pytest.mark.asyncio
async def test_amendment_keeps_an_empty_stored_base_unchecked(db, healthy_attempt):
    a = healthy_attempt
    await open_pull(db, a, None)
    a.pull["base"]["ref"] = "anything"
    _revision, request, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    assert created and request.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("base", ["origin/other", "other", "origin/origin/release/v4.1", "origin/"])
async def test_amendment_refuses_a_different_base_branch(db, healthy_attempt, base):
    a = healthy_attempt
    await open_pull(db, a, base)
    with pytest.raises(GithubApprovalError, match="amendment_head_changed"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["head", "number", "closed", "merged"])
async def test_prefixed_base_does_not_relax_the_other_pull_checks(db, healthy_attempt, change):
    a = healthy_attempt
    await open_pull(db, a, "origin/release/v4.1")
    if change == "head":
        a.pull["head"]["sha"] = "b" * 40
    elif change == "number":
        a.pull["number"] = 6
    elif change == "closed":
        a.pull["state"] = "closed"
    else:
        a.pull["merged_at"] = "2026-11-13T00:00:00Z"
    with pytest.raises(GithubApprovalError, match="amendment_head_changed"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))


@pytest.mark.asyncio
async def test_prefixed_base_does_not_relax_the_owner_check(db, healthy_attempt):
    a = healthy_attempt
    await open_pull(db, a, "origin/release/v4.1")
    wrong = amendment(a) | {"authenticated_owner_member_id": a.leader.id}
    with pytest.raises(GithubApprovalError, match="not_item_owner"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **wrong)


@pytest.mark.asyncio
@pytest.mark.parametrize("github_ref, stored, expected", [
    ("release/v4.1", "origin/release/v4.1", True),
    ("release/v4.1", "release/v4.1", True),
    ("origin/x", "origin/x", True),
    ("origin/x", "origin/origin/x", True),
    ("x", "origin/origin/x", False),
    ("release/v4.1", "origin/release/v4.2", False),
    ("release/v4.1", "origin/", False),
    ("release/v4.1", "xorigin/release/v4.1", False),
    ("origin/release/v4.1", "release/v4.1", False),
    ("", "origin/release/v4.1", False),
    ("anything", None, True),
    ("anything", "", True),
])
async def test_base_ref_matches_is_exact_with_at_most_one_origin_prefix(github_ref, stored, expected):
    assert github_approval_service.base_ref_matches(github_ref, stored) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("base, error", [
    ("origin/release/v4.1", None),
    ("release/v4.1", None),
    ("origin/other", "revision_review_head_changed"),
    ("origin/origin/release/v4.1", "revision_review_head_changed"),
])
async def test_review_head_check_uses_the_same_base_comparison(db, submitted_attempt, base, error):
    a = submitted_attempt
    a.item.dispatch_base_ref = base
    await db.commit()
    if error:
        with pytest.raises(GithubApprovalError, match=error):
            await record_changes(db, a)
    else:
        review = await record_changes(db, a)
        assert review.id and a.revision.status == "submitted"


@pytest.mark.asyncio
async def test_healthy_amendment_accepts_a_settled_accepted_handoff(db, healthy_attempt):
    a = healthy_attempt
    a.item.handoff_state = "accepted"
    await db.commit()
    revision, request, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    assert created and request.status == "pending" and revision.status == "proposed"
    assert revision.owner_member_id == a.owner.id and revision.owner_slot_id == a.item.owner_slot_id
    assert a.item.handoff_state == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "declined", "cancelled", ""])
async def test_healthy_amendment_still_refuses_an_unsettled_or_unknown_handoff(db, healthy_attempt, state):
    a = healthy_attempt
    a.item.handoff_state = state
    if state == "pending":
        a.item.handoff_target_slot_id = a.item.owner_slot_id
    await db.commit()
    with pytest.raises(GithubApprovalError, match="amendment_context_unavailable"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    await db.refresh(a.item)
    assert a.item.active_scope_revision == 0 and a.item.dispatch_status == "dispatched"


@pytest.mark.asyncio
async def test_amendment_after_a_handoff_to_a_new_owner_needs_the_new_owners_fresh_approval(
    db, healthy_attempt
):
    from app.models.database import AgentTeamSlot, MailTeamMember
    a = healthy_attempt
    old_owner_slot_id, old_owner_id, old_round = a.item.owner_slot_id, a.owner.id, a.item.approval_round_count
    new_slot = AgentTeamSlot(preset_id=a.preset.id, position=5, display_name="New owner", provider="codex-cli",
        repo_id="approval", repo_path="/tmp/approval", repo_name="approval", launch_mode="plain",
        launch_options={}, enabled=True)
    db.add(new_slot)
    await db.flush()
    new_owner = MailTeamMember(identity_key=f"slot:approval:{new_slot.id}", repo_id="approval",
        repo_path="/tmp/approval", repo_name="approval", display_name="New owner",
        participant_kind="team_slot", team_preset_id=a.preset.id, team_slot_id=new_slot.id)
    db.add(new_owner)
    await db.flush()
    new_session = MailAgentSession(member_id=new_owner.id, provider="codex-cli", source="mcp",
        session_key="mcp:approval:new-owner", cwd="/tmp/approval", team_preset_id=a.preset.id,
        team_slot_id=new_slot.id, mailbox_status="connected", last_seen_at=datetime.utcnow(),
        capability_token_hash=agent_mail_service.hash_capability_token("token-new-owner"),
        bound_pane_pid=1999, bound_pane_proc_start="start-new-owner")
    db.add(new_session)
    # The state accept_handoff leaves behind: new owner, accepted handoff, cleared acknowledgement.
    a.item.owner_slot_id = new_slot.id
    a.item.handoff_state, a.item.handoff_target_slot_id, a.item.routing_method = "accepted", None, "reassigned"
    a.item.ack_received_at = a.item.ack_approver_member_id = a.item.ack_evidence_message_id = None
    a.item.ack_enforcement_epoch = a.item.ack_approval_round = None
    await db.commit()

    def as_new_owner(**overrides):
        return amendment(a) | dict(authenticated_owner_member_id=new_owner.id,
                                   authenticated_owner_slot_id=new_slot.id) | overrides

    # Only the former owner's approved plan exists, so the new owner cannot amend yet.
    with pytest.raises(GithubApprovalError, match="amendment_baseline_unverifiable"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **as_new_owner())
    await db.refresh(a.item)
    assert a.item.owner_slot_id == new_slot.id and a.item.active_scope_revision == 0
    assert a.item.approval_round_count == old_round

    # The new owner gets a fresh approval and acknowledgement from the Leader.
    response = await agent_mail.request_work_item_approval(MailApprovalRequestCreate(
        work_item_id=a.item.id, dispatch_nonce=a.item.dispatch_nonce, summary="New owner closed scope",
        plan_metadata={"allowed_paths": ["src/base.py"]}), new_session, db)
    fresh = await db.get(GithubApprovalRequest, response.id)
    assert fresh.owner_member_id == new_owner.id and fresh.owner_member_id != old_owner_id
    await github_approval_service.decide(db, a.item, authenticated_leader_member_id=a.leader.id,
        decision="approved", reason="Fresh decision for the new owner", request_id=fresh.id)
    await db.refresh(a.item)
    a.item.ack_received_at, a.item.ack_approver_member_id = datetime.utcnow(), a.leader.id
    a.item.ack_enforcement_epoch = a.item.ack_approval_round = fresh.approval_round
    await db.commit()

    revision, request, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **as_new_owner())
    assert created and request.status == "pending" and revision.status == "proposed"
    assert revision.owner_member_id == new_owner.id and revision.owner_slot_id == new_slot.id
    assert request.owner_member_id == new_owner.id and request.approval_round == fresh.approval_round + 1
    assert a.item.approval_round_count == request.approval_round > old_round
    assert request.owner_member_id != old_owner_id and revision.owner_slot_id != old_owner_slot_id


REQUIRED_JOBS = ["Full backend regression tests", "e2e"]


def green_runs(head, names=REQUIRED_JOBS, **overrides):
    return [{"name": name, "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"},
             "head_sha": head, **overrides} for name in names]


async def require_jobs(db, a, monkeypatch, runs=None):
    """Give the attempt a delivery policy with required jobs and serve current check runs."""
    from app.services.factory_delivery_policy import effective_policy
    from app.services.github_ci_watch_service import _digest
    a.item.delivery_policy = {"required_checks": [{"name": name, "app_slug": "github-actions"} for name in REQUIRED_JOBS]}
    await db.commit()
    a.watch.policy_digest = _digest(effective_policy(a.item, a.scope))
    await db.commit()
    served = AsyncMock(return_value=green_runs(a.head) if runs is None else runs)
    monkeypatch.setattr(github_client, "list_check_runs_for_ref", served)
    return served


async def record_accepted(db, a, **overrides):
    from app.services.github_terminal_revision_service import record_revision_review
    args = dict(revision_number=a.revision.revision, dispatch_nonce=a.item.dispatch_nonce, head_sha=a.head,
        reviewer_member_id=a.reviewer.id, verdict="accepted", summary="Independent review accepts the exact head",
        evidence_url="https://github.com/o/approval/pull/5#pullrequestreview-456")
    args.update(overrides)
    return await record_revision_review(db, a.item, **args)


async def settle_accepted(db, a, review, **overrides):
    from app.services.github_terminal_revision_service import settle_accepted_revision
    args = dict(review_id=review.id, leader_member_id=a.leader.id, dispatch_nonce=a.item.dispatch_nonce,
        reason="Accepted at the exact head")
    args.update(overrides)
    return await settle_accepted_revision(db, a.item, **args)


async def attempt_facts(db, a):
    for row in (a.item, a.revision, a.workspace):
        await db.refresh(row)
    return (a.item.dispatch_status, a.item.active_scope_revision, a.item.retry_count, a.item.diagnostic_retry_count,
            a.item.approval_round_count, a.item.last_verified_sha, a.revision.failed_head_count,
            a.workspace.leased_item_id, a.workspace.lease_token, a.workspace.leased_at)


@pytest.mark.asyncio
async def test_the_bound_independent_reviewer_records_an_accepted_fact(db, submitted_attempt):
    a = submitted_attempt
    before = await attempt_facts(db, a)
    fact = await record_accepted(db, a)
    assert fact.verdict == "accepted" and fact.status == "active" and fact.head_sha == a.head
    assert fact.reviewer_member_id == a.reviewer.id and fact.revision_id == a.revision.id
    assert await attempt_facts(db, a) == before and a.revision.status == "submitted"
    assert (await record_accepted(db, a)).id == fact.id


@pytest.mark.asyncio
@pytest.mark.parametrize("escalated", [False, True], ids=["verifying", "recoverable_escalation"])
async def test_review_records_accepted_at_a_submitted_continuation_head(db, submitted_continuation, escalated):
    from app.api.v1.agent_teams import _named_reviewer_binding

    a = submitted_continuation
    if escalated:
        await github_dispatch_service.escalate_without_notification(db, a.item, "plan_blocked")
    before = await attempt_facts(db, a)
    assert a.revision.acknowledged_at is not None and a.revision.status == "submitted"
    fact = await record_accepted(db, a)
    assert fact.verdict == "accepted" and fact.head_sha == a.head and fact.revision_id == a.revision.id
    assert fact.reviewer_member_id == a.reviewer.id and fact.status == "active"
    assert (await record_accepted(db, a)).id == fact.id
    assert await attempt_facts(db, a) == before
    if escalated:
        assert a.item.escalation_reason == "plan_blocked"
        with pytest.raises(GithubApprovalError):
            await settle_accepted(db, a, fact)
    binding, _ = await _named_reviewer_binding(db, a.item, a.scope, a.reviewer.id)
    assert binding.pr_number == a.item.pr_number and binding.active_scope_revision == a.revision.revision


@pytest.mark.asyncio
@pytest.mark.parametrize(("stop", "error"), [
    ("abandoned", "revision_review_context_unavailable"),
    ("paused", "revision_review_context_unavailable"),
    ("exhausted_revision", "submitted_revision_required"),
    ("unacknowledged", "submitted_revision_required"),
])
async def test_escalated_review_recording_requires_current_submitted_authority(db, submitted_continuation, stop, error):
    from app.models.database import GithubOwnerObservationPause, GithubRevisionReview

    a = submitted_continuation
    item_id = a.item.id
    reason = "abandoned_by_operator" if stop == "abandoned" else "plan_blocked"
    await github_dispatch_service.escalate_without_notification(db, a.item, reason)
    if stop == "paused":
        db.add(GithubOwnerObservationPause(work_item_id=item_id, wait_key="synthetic-wait",
            authority_sha256="synthetic-authority", deadline=datetime.utcnow(), status="paused"))
    elif stop == "exhausted_revision":
        a.revision.status = "exhausted"
    elif stop == "unacknowledged":
        a.revision.acknowledged_at = None
    await db.commit()

    with pytest.raises(GithubApprovalError, match=error):
        await record_accepted(db, a)
    assert await db.scalar(select(GithubRevisionReview.id).where(GithubRevisionReview.work_item_id == item_id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["approval", "acknowledgement", "owner", "lease"])
async def test_escalated_named_reviewer_binding_requires_submitted_authority(db, submitted_continuation, changed):
    from fastapi import HTTPException
    from app.api.v1.agent_teams import _named_reviewer_binding

    a = submitted_continuation
    await github_dispatch_service.escalate_without_notification(db, a.item, "plan_blocked")
    if changed == "approval":
        approval = await db.get(GithubApprovalRequest, a.revision.approval_request_id)
        approval.status = "superseded"
    elif changed == "acknowledgement":
        a.revision.acknowledged_at = None
    elif changed == "owner":
        a.revision.owner_member_id = a.leader.id
    else:
        a.workspace.lease_token = "replacement-lease"
    await db.commit()

    with pytest.raises(HTTPException) as error:
        await _named_reviewer_binding(db, a.item, a.scope, a.reviewer.id)
    assert (error.value.status_code, error.value.detail) == (409, "review_binding_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["attempt", "pause", "hold"])
async def test_named_reviewer_binding_rechecks_attempt_and_stops_before_return(
    db, submitted_continuation, monkeypatch, changed,
):
    from fastapi import HTTPException
    from app.api.v1.agent_teams import _named_reviewer_binding
    from app.models.database import GithubOwnerObservationPause
    from app.services import github_coordination_service
    from app.services.github_ci_watch_service import github_ci_watch_service

    a = submitted_continuation
    await github_dispatch_service.escalate_without_notification(db, a.item, "plan_blocked")
    require_reviewer = github_ci_watch_service._reviewer

    async def change_after_reviewer_check(*args):
        result = await require_reviewer(*args)
        if changed == "attempt":
            a.item.dispatch_nonce = "replacement"
        elif changed == "pause":
            db.add(GithubOwnerObservationPause(work_item_id=a.item.id, wait_key="synthetic-wait",
                authority_sha256="synthetic-authority", deadline=datetime.utcnow(), status="paused"))
        else:
            monkeypatch.setattr(github_coordination_service, "hold_code", lambda: "factory_hold")
        await db.commit()
        return result

    monkeypatch.setattr(github_ci_watch_service, "_reviewer", change_after_reviewer_check)
    with pytest.raises(HTTPException) as error:
        await _named_reviewer_binding(db, a.item, a.scope, a.reviewer.id)
    assert (error.value.status_code, error.value.detail) == (409, "review_binding_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["owner", "leader", "leader_named", "old_head", "bad_verdict", "bad_evidence",
                                    "empty_summary"])
async def test_an_accepted_fact_needs_the_same_binding_and_evidence_as_a_negative_one(db, submitted_attempt, invalid):
    a = submitted_attempt
    if invalid == "leader_named":
        a.watch.reviewer_member_id = a.leader.id
        await db.commit()
    kwargs = {"owner": {"reviewer_member_id": a.owner.id}, "leader": {"reviewer_member_id": a.leader.id},
        "leader_named": {"reviewer_member_id": a.leader.id},
        "old_head": {"head_sha": "a" * 40}, "bad_verdict": {"verdict": "approved"},
        "bad_evidence": {"evidence_url": "https://example.com/o/approval/pull/5#pullrequestreview-1"},
        "empty_summary": {"summary": "  "}}[invalid]
    with pytest.raises(GithubApprovalError):
        await record_accepted(db, a, **kwargs)
    assert a.revision.status == "submitted"


@pytest.mark.asyncio
async def test_the_leader_settles_an_accepted_fact_when_every_required_job_succeeded(db, submitted_attempt, monkeypatch):
    from app.models.database import FactoryAuditEvent, GithubRevisionReview
    a = submitted_attempt
    served = await require_jobs(db, a, monkeypatch)
    fact = await record_accepted(db, a)
    before = await attempt_facts(db, a)
    revision = await settle_accepted(db, a, fact)
    assert revision.id == a.revision.id and revision.status == "completed" and revision.completed_at is not None
    assert revision.cancelled_at is None and revision.submitted_head_sha == a.head
    await db.refresh(fact)
    assert fact.status == "consumed" and fact.settled_by_member_id == a.leader.id
    assert fact.settlement_reason == "Accepted at the exact head"
    assert await attempt_facts(db, a) == before, "settlement must not change the item, counters, rounds or lease"
    assert a.item.dispatch_status == "verifying"
    served.assert_awaited()
    assert served.await_args.args[2] == a.head
    audit = list((await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "revision_review", FactoryAuditEvent.revision_id == a.revision.id))).all())
    assert any(row.after_values.get("outcome") == "settled" and row.after_values.get("verdict") == "accepted"
               for row in audit)
    assert (await settle_accepted(db, a, fact)).id == revision.id
    with pytest.raises(GithubApprovalError):
        await settle_accepted(db, a, fact, reason="A different settlement is not a replay")
    assert (await db.get(GithubRevisionReview, fact.id)).settlement_reason == "Accepted at the exact head"


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", [
    "missing_job", "pending_job", "failed_job", "skipped_job", "wrong_app", "wrong_head", "duplicate_run", "no_required_jobs",
])
async def test_settlement_needs_every_required_job_successful_with_github_actions_provenance(
        db, submitted_attempt, monkeypatch, problem):
    a = submitted_attempt
    runs = green_runs(a.head)
    if problem == "missing_job":
        runs = runs[:1]
    elif problem == "pending_job":
        runs[1].update(status="in_progress", conclusion=None)
    elif problem == "failed_job":
        runs[1]["conclusion"] = "failure"
    elif problem == "skipped_job":
        runs[1]["conclusion"] = "skipped"
    elif problem == "wrong_app":
        runs[1]["app"] = {"slug": "some-other-app"}
    elif problem == "wrong_head":
        runs[1]["head_sha"] = "b" * 40
    elif problem == "duplicate_run":
        runs.append(dict(runs[1]))
    await require_jobs(db, a, monkeypatch, runs=runs)
    if problem == "no_required_jobs":
        a.item.delivery_policy = {"required_checks": []}
        from app.services.factory_delivery_policy import effective_policy
        from app.services.github_ci_watch_service import _digest
        await db.commit()
        a.watch.policy_digest = _digest(effective_policy(a.item, a.scope))
        await db.commit()
    fact = await record_accepted(db, a)
    before = await attempt_facts(db, a)
    with pytest.raises(GithubApprovalError, match="required_checks_not_successful"):
        await settle_accepted(db, a, fact)
    await db.refresh(fact)
    assert fact.status == "active" and a.revision.status == "submitted" and a.revision.completed_at is None
    assert await attempt_facts(db, a) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", [
    "owner_as_leader", "other_member", "pending_approval", "newer_negative", "withdrawn", "negative_fact_given",
    "pr_head_moved", "hold", "autonomy_off", "lease", "wrong_nonce", "revision_not_submitted", "reviewer_replaced",
])
async def test_settlement_refuses_stale_or_unbound_authority_before_any_mutation(
        db, submitted_attempt, monkeypatch, tmp_path, problem):
    from app.models.database import GithubApprovalRequest, MailTeamMember
    from app.services.github_terminal_revision_service import withdraw_revision_review
    a = submitted_attempt
    await require_jobs(db, a, monkeypatch)
    fact = await record_accepted(db, a)
    kwargs = {}
    if problem == "owner_as_leader":
        kwargs["leader_member_id"] = a.owner.id
    elif problem == "other_member":
        kwargs["leader_member_id"] = a.reviewer.id
    elif problem == "pending_approval":
        db.add(GithubApprovalRequest(work_item_id=a.item.id, request_kind="continuation",
            dispatch_nonce=a.item.dispatch_nonce, approval_round=9, owner_member_id=a.owner.id,
            leader_member_id=a.leader.id, request_fingerprint="f" * 64, status="pending"))
        await db.commit()
    elif problem == "newer_negative":
        await record_changes(db, a)
    elif problem == "withdrawn":
        await withdraw_revision_review(db, a.item, review_id=fact.id, reviewer_member_id=a.reviewer.id,
            dispatch_nonce=a.item.dispatch_nonce)
    elif problem == "negative_fact_given":
        fact = await record_changes(db, a)
    elif problem == "pr_head_moved":
        a.pull["head"] = {**a.pull["head"], "sha": "c" * 40}
    elif problem == "hold":
        marker = tmp_path / "hold.json"
        marker.write_text("{}")
        monkeypatch.setattr(settings, "github_coordination_hold_paths", [str(marker)])
        monkeypatch.setattr(settings, "github_recovery_only_attempt", "")
    elif problem == "autonomy_off":
        a.preset.autonomy_enabled = False
        await db.commit()
    elif problem == "lease":
        a.workspace.lease_token = "replacement-lease"
        await db.commit()
    elif problem == "wrong_nonce":
        kwargs["dispatch_nonce"] = "another-nonce"
    elif problem == "revision_not_submitted":
        a.revision.status = "active"
        await db.commit()
    else:
        newer = await db.get(MailTeamMember, a.reviewer.id)
        newer.updated_at = datetime.utcnow().replace(year=2099)
        db.add(MailTeamMember(identity_key=f"newer:{a.reviewer.id}", repo_id="approval", repo_path=str(a.repo),
            repo_name="approval", display_name="Replacement reviewer", participant_kind="team_slot",
            team_preset_id=a.preset.id, team_slot_id=a.reviewer.team_slot_id, updated_at=datetime.utcnow().replace(year=2100)))
        await db.commit()
    status_before = a.revision.status
    with pytest.raises(GithubApprovalError):
        await settle_accepted(db, a, fact, **kwargs)
    await db.refresh(a.revision)
    assert a.revision.status == status_before and a.revision.completed_at is None
    assert a.item.dispatch_status == "verifying" and a.item.active_scope_revision == a.revision.revision


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["check_read", "final_write"])
async def test_a_head_or_authority_change_during_settlement_refuses_before_the_revision_changes(
        db, submitted_attempt, monkeypatch, boundary):
    from app.services.github_terminal_revision_service import settle_accepted_revision  # noqa: F401
    a = submitted_attempt
    await require_jobs(db, a, monkeypatch)
    fact = await record_accepted(db, a)
    moved = []

    async def move_head():
        if not moved:
            moved.append(True)
            a.pull["head"] = {**a.pull["head"], "sha": "d" * 40}

    if boundary == "check_read":
        runs = green_runs(a.head)

        async def checks(*_args, **_kwargs):
            await move_head()
            return runs

        monkeypatch.setattr(github_client, "list_check_runs_for_ref", AsyncMock(side_effect=checks))
    else:
        from app.services import github_terminal_revision_service as terminal
        original = terminal._review_audit

        async def audited(*args, **kwargs):
            await move_head()
            # An uncommitted conflicting write: the final claim must see it and the rollback must undo the settlement.
            await db.execute(update(GithubWorkspace).where(GithubWorkspace.id == a.workspace.id)
                .values(lease_token="replacement-lease"))
            return await original(*args, **kwargs)

        monkeypatch.setattr(terminal, "_review_audit", audited)
    with pytest.raises(GithubApprovalError):
        await settle_accepted(db, a, fact)
    await db.refresh(a.revision)
    await db.refresh(fact)
    assert a.revision.status == "submitted" and a.revision.completed_at is None and fact.status == "active"


@pytest.mark.asyncio
async def test_accepted_review_http_routes_derive_identity_and_settle(db, submitted_attempt, monkeypatch):
    from fastapi import HTTPException
    from app.api.v1 import agent_teams
    from app.models.schemas import GithubRevisionReviewCreate, GithubSubmittedRevisionEnd
    from app.models.database import GithubAttemptScopeRevision, MailAgentSession
    a = submitted_attempt
    await require_jobs(db, a, monkeypatch)
    reviewer_session = MailAgentSession(member_id=a.reviewer.id, provider="codex-cli", source="mcp",
        session_key="synthetic-accepted-reviewer", cwd=str(a.repo), team_preset_id=a.preset.id,
        team_slot_id=a.reviewer.team_slot_id, mailbox_status="connected",
        capability_token_hash="synthetic-accepted-token", bound_pane_pid=1004, bound_pane_proc_start="start-4")
    db.add(reviewer_session)
    await db.commit()
    created = GithubRevisionReviewCreate(dispatch_nonce=a.item.dispatch_nonce, revision=a.revision.revision,
        head_sha=a.head, summary="Accepted at the exact head", verdict="accepted",
        evidence_url="https://github.com/o/approval/pull/5#pullrequestreview-789")
    response = await agent_teams.record_github_revision_review(a.item.id, created, reviewer_session, db)
    assert response.verdict == "accepted" and response.reviewer_member_id == a.reviewer.id
    assert GithubRevisionReviewCreate(dispatch_nonce="n", revision=1, head_sha="a" * 40, summary="s",
        evidence_url="https://github.com/o/approval/pull/5#pullrequestreview-1").verdict == "changes_required"
    body = GithubSubmittedRevisionEnd(review_id=response.id, dispatch_nonce=a.item.dispatch_nonce,
        reason="Settle accepted review")
    item_id, revision_id, leader_id = a.item.id, a.revision.id, a.leader.id
    with pytest.raises(HTTPException) as refused:
        await agent_teams.settle_github_accepted_revision(item_id, body, reviewer_session, db)
    assert refused.value.status_code in {403, 409}
    assert (await db.get(GithubAttemptScopeRevision, revision_id, populate_existing=True)).status == "submitted"
    leader_session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == leader_id))
    settled = await agent_teams.settle_github_accepted_revision(item_id, body, leader_session, db)
    assert settled.status == "completed" and settled.origin_kind == "scope_amendment"


async def require_jobs_from_apps(db, a, monkeypatch, apps):
    """A delivery policy whose required checks name the given apps, with matching successful exact-head runs."""
    from app.services.factory_delivery_policy import effective_policy
    from app.services.github_ci_watch_service import _digest
    a.item.delivery_policy = {"required_checks": [
        {"name": name, "app_slug": app} for name, app in zip(REQUIRED_JOBS, apps)]}
    await db.commit()
    a.watch.policy_digest = _digest(effective_policy(a.item, a.scope))
    await db.commit()
    runs = [{"name": name, "status": "completed", "conclusion": "success", "app": {"slug": app},
             "head_sha": a.head} for name, app in zip(REQUIRED_JOBS, apps)]
    served = AsyncMock(return_value=runs)
    monkeypatch.setattr(github_client, "list_check_runs_for_ref", served)
    return served


@pytest.mark.asyncio
@pytest.mark.parametrize("apps", [
    ("another-app", "another-app"), ("github-actions", "another-app"), ("another-app", "github-actions"),
    ("github-actions-lookalike", "github-actions"),
])
async def test_settlement_requires_github_actions_for_every_required_check_even_with_a_matching_policy_and_runs(
        db, submitted_attempt, monkeypatch, apps):
    a = submitted_attempt
    served = await require_jobs_from_apps(db, a, monkeypatch, apps)
    fact = await record_accepted(db, a)
    before = await attempt_facts(db, a)
    with pytest.raises(GithubApprovalError, match="required_checks_not_successful"):
        await settle_accepted(db, a, fact)
    await db.refresh(fact)
    assert fact.status == "active" and a.revision.status == "submitted" and a.revision.completed_at is None
    assert await attempt_facts(db, a) == before
    served.assert_not_awaited()


@pytest.mark.asyncio
async def test_settlement_still_completes_when_every_required_check_is_from_github_actions(
        db, submitted_attempt, monkeypatch):
    a = submitted_attempt
    served = await require_jobs_from_apps(db, a, monkeypatch, ("github-actions", "github-actions"))
    fact = await record_accepted(db, a)
    revision = await settle_accepted(db, a, fact)
    assert revision.status == "completed" and revision.completed_at is not None
    served.assert_awaited()


# --- #906: only amendments the Leader approved count toward the limit of three ---------------------------------


@pytest.mark.asyncio
async def test_a_rejected_amendment_does_not_spend_the_correction_budget(db, healthy_attempt):
    a = healthy_attempt
    await prior_amendment(a, db, 1, [])
    await prior_amendment(a, db, 2, [], status="completed", approved=True)
    await prior_amendment(a, db, 3, [], status="completed", approved=True)
    revision, approval, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    assert created and revision.revision == 4
    await github_approval_service.ensure_continuation_request_message(db, a.item, approval, revision)
    await github_approval_service.decide_continuation(db, a.item, authenticated_leader_member_id=a.leader.id,
        decision="approved", reason="Third approved amendment", request_id=approval.id)
    assert revision.status == "approved" and revision.approved_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "superseded", "expired", "cancelled", "active"])
async def test_every_amendment_ever_approved_counts_once_whatever_it_became(db, healthy_attempt, status):
    a = healthy_attempt
    await prior_amendment(a, db, 1, [], status="completed", approved=True)
    await prior_amendment(a, db, 2, [], status="completed", approved=True)
    await prior_amendment(a, db, 3, [], status=status, approved=True)
    with pytest.raises(GithubApprovalError, match="amendment_count_limit"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "expired", "superseded", "cancelled"])
async def test_amendments_that_were_never_approved_are_not_charged(db, healthy_attempt, status):
    a = healthy_attempt
    for number in range(1, 4):
        await prior_amendment(a, db, number, [], status=status)
    revision, _, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a))
    assert created and revision.revision == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,prior,allowed", [(8, 7, True), (8, 8, False), (1, 0, True), (1, 1, False)])
async def test_the_total_request_cap_is_separate_named_and_leaves_its_final_slot(db, healthy_attempt, limit, prior, allowed):
    a = healthy_attempt
    a.scope.max_continuation_revisions = 10  # the production scope budget; the request cap is under test
    a.scope.max_scope_amendment_requests = limit
    await db.commit()
    for number in range(1, prior + 1):
        await prior_amendment(a, db, number, [])
    if allowed:
        revision, _, created = await github_approval_service.create_scope_amendment_request(
            db, a.item, a.scope, **amendment(a))
        assert created and revision.revision == limit
    else:
        with pytest.raises(GithubApprovalError, match="amendment_request_limit"):
            await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))


@pytest.mark.asyncio
async def test_the_two_limits_name_themselves_differently(db, healthy_attempt):
    a = healthy_attempt
    a.scope.max_continuation_revisions = 10
    for number in range(1, 4):
        await prior_amendment(a, db, number, [], status="completed", approved=True)
    with pytest.raises(GithubApprovalError, match="amendment_count_limit"):
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    for number in range(4, 9):
        await prior_amendment(a, db, number, [])
    with pytest.raises(GithubApprovalError) as refused:
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    assert refused.value.detail in {"amendment_count_limit", "amendment_request_limit"}
    assert (a.scope.max_scope_amendments, a.scope.max_scope_amendment_requests) == (3, 8)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["max_scope_amendments", "max_scope_amendment_requests"])
async def test_revision_settlement_reads_current_scope_caps(db, submitted_attempt, field):
    from app.services.github_terminal_revision_service import end_submitted_revision

    a = submitted_attempt
    setattr(a.scope, field, 1)
    await db.commit()
    fact = await record_changes(db, a)
    await end_submitted_revision(db, a.item, review_id=fact.id, leader_member_id=a.leader.id,
        dispatch_nonce=a.item.dispatch_nonce, reason="Settle at the scope's changed cap")
    assert a.item.dispatch_status == "escalated" and a.item.escalation_reason == "amendment_budget_exhausted"


@pytest.mark.asyncio
async def test_the_decision_time_budget_charges_the_candidate_once(db, healthy_attempt):
    a = healthy_attempt
    await prior_amendment(a, db, 1, [], status="completed", approved=True)
    await prior_amendment(a, db, 2, [])
    revision, _, _ = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    await github_approval_service.validate_amendment_budget(db, a.item, a.scope, revision)
    a.scope.max_scope_amendments = 1
    await db.commit()
    with pytest.raises(GithubApprovalError, match="amendment_count_limit"):
        await github_approval_service.validate_amendment_budget(db, a.item, a.scope, revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,approved,code", [
    ("max_scope_amendments", True, "amendment_count_limit"),
    ("max_scope_amendment_requests", False, "amendment_request_limit"),
])
async def test_approval_rechecks_tightened_amendment_count_and_request_caps(db, healthy_attempt, field, approved, code):
    a = healthy_attempt
    await prior_amendment(a, db, 1, [], status="completed" if approved else "rejected", approved=approved)
    revision, request, _ = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    await github_approval_service.ensure_continuation_request_message(db, a.item, request, revision)
    setattr(a.scope, field, 1)
    await db.commit()
    with pytest.raises(GithubApprovalError) as refused:
        await github_approval_service.decide_continuation(
            db, a.item, authenticated_leader_member_id=a.leader.id,
            decision="approved", reason="Review the amendment", request_id=request.id)
    assert refused.value.detail == code
    await db.refresh(request)
    await db.refresh(revision)
    assert request.status == "pending" and revision.status == "proposed"


@pytest.mark.asyncio
async def test_the_activation_time_budget_does_not_charge_an_approved_revision_twice(db, healthy_attempt):
    a = healthy_attempt
    await prior_amendment(a, db, 1, [], status="completed", approved=True)
    await prior_amendment(a, db, 2, [], status="completed", approved=True)
    revision, _, _ = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    revision.status, revision.approved_at = "approved", datetime.utcnow()
    await db.commit()
    await github_approval_service.validate_amendment_budget(db, a.item, a.scope, revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [2, 8])
async def test_the_decision_time_request_cap_names_itself_and_keeps_the_final_slot(db, healthy_attempt, limit):
    a = healthy_attempt
    a.scope.max_continuation_revisions = 10
    a.scope.max_scope_amendment_requests = limit
    await db.commit()
    for number in range(1, limit - 1):
        await prior_amendment(a, db, number, [])
    revision, _, _ = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    await github_approval_service.validate_amendment_budget(db, a.item, a.scope, revision)
    await prior_amendment(a, db, limit, [])
    await github_approval_service.validate_amendment_budget(db, a.item, a.scope, revision)
    await prior_amendment(a, db, limit + 1, [])
    with pytest.raises(GithubApprovalError, match="amendment_request_limit"):
        await github_approval_service.validate_amendment_budget(db, a.item, a.scope, revision)


@pytest.mark.asyncio
async def test_a_rejected_history_does_not_block_the_decision_time_budget(db, healthy_attempt):
    a = healthy_attempt
    revision, _, _ = await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **amendment(a))
    for number in range(2, 5):
        await prior_amendment(a, db, number, [])
    await github_approval_service.validate_amendment_budget(db, a.item, a.scope, revision)


# --- #1154 (Supervisor option A): an amendment names its whole effective set; withdrawals are explicit ------------

async def _amendment_rows(db, item_id):
    from app.models.database import GithubAttemptScopeRevision
    return (await db.scalars(select(GithubAttemptScopeRevision).where(
        GithubAttemptScopeRevision.work_item_id == item_id))).all()


@pytest.mark.asyncio
async def test_an_amendment_that_omits_a_base_path_is_refused_before_anything_is_stored(db, healthy_attempt):
    # #1055: the owner named only the added path, the approved base fell out of allowed_paths, and completion then
    # refused the base-path changes. The omission is now refused when the amendment is proposed.
    a = healthy_attempt
    item_id, round_before = a.item.id, a.item.approval_round_count
    with pytest.raises(GithubApprovalError) as refused:
        await github_approval_service.create_scope_amendment_request(
            db, a.item, a.scope, **amendment(a, ["src/added.py"]))
    assert refused.value.detail == "amendment_base_paths_omitted"
    await db.rollback()
    assert await _amendment_rows(db, item_id) == []
    await db.refresh(a.item)
    assert a.item.approval_round_count == round_before


@pytest.mark.asyncio
async def test_an_explicit_withdrawal_narrows_the_scope(db, healthy_attempt):
    a = healthy_attempt
    revision, approval, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **amendment(a, ["src/added.py"]), withdrawn_paths=["src/base.py"])
    assert created and approval.status == "pending"
    assert revision.allowed_paths == ["src/added.py"]  # literal: the withdrawn base path is not allowed
    assert revision.amendment_base_paths == ["src/base.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("paths,withdrawn", [
    (["src/added.py"], ["src/other.py"]),  # not a base path
    (["src/base.py", "src/added.py"], ["src/base.py"]),  # still named
    (["src/added.py"], ["src/base.py", "src/added.py"]),  # more than the omission
])
async def test_a_withdrawal_must_be_exactly_the_omitted_base_paths(db, healthy_attempt, paths, withdrawn):
    a = healthy_attempt
    item_id = a.item.id
    with pytest.raises(GithubApprovalError) as refused:
        await github_approval_service.create_scope_amendment_request(
            db, a.item, a.scope, **amendment(a, paths), withdrawn_paths=withdrawn)
    assert refused.value.detail == "amendment_withdrawn_paths_invalid"
    await db.rollback()
    assert await _amendment_rows(db, item_id) == []


@pytest.mark.asyncio
async def test_the_api_passes_withdrawn_paths_through(db, healthy_attempt):
    from app.api.v1 import agent_teams
    from app.models.schemas import GithubScopeAmendmentCreate

    a = healthy_attempt
    args = amendment(a, ["src/added.py"])
    args.pop("authenticated_owner_member_id")
    args.pop("authenticated_owner_slot_id")
    session = await db.scalar(select(MailAgentSession).where(MailAgentSession.member_id == a.owner.id))
    accepted = await agent_teams.request_github_scope_amendment(
        a.item.id, GithubScopeAmendmentCreate(**args, withdrawn_paths=["src/base.py"]), session, db)
    assert accepted.revision.allowed_paths == ["src/added.py"]
    assert GithubScopeAmendmentCreate(**args).withdrawn_paths == []  # optional: old clients send nothing

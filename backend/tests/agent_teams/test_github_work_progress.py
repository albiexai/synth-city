"""Source-bound publication facts. These tests use disposable repos and DBs."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base, get_db
from app.models.database import AgentTeamPreset, AgentTeamSlot, GithubWorkItem, GithubWorkspace, TeamGithubScope
from app.models.github_work_progress import GithubWorkProgressSnapshot
from app.services.github_work_progress_observation import (
    LocalProgressObservation, ProgressObservationError, WorkspaceProgressContext, WorkspaceProgressObserver,
)
from app.services.github_work_progress_service import GithubWorkProgressService, _next_step
from app.api.v1 import github_work_progress as api
from mcp_shim.work_remaining_protocol import RemainingReport

SHA = "a" * 40
REMOTE = "b" * 40


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "task")
    git(path, "config", "user.email", "test@example.invalid")
    git(path, "config", "user.name", "Test")
    (path / "file").write_text("first")
    git(path, "add", "file")
    git(path, "commit", "-m", "first")
    return path


def context(repo):
    return WorkspaceProgressContext(str(repo), str(repo), "primary")


@pytest.mark.asyncio
async def test_exact_local_gap_and_matching_head(repo):
    observer = WorkspaceProgressObserver()
    published = git(repo, "rev-parse", "HEAD")
    (repo / "file").write_text("second")
    git(repo, "commit", "-am", "second")
    local = await observer.read(context(repo))
    assert await observer.compare(context(repo), local.sha, published) == ("ahead", 1)
    assert await observer.compare(context(repo), local.sha, local.sha) == ("synchronized", 0)
    assert await observer.compare(context(repo), published, local.sha) == ("behind", 0)
    assert local.tracked_changes is None and local.untracked_files is None
    assert await observer.confirm(context(repo), local)
    git(repo, "checkout", "--detach")
    assert (await observer.read(context(repo))).branch is None
    assert not await observer.confirm(context(repo), local)


@pytest.mark.asyncio
async def test_diverged_history_and_missing_object(repo):
    observer = WorkspaceProgressObserver()
    git(repo, "checkout", "-b", "other")
    (repo / "other").write_text("remote")
    git(repo, "add", "other"); git(repo, "commit", "-m", "other")
    published = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "task")
    (repo / "local").write_text("local")
    git(repo, "add", "local"); git(repo, "commit", "-m", "local")
    local = await observer.read(context(repo))
    assert await observer.compare(context(repo), local.sha, published) == ("diverged", 1)
    with pytest.raises(ProgressObservationError, match="published_object_unavailable"):
        await observer.compare(context(repo), local.sha, REMOTE)
    (repo / ".git/shallow").write_text(local.sha + "\n")
    with pytest.raises(ProgressObservationError, match="ancestry_unavailable"):
        await observer.compare(context(repo), local.sha, published)


@pytest.mark.asyncio
async def test_filters_dirty_untracked_and_promisor_never_execute(repo, tmp_path, monkeypatch):
    marker = tmp_path / "executed"
    git(repo, "config", "filter.test.clean", f"touch {marker}; cat")
    git(repo, "config", "filter.test.process", f"touch {marker}")
    (repo / ".git/info/attributes").write_text("* filter=test\n")
    (repo / "file").write_text("dirty")
    (repo / "private.env").write_text("private-file-name")
    git(repo, "config", "remote.evil.promisor", "true")
    git(repo, "config", "remote.evil.url", f"ext::touch {marker}")
    git(repo, "config", "protocol.ext.allow", "always")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"touch {marker}")
    monkeypatch.setenv("SECRET_PROGRESS_TEST", "must-not-enter-child")
    before = (repo / ".git/index").read_bytes()
    observer = WorkspaceProgressObserver()
    local = await observer.read(context(repo))
    assert local.tracked_changes is None and local.untracked_files is None
    with pytest.raises(ProgressObservationError, match="published_object_unavailable"):
        await observer.compare(context(repo), local.sha, REMOTE)
    assert await observer.confirm(context(repo), local)
    assert not marker.exists()
    assert (repo / ".git/index").read_bytes() == before
    assert (repo / "file").read_text() == "dirty"
    assert (repo / "private.env").read_text() == "private-file-name"


@pytest.mark.asyncio
async def test_registered_linked_worktree_and_invalid_paths(repo, tmp_path):
    workspace = tmp_path / "owner"
    git(repo, "worktree", "add", "-b", "owner", str(workspace))
    observer = WorkspaceProgressObserver()
    linked = WorkspaceProgressContext(str(repo), str(workspace), "worktree")
    assert (await observer.read(linked)).branch == "owner"
    other = tmp_path / "other"; other.mkdir()
    git(other, "init")
    with pytest.raises(ProgressObservationError, match="workspace_identity_unavailable"):
        await observer.read(WorkspaceProgressContext(str(repo), str(other), "worktree"))
    alias = tmp_path / "alias"; alias.symlink_to(repo)
    with pytest.raises(ProgressObservationError, match="workspace_identity_unavailable"):
        await observer.read(context(alias))
    with pytest.raises(ProgressObservationError, match="workspace_identity_unavailable"):
        await observer.read(context(tmp_path / "missing"))


@pytest_asyncio.fixture
async def db(monkeypatch):
    monkeypatch.setattr("app.services.github_work_progress_service.hold_code", lambda: None)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", hide_parameters=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def seed(db):
    preset = AgentTeamPreset(name="Progress", created_by="test", autonomy_enabled=True)
    db.add(preset); await db.flush()
    slot = AgentTeamSlot(preset_id=preset.id, position=0, display_name="Owner", provider="codex-cli",
                         repo_id="repo", repo_path="/safe/repo", repo_name="repo", launch_mode="plain", launch_options={})
    db.add(slot); await db.flush()
    scope = TeamGithubScope(preset_id=preset.id, repo_owner="owner", repo_name="repo", repo_path="/safe/repo",
                            merge_policy="human", enabled=True)
    db.add(scope); await db.flush()
    item = GithubWorkItem(scope_id=scope.id, issue_number=1, issue_title="Progress", issue_url="https://github.com/owner/repo/issues/1",
                          github_updated_at=datetime.utcnow(), owner_slot_id=slot.id, dispatch_status="dispatched",
                          dispatch_nonce="public-dispatch-id", dispatch_head_ref="task", ack_received_at=datetime.utcnow())
    db.add(item); await db.flush()
    workspace = GithubWorkspace(scope_id=scope.id, kind="primary", path=scope.repo_path,
                                leased_item_id=item.id, lease_token="private-lease-value", leased_at=datetime.utcnow())
    db.add(workspace); await db.commit()
    return item, scope, preset, workspace


class Observer:
    calls = 0
    changed = False
    async def read(self, context):
        self.calls += 1
        return LocalProgressObservation(SHA, "task")
    async def confirm(self, context, local):
        return not self.changed
    async def compare(self, context, local, remote):
        return ("synchronized", 0) if local == remote else ("ahead", 2)


class Client:
    calls = 0
    sha = REMOTE
    missing = False
    async def get_ref(self, *args, **kwargs):
        self.calls += 1
        return None if self.missing else {"ref": "refs/heads/task", "object": {"sha": self.sha, "type": "commit"}}


@pytest.mark.asyncio
async def test_cache_reuse_release_and_private_fields(db):
    item, scope, preset, workspace = await seed(db)
    observer, client = Observer(), Client()
    service = GithubWorkProgressService(observer, client)
    first = await service.summary(db, item.id)
    assert first.publication.unpublished_commits == 2
    assert first.publication.publication_time_source == "github_head_observation"
    assert first.publication.tracked_changes is None
    assert (first.phase, first.next_actor) == ("implementation", "owner")
    assert first.last_check_head is None
    saved = await db.get(GithubWorkProgressSnapshot, item.id)
    assert "private-lease-value" not in str(saved.identity) + str(saved.observation)
    assert "/safe/repo" not in first.model_dump_json()
    assert "private-lease-value" not in first.model_dump_json()
    second = await service.summary(db, item.id)
    assert observer.calls == 1 and client.calls == 1
    assert second.publication.publication_first_observed_at == first.publication.publication_first_observed_at
    workspace.leased_item_id = None; workspace.lease_token = None
    item.dispatch_status = "merged"
    await db.commit()
    released = await service.summary(db, item.id)
    assert released.publication.state == "historical" and released.publication.local_sha == SHA
    assert observer.calls == 1
    assert (released.phase, released.next_actor) == ("complete", "none")
    assert item.retry_count == 0 and item.approval_round_count == 0 and preset.autonomy_enabled


@pytest.mark.asyncio
async def test_new_read_keeps_first_observation_time_and_handles_missing_remote(db):
    item, *_ = await seed(db)
    observer, client = Observer(), Client()
    service = GithubWorkProgressService(observer, client)
    first = await service.summary(db, item.id)
    await db.execute(update(GithubWorkProgressSnapshot).values(observed_at=datetime.utcnow() - timedelta(seconds=30)))
    await db.commit()
    refreshed = await service.summary(db, item.id)
    assert observer.calls == 2
    assert refreshed.publication.publication_first_observed_at == first.publication.publication_first_observed_at
    client.missing = True
    await db.execute(update(GithubWorkProgressSnapshot).values(observed_at=datetime.utcnow() - timedelta(seconds=30)))
    await db.commit()
    missing = await service.summary(db, item.id)
    assert missing.publication.state == "historical" and missing.publication.reason == "remote_unavailable"
    assert missing.publication.observed_at == refreshed.publication.observed_at


@pytest.mark.asyncio
async def test_changed_source_and_dispatch_do_not_publish_current_facts(db):
    item, *_ = await seed(db)
    observer = Observer(); observer.changed = True
    service = GithubWorkProgressService(observer, Client())
    changed = await service.summary(db, item.id)
    assert changed.publication.state == "unavailable" and changed.publication.reason == "changed_during_read"
    assert await db.get(GithubWorkProgressSnapshot, item.id) is None
    observer.changed = False
    class RaceClient(Client):
        async def get_ref(self, *args, **kwargs):
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(dispatch_nonce="new-dispatch"))
            await db.commit()
            return await super().get_ref(*args, **kwargs)
    service.client = RaceClient()
    raced = await service.summary(db, item.id)
    assert raced.dispatch_nonce == "new-dispatch"
    assert raced.publication.state == "unavailable" and raced.publication.local_sha is None
    assert await db.get(GithubWorkProgressSnapshot, item.id) is None


@pytest.mark.asyncio
async def test_route_is_safe_read_no_store_and_missing_item_404(db, monkeypatch):
    item, *_ = await seed(db)
    app = FastAPI(); app.include_router(api.router, prefix="/agent-teams")
    async def session(): yield db
    app.dependency_overrides[get_db] = session
    monkeypatch.setattr(api, "github_work_progress_service", GithubWorkProgressService(Observer(), Client()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/agent-teams/github-work-items/{item.id}/progress")
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["publication"]["file_counts_reason"] == "safe_metadata_read"
        assert (await client.get("/agent-teams/github-work-items/999/progress")).status_code == 404


@pytest.mark.parametrize("status, pending, hold, phase, actor", [
    ("dispatched", None, None, "implementation", "owner"),
    ("dispatched", 1, None, "plan_review", "leader"),
    ("ready_for_review", None, None, "review", "operator"),
    ("awaiting_human_review", None, None, "review", "operator"),
    ("verifying", None, None, "ci", "controller"),
    ("escalated", None, None, "intervention", "operator"),
    ("dispatched", None, "hold", "paused", "operator"),
    ("merged", None, "hold", "complete", "none"),
])
def test_phase_actors_do_not_grant_authority(status, pending, hold, phase, actor):
    item = SimpleNamespace(dispatch_status=status, attempt_phase="implementation", status_note=None,
                           handoff_state=None, ack_received_at=datetime.now(timezone.utc))
    scope = SimpleNamespace(enabled=True, merge_policy="human")
    preset = SimpleNamespace(autonomy_enabled=True)
    assert _next_step(item, scope, preset, pending, hold)[:2] == (phase, actor)


@pytest.mark.asyncio
async def test_git_child_is_bounded_and_has_no_inherited_secrets(repo, monkeypatch):
    import app.services.github_work_progress_observation as module
    monkeypatch.setenv("SECRET_PROGRESS_TEST", "private-value")
    captured, killed, finished = {}, [], []
    class Stream:
        async def read(self, size): return b"x" * 64
    class Process:
        pid = 123456789
        returncode = None
        stdout = Stream()
        async def wait(self): self.returncode = 0; finished.append(True); return 0
    async def spawn(*args, **kwargs): captured.update(args=args, **kwargs); return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(module, "_MAX_OUTPUT", 32)
    monkeypatch.setattr(module.os, "killpg", lambda pid, signal: killed.append(pid))
    with pytest.raises(ProgressObservationError, match="observation_limit"):
        await module.WorkspaceProgressObserver._run_git(str(repo), ["rev-parse", "HEAD"])
    assert "SECRET_PROGRESS_TEST" not in captured["env"]
    assert Path(captured["args"][0]).is_absolute()
    assert captured["start_new_session"] and killed == [123456789] and finished == [True]
    assert captured["env"]["GIT_TERMINAL_PROMPT"] == "0"
    class HangingStream:
        async def read(self, size): await asyncio.Event().wait()
    Process.stdout = HangingStream()
    monkeypatch.setattr(module, "_COMMAND_SECONDS", 0.01)
    with pytest.raises(ProgressObservationError, match="observation_timeout"):
        await module.WorkspaceProgressObserver._run_git(str(repo), ["rev-parse", "HEAD"])
    assert finished == [True, True]


@pytest.mark.asyncio
async def test_private_object_view_is_removed_after_limit(repo):
    paths = []
    async def runner(path, args):
        if args[0] == "cat-file": return 0, b"commit\n"
        if args[0] == "rev-list":
            view = Path(path); paths.append(view)
            assert "remote" not in (view / "config").read_text()
            assert "commitGraph = false" in (view / "config").read_text()
            assert (view / "objects").is_symlink()
            return 0, b"0 10001\n"
        return await WorkspaceProgressObserver._run_git(path, args)
    with pytest.raises(ProgressObservationError, match="observation_limit"):
        await WorkspaceProgressObserver(runner).compare(context(repo), git(repo, "rev-parse", "HEAD"), REMOTE)
    assert len(paths) == 1 and not paths[0].exists()


@pytest.mark.asyncio
async def test_tag_pointer_is_not_peeled_for_commit_counts(repo):
    git(repo, "tag", "-a", "tag", "-m", "annotated")
    tag = git(repo, "rev-parse", "refs/tags/tag")
    with pytest.raises(ProgressObservationError, match="published_object_unavailable"):
        await WorkspaceProgressObserver().compare(context(repo), tag, git(repo, "rev-parse", "HEAD"))


@pytest.mark.asyncio
async def test_lease_revocation_during_remote_read_grants_no_authority(db):
    item, scope, preset, workspace = await seed(db)
    class RevokeClient(Client):
        async def get_ref(self, *args, **kwargs):
            await db.execute(update(GithubWorkspace).where(GithubWorkspace.id == workspace.id).values(lease_token=None, leased_item_id=None))
            await db.commit()
            return await super().get_ref(*args, **kwargs)
    value = await GithubWorkProgressService(Observer(), RevokeClient()).summary(db, item.id)
    assert value.publication.state == "unavailable" and value.publication.reason == "changed_during_read"
    assert await db.get(GithubWorkProgressSnapshot, item.id) is None
    await db.refresh(item); await db.refresh(preset); await db.refresh(workspace)
    assert item.retry_count == 0 and item.dispatch_status == "dispatched"
    assert workspace.lease_token is None and preset.autonomy_enabled


@pytest.mark.asyncio
async def test_missing_remote_without_prior_snapshot_is_not_zero(db):
    item, *_ = await seed(db)
    client = Client(); client.missing = True
    value = await GithubWorkProgressService(Observer(), client).summary(db, item.id)
    assert value.publication.reason == "remote_unavailable" and value.publication.state == "unavailable"
    assert value.publication.published_sha is None and value.publication.unpublished_commits is None


@pytest.mark.asyncio
async def test_route_timeout_and_disposable_database_startup(db, monkeypatch):
    from app import database
    original_wait = asyncio.wait_for
    async def short_wait(operation, timeout): return await original_wait(operation, timeout=0.01)
    class Hanging:
        async def summary(self, *args): await asyncio.Event().wait()
    monkeypatch.setattr(api.asyncio, "wait_for", short_wait)
    monkeypatch.setattr(api, "github_work_progress_service", Hanging())
    app = FastAPI(); app.include_router(api.router)
    async def session(): yield db
    app.dependency_overrides[get_db] = session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/github-work-items/1/progress")
        assert response.status_code == 409 and response.json()["detail"] == "progress_observation_unavailable"
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(database, "engine", engine)
    await database.init_db()
    from sqlalchemy import inspect
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
        assert "github_work_progress_snapshots" in tables
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_final_context_refresh_after_failed_observation(db, failure):
    item, scope, preset, workspace = await seed(db)
    item_id = item.id
    class ChangingObserver(Observer):
        async def read(self, context):
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(dispatch_nonce="new-dispatch", owner_slot_id=None))
            await db.execute(update(AgentTeamPreset).where(AgentTeamPreset.id == preset.id).values(autonomy_enabled=False))
            await db.commit()
            if failure == "timeout": raise TimeoutError
            raise ProgressObservationError("git_unavailable")
    result = await GithubWorkProgressService(ChangingObserver(), Client()).summary(db, item_id)
    assert result.dispatch_nonce == "new-dispatch" and result.owner_slot_id is None
    assert (result.phase, result.next_actor) == ("paused", "operator")
    assert result.publication.state == "unavailable" and result.publication.local_sha is None
    assert item.retry_count == 0


@pytest.mark.asyncio
async def test_release_after_cache_write_before_final_load_is_historical(db):
    item, scope, preset, workspace = await seed(db)
    workspace_id = workspace.id
    class ReleasingService(GithubWorkProgressService):
        async def _rows(self, db, item_id, *, final=False):
            if final:
                await db.execute(update(GithubWorkspace).where(GithubWorkspace.id == workspace_id).values(leased_item_id=None, lease_token=None))
                await db.commit()
            return await super()._rows(db, item_id, final=final)
    result = await ReleasingService(Observer(), Client()).summary(db, item.id)
    assert result.publication.state == "historical" and result.publication.reason == "workspace_not_leased"
    assert result.publication.local_sha == SHA
    assert (result.phase, result.next_actor) == ("workspace_wait", "leader")


@pytest.mark.asyncio
async def test_same_session_lease_timestamp_refresh_cannot_substitute_guard(db):
    item, scope, preset, workspace = await seed(db)
    changed_time = workspace.leased_at + timedelta(seconds=1)
    class TimestampClient(Client):
        async def get_ref(self, *args, **kwargs):
            workspace.leased_at = changed_time
            await db.commit()
            await db.refresh(workspace)
            return await super().get_ref(*args, **kwargs)
    result = await GithubWorkProgressService(Observer(), TimestampClient()).summary(db, item.id)
    assert result.publication.state == "unavailable" and result.publication.reason == "changed_during_read"
    assert await db.get(GithubWorkProgressSnapshot, item.id) is None
    assert workspace.leased_at == changed_time and workspace.lease_token == "private-lease-value"


@pytest.mark.asyncio
async def test_cache_hit_has_final_lease_validation(db):
    item, scope, preset, workspace = await seed(db)
    service = GithubWorkProgressService(Observer(), Client())
    await service.summary(db, item.id)
    workspace_id = workspace.id
    class ReleasingService(GithubWorkProgressService):
        async def _rows(self, db, item_id, *, final=False):
            if final:
                await db.execute(update(GithubWorkspace).where(GithubWorkspace.id == workspace_id).values(leased_item_id=None, lease_token=None))
                await db.commit()
            return await super()._rows(db, item_id, final=final)
    result = await ReleasingService(service.observer, service.client).summary(db, item.id)
    assert service.observer.calls == 1
    assert result.publication.state == "historical"
    assert (result.phase, result.next_actor) == ("workspace_wait", "leader")


@pytest.mark.asyncio
async def test_creation_timestamp_is_captured_before_async_read(db):
    item, *_ = await seed(db)
    changed_time = item.created_at + timedelta(seconds=1)
    class ChangingClient(Client):
        async def get_ref(self, *args, **kwargs):
            item.created_at = changed_time
            await db.commit()
            await db.refresh(item)
            return await super().get_ref(*args, **kwargs)
    result = await GithubWorkProgressService(Observer(), ChangingClient()).summary(db, item.id)
    assert result.publication.state == "unavailable" and result.publication.local_sha is None
    assert await db.get(GithubWorkProgressSnapshot, item.id) is None


@pytest.mark.asyncio
async def test_pending_approval_matches_current_dispatch_and_round(db):
    from app.models.database import GithubApprovalRequest
    item, *_ = await seed(db)
    item.approval_round_count = 1
    request = GithubApprovalRequest(work_item_id=item.id, request_kind="initial_plan", dispatch_nonce="old-dispatch",
        approval_round=1, owner_member_id=1, leader_member_id=2, request_fingerprint="fixture", status="pending")
    db.add(request); await db.commit()
    service = GithubWorkProgressService(Observer(), Client())
    assert (await service.summary(db, item.id)).next_actor == "owner"
    request.dispatch_nonce = item.dispatch_nonce
    request.approval_round = 0
    await db.commit()
    assert (await service.summary(db, item.id)).next_actor == "owner"
    request.approval_round = 1
    await db.commit()
    value = await service.summary(db, item.id)
    assert (value.phase, value.next_actor) == ("plan_review", "leader")


@pytest.mark.parametrize("status, merge, attempt, note, ack, pending, expected", [
    ("ready_for_review", "auto", "implementation", None, True, None, ("review", "controller")),
    ("ready_for_review", "auto", "implementation", "Auto-merge blocked: test", True, None, ("review", "operator")),
    ("ready_for_review", "human", "diagnostic", None, True, None, ("diagnostic_review", "leader")),
    ("dispatched", "human", "implementation", None, False, None, ("planning", "owner")),
    ("awaiting_human_review", "human", "implementation", None, True, 1, ("plan_review", "leader")),
])
def test_review_and_planning_variants_retain_existing_gates(status, merge, attempt, note, ack, pending, expected):
    item = SimpleNamespace(dispatch_status=status, attempt_phase=attempt, status_note=note,
                           handoff_state=None, ack_received_at=datetime.utcnow() if ack else None)
    scope = SimpleNamespace(enabled=True, merge_policy=merge)
    assert _next_step(item, scope, SimpleNamespace(autonomy_enabled=True), pending, None)[:2] == expected


class ReportClient(Client):
    sha = SHA
    issue_calls = 0
    def __init__(self, item, **changes):
        self.report = RemainingReport(work_item_id=item.id, generation=_generation(item),
            owner_slot_id=item.owner_slot_id, scope_revision=item.active_scope_revision, source_sha=SHA,
            phase="implementation", reported_at=datetime.now(timezone.utc), reported_by="Team member 2",
            remaining="Fix the interface, then review and merge.", next_action="B2 publishes the next checkpoint.",
            **changes)
        self.body = self.report.markdown()
    async def get_issue(self, owner, repo, number, **kwargs):
        self.issue_calls += 1
        return {"number": number, "html_url": f"https://github.com/{owner}/{repo}/issues/{number}", "body": self.body}


@pytest.mark.asyncio
async def test_remaining_report_current_cache_and_phase_change(db):
    item, scope, preset, workspace = await seed(db)
    client = ReportClient(item, effort_low_minutes=30, effort_high_minutes=60, confidence="low", effort_scope="interface fixes")
    service = GithubWorkProgressService(Observer(), client)
    first = await service.summary(db, item.id)
    assert first.remaining_work.state == "current"
    assert first.remaining_work.estimate == "About 30–60 minutes of active work (low confidence; interface fixes)."
    assert first.remaining_work.effort_low_minutes == 30
    assert first.remaining_work.effort_high_minutes == 60
    assert first.remaining_work.confidence == "low"
    assert first.remaining_work_context.source_sha == SHA
    saved = await db.get(GithubWorkProgressSnapshot, item.id)
    assert saved.observation["_remaining_report"]["report"]["remaining"] == client.report.remaining
    assert client.body not in str(saved.observation)
    assert (await service.summary(db, item.id)).remaining_work.state == "current"
    assert client.issue_calls == 1
    item.dispatch_status = "ready_for_review"
    await db.commit()
    changed = await service.summary(db, item.id)
    assert (changed.phase, changed.next_actor) == ("review", "operator")
    assert changed.remaining_work.state == "historical" and changed.remaining_work.reason == "context_changed"
    assert changed.remaining_work.effort_low_minutes is None
    assert changed.remaining_work.effort_high_minutes is None
    assert item.retry_count == 0 and item.approval_round_count == 0 and preset.autonomy_enabled
    assert workspace.lease_token == "private-lease-value"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "invalid", "private", "wrong_issue", "unavailable", "old", "ahead"])
async def test_remaining_reports_do_not_manufacture_current_estimates(db, failure):
    item, *_ = await seed(db)
    client = ReportClient(item)
    if failure == "missing": client.body = "Original issue facts."
    if failure == "invalid": client.body = client.body.replace("Unknown.", "Almost done.")
    if failure == "private": client.body = client.body.replace(client.report.remaining, "private-lease-value")
    if failure == "old":
        client.body = client.report.model_copy(update={"reported_at": datetime.now(timezone.utc) - timedelta(hours=3)}).markdown()
    if failure == "ahead": client.sha = REMOTE
    if failure == "wrong_issue":
        async def bad_issue(*args, **kwargs): return {"number": 2, "html_url": "https://github.com/owner/repo/issues/2", "body": client.body}
        client.get_issue = bad_issue
    if failure == "unavailable":
        async def unavailable(*args, **kwargs): raise httpx.ConnectError("unavailable")
        client.get_issue = unavailable
    value = await GithubWorkProgressService(Observer(), client).summary(db, item.id)
    assert value.remaining_work.state != "current"
    assert "private-lease-value" not in value.model_dump_json()
    if failure in {"old", "ahead"}:
        assert value.remaining_work.state == "historical"
    else:
        assert value.remaining_work.estimate is None


@pytest.mark.asyncio
async def test_remaining_report_scope_race_is_not_cached(db):
    item, *_ = await seed(db)
    class Race(ReportClient):
        async def get_issue(self, *args, **kwargs):
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(active_scope_revision=1))
            await db.commit()
            return await super().get_issue(*args, **kwargs)
    value = await GithubWorkProgressService(Observer(), Race(item)).summary(db, item.id)
    assert value.remaining_work_context is None and value.remaining_work.state != "current"
    assert await db.get(GithubWorkProgressSnapshot, item.id) is None


@pytest.mark.asyncio
async def test_remaining_context_requires_current_scoped_leader_and_rechecks_after_reads(db, monkeypatch):
    from app.config import settings
    from app.models.database import MailAgentSession, MailTeamMember
    from app.services.agent_mail_service import agent_mail_service
    item, scope, preset, workspace = await seed(db)
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr("app.utils.peer_process.pane_is_alive", lambda *_: True)
    owner_slot = await db.get(AgentTeamSlot, item.owner_slot_id)
    owner_slot.position = 1
    slot = AgentTeamSlot(preset_id=preset.id, position=0, display_name="Leader", role="Leader",
        provider="codex-cli", repo_id="repo", repo_path="/safe/leader", repo_name="repo")
    db.add(slot); await db.flush()
    preset.leader_slot_id = slot.id
    member = MailTeamMember(identity_key="leader", repo_id="repo", repo_path=slot.repo_path,
        repo_name="repo", display_name="Leader", participant_kind="team_slot", team_preset_id=preset.id, team_slot_id=slot.id)
    db.add(member); await db.flush()
    leader = MailAgentSession(member_id=member.id, provider="codex-cli", source="mcp", session_key="leader",
        team_preset_id=preset.id, team_slot_id=slot.id, capability_token_hash=agent_mail_service.hash_capability_token("leader-token"),
        bound_pane_pid=1234, bound_pane_proc_start="1", wake_enabled=True)
    outsider = MailAgentSession(member_id=member.id, provider="codex-cli", source="mcp", session_key="outsider",
        capability_token_hash=agent_mail_service.hash_capability_token("other-token"))
    db.add_all([leader, outsider]); await db.commit()
    app = FastAPI(); app.include_router(api.router)
    async def session(): yield db
    app.dependency_overrides[get_db] = session
    observer = Observer()
    monkeypatch.setattr(api, "github_work_progress_service", GithubWorkProgressService(observer, ReportClient(item)))
    path = f"/github-work-items/{item.id}/remaining-work-context"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get(path)).status_code == 401
        assert (await http.get(path, headers={"X-Synth-City-Session-Token": "other-token"})).status_code == 403
        response = await http.get(path, headers={"X-Synth-City-Session-Token": "leader-token"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.json()["remaining_work_context"]["owner_slot_id"] == item.owner_slot_id
        await db.execute(update(GithubWorkProgressSnapshot).values(observed_at=datetime.utcnow() - timedelta(seconds=30)))
        await db.commit()
        class ClosingObserver(Observer):
            async def read(self, context):
                await db.execute(update(MailAgentSession).where(MailAgentSession.id == leader.id).values(closed_at=datetime.utcnow()))
                await db.commit()
                return await super().read(context)
        monkeypatch.setattr(api, "github_work_progress_service", GithubWorkProgressService(ClosingObserver(), ReportClient(item)))
        closed = await http.get(path, headers={"X-Synth-City-Session-Token": "leader-token"})
        assert closed.status_code == 409 and closed.json()["detail"] == "leader_unavailable"


# --- #976: the remaining-work context of a TERMINAL item after its workspace is released -----------------------

async def _released(db, status="merged", last_verified=SHA):
    item, scope, preset, workspace = await seed(db)
    item.dispatch_status = status
    item.last_verified_sha = last_verified
    workspace.leased_item_id = None
    workspace.lease_token = None
    await db.commit()
    return item, scope, preset, workspace


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["merged", "completed", "closed"])
async def test_a_terminal_released_item_still_has_a_remaining_work_context(db, status):
    item, *_ = await _released(db, status)
    result = await GithubWorkProgressService(Observer(), Client()).summary(db, item.id)
    context = result.remaining_work_context
    assert context is not None, "the Leader cannot prepare the post-merge summary (409 progress_context_unavailable)"
    assert (context.work_item_id, context.generation, context.owner_slot_id) == (
        item.id, _generation(item), item.owner_slot_id)
    assert (context.scope_revision, context.source_sha) == (item.active_scope_revision, SHA)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_verified_head", "bad_verified_head", "nonterminal", "workspace_still_leased"])
async def test_the_terminal_context_is_withheld_without_recorded_facts(db, case):
    status = "dispatched" if case == "nonterminal" else "merged"
    verified = None if case == "no_verified_head" else ("not-a-sha" if case == "bad_verified_head" else SHA)
    item, scope, preset, workspace = await _released(db, status, verified)
    if case == "workspace_still_leased":
        workspace.leased_item_id, workspace.lease_token = item.id, "private-lease-value"
        await db.commit()
    result = await GithubWorkProgressService(Observer(), Client()).summary(db, item.id)
    # a leased workspace keeps the existing rule (local and published sha must match): here they differ
    assert result.remaining_work_context is None


@pytest.mark.asyncio
async def test_the_terminal_context_publishes_no_authority_value(db):
    item, *_ = await _released(db)
    result = await GithubWorkProgressService(Observer(), Client()).summary(db, item.id)
    text = result.model_dump_json()
    assert "private-lease-value" not in text and "/safe/repo" not in text
    assert result.remaining_work_context is not None  # the context carries the nonce the existing report protocol binds


@pytest.mark.asyncio
async def test_a_changed_identity_during_the_terminal_read_gives_no_context(db, monkeypatch):
    item, *_ = await _released(db)
    service = GithubWorkProgressService(Observer(), Client())
    real = service._rows
    state = {"calls": 0}

    async def rows(session, item_id, final=False):
        state["calls"] += 1
        if final:
            row = await session.get(GithubWorkItem, item_id, populate_existing=True)
            row.dispatch_nonce = "a-different-dispatch"
            await session.commit()
        return await real(session, item_id, final=final)
    monkeypatch.setattr(service, "_rows", rows)
    result = await service.summary(db, item.id)
    assert result.remaining_work_context is None


@pytest.mark.asyncio
async def test_the_leader_route_returns_the_terminal_context_after_release_and_keeps_its_fences(db, monkeypatch):
    from app.config import settings
    from app.models.database import MailAgentSession, MailTeamMember
    from app.services.agent_mail_service import agent_mail_service
    item, scope, preset, workspace = await seed(db)
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr("app.utils.peer_process.pane_is_alive", lambda *_: True)
    owner_slot = await db.get(AgentTeamSlot, item.owner_slot_id)
    owner_slot.position = 1
    slot = AgentTeamSlot(preset_id=preset.id, position=0, display_name="Leader", role="Leader",
        provider="codex-cli", repo_id="repo", repo_path="/safe/leader", repo_name="repo")
    db.add(slot); await db.flush()
    preset.leader_slot_id = slot.id
    member = MailTeamMember(identity_key="leader", repo_id="repo", repo_path=slot.repo_path,
        repo_name="repo", display_name="Leader", participant_kind="team_slot", team_preset_id=preset.id, team_slot_id=slot.id)
    db.add(member); await db.flush()
    leader = MailAgentSession(member_id=member.id, provider="codex-cli", source="mcp", session_key="leader",
        team_preset_id=preset.id, team_slot_id=slot.id, capability_token_hash=agent_mail_service.hash_capability_token("leader-token"),
        bound_pane_pid=1234, bound_pane_proc_start="1", wake_enabled=True)
    outsider = MailAgentSession(member_id=member.id, provider="codex-cli", source="mcp", session_key="outsider",
        capability_token_hash=agent_mail_service.hash_capability_token("other-token"))
    db.add_all([leader, outsider])
    item.dispatch_status, item.last_verified_sha = "merged", SHA
    workspace.leased_item_id, workspace.lease_token = None, None
    await db.commit()
    app = FastAPI(); app.include_router(api.router)
    async def session(): yield db
    app.dependency_overrides[get_db] = session
    monkeypatch.setattr(api, "github_work_progress_service", GithubWorkProgressService(Observer(), Client()))
    path = f"/github-work-items/{item.id}/remaining-work-context"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get(path)).status_code == 401
        assert (await http.get(path, headers={"X-Synth-City-Session-Token": "other-token"})).status_code == 403
        response = await http.get(path, headers={"X-Synth-City-Session-Token": "leader-token"})
        assert response.status_code == 200, response.text
        context = response.json()["remaining_work_context"]
        assert context["source_sha"] == SHA and context["owner_slot_id"] == item.owner_slot_id
        assert "private-lease-value" not in response.text and "/safe/repo" not in response.text
        # the verified head is gone from the record: the same route refuses again instead of inventing a context
        item.last_verified_sha = None
        await db.commit()
        refused = await http.get(path, headers={"X-Synth-City-Session-Token": "leader-token"})
        assert refused.status_code == 409 and refused.json()["detail"] == "progress_context_unavailable"


# --- #971 folded into #976: no dispatch nonce in any public report value; terminal facts after the last await -----

NONCE = "public-dispatch-id"


def _report_from(context):
    return RemainingReport(**{**context.model_dump(), "reported_at": datetime.now(timezone.utc),
        "reported_by": "Team member 2", "remaining": "Review and merge.", "next_action": "V1 reviews the head."})


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["in_flight", "terminal"])
async def test_no_report_value_carries_the_dispatch_nonce(db, state):
    if state == "terminal":
        item, *_ = await _released(db)
        service = GithubWorkProgressService(Observer(), Client())
    else:
        item, *_ = await seed(db)
        service = GithubWorkProgressService(Observer(), type("C", (Client,), {"sha": SHA})())
    result = await service.summary(db, item.id)
    context = result.remaining_work_context
    assert context is not None
    assert NONCE not in context.model_dump_json()                 # the context a Leader receives
    report = _report_from(context)
    assert NONCE not in report.markdown() and NONCE not in report.model_dump_json()   # the published block and marker
    assert "dispatch_nonce" not in context.model_dump()


def test_a_published_block_that_still_carries_a_nonce_is_never_trusted():
    from mcp_shim.work_remaining_protocol import parse_report
    good = RemainingReport(work_item_id=1, owner_slot_id=2, scope_revision=0, source_sha=SHA, phase="review",
        generation="a" * 64, reported_at=datetime.now(timezone.utc), reported_by="Team member 2",
        remaining="Review.", next_action="V1 reviews.")
    assert parse_report(good.markdown()) is not None
    old = good.markdown().replace('{"work_item_id"', '{"dispatch_nonce":"old-nonce","work_item_id"', 1)
    assert "old-nonce" in old and parse_report(old) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["nonterminal", "replaced_head", "workspace_leased", "status_swap"])
async def test_terminal_facts_that_change_during_the_final_check_view_give_no_context(db, monkeypatch, late):
    item, scope, preset, workspace = await _released(db)
    item_id, workspace_id = item.id, workspace.id  # plain values: the service rolls the session back after the await
    from app.services import github_ci_watch_service as watch_module
    real = watch_module.github_ci_watch_service.view

    async def view(session, row):
        if late == "nonterminal":
            await session.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(dispatch_status="dispatched"))
        elif late == "status_swap":   # still terminal, but not the status the phase and next step were computed from
            await session.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(dispatch_status="closed"))
        elif late == "replaced_head":
            await session.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(last_verified_sha=REMOTE))
        else:
            await session.execute(update(GithubWorkspace).where(GithubWorkspace.id == workspace_id).values(
                leased_item_id=item_id, lease_token="private-lease-value", leased_at=datetime.utcnow()))
        await session.commit()
        return await real(session, row)
    monkeypatch.setattr(watch_module.github_ci_watch_service, "view", view)
    result = await GithubWorkProgressService(Observer(), Client()).summary(db, item_id)
    context = result.remaining_work_context
    if late == "replaced_head":   # a coherent re-read binds the NEW recorded head or refuses; never the old one
        assert context is None or context.source_sha == REMOTE
    elif late == "status_swap":
        assert context is None
    else:
        assert context is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["in_flight", "terminal"])
async def test_the_leader_route_response_never_carries_the_dispatch_nonce(db, monkeypatch, state):
    if state == "terminal":
        item, scope, preset, workspace = await _released(db)
        service = GithubWorkProgressService(Observer(), Client())
    else:
        item, scope, preset, workspace = await seed(db)
        service = GithubWorkProgressService(Observer(), type("C", (Client,), {"sha": SHA})())
    http, path, headers = await _leader_http(db, monkeypatch, item, scope, preset, service)
    async with http:
        response = await http.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert NONCE not in response.text and response.json()["dispatch_nonce"] is None
    assert response.json()["remaining_work_context"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["nonterminal", "replaced_head", "workspace_leased"])
async def test_the_route_refuses_a_terminal_context_whose_facts_changed_after_the_service_read(db, monkeypatch, late):
    item, scope, preset, workspace = await _released(db)
    item_id, workspace_id = item.id, workspace.id
    real = GithubWorkProgressService(Observer(), Client())

    class Racing:
        async def summary(self, session, row_id):
            result = await real.summary(session, row_id)
            assert result.remaining_work_context_basis == "recorded_head"
            if late == "nonterminal":
                await session.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(dispatch_status="dispatched"))
            elif late == "replaced_head":
                await session.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(last_verified_sha=REMOTE))
            else:
                await session.execute(update(GithubWorkspace).where(GithubWorkspace.id == workspace_id).values(
                    leased_item_id=item_id, lease_token="private-lease-value", leased_at=datetime.utcnow()))
            await session.commit()
            return result
    http, path, headers = await _leader_http(db, monkeypatch, item, scope, preset, Racing())
    async with http:
        response = await http.get(path, headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "progress_context_unavailable"


@pytest.mark.asyncio
async def test_a_published_report_that_contains_the_live_dispatch_nonce_is_refused(db):
    item, *_ = await seed(db)
    client = ReportClient(item)
    client.body = client.report.model_copy(update={"remaining": f"Review and merge {NONCE}."}).markdown()
    result = await GithubWorkProgressService(Observer(), client).summary(db, item.id)
    assert result.remaining_work.state != "current" and result.remaining_work.reason == "report_invalid"


# --- review 5923188149: a public generation that cannot repeat, and the route's final boundary -------------------

def _generation(item):
    from mcp_shim.work_remaining_protocol import public_generation
    return public_generation(item.id, item.dispatch_nonce)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["in_flight", "terminal"])
async def test_the_context_carries_a_full_digest_generation_and_never_the_nonce(db, state):
    if state == "terminal":
        item, *_ = await _released(db)
        client = Client()
    else:
        item, *_ = await seed(db)
        client = type("C", (Client,), {"sha": SHA})()
    context = (await GithubWorkProgressService(Observer(), client).summary(db, item.id)).remaining_work_context
    assert context is not None
    assert len(context.generation) == 64 and context.generation == _generation(item)
    assert NONCE not in context.model_dump_json() and "attempt" not in context.model_dump()


@pytest.mark.asyncio
async def test_a_real_reset_and_prepare_attempt_makes_the_old_block_stale_for_the_same_owner_and_head(db):
    from app.services.github_dispatch_service import github_dispatch_service
    from mcp_shim.work_remaining_protocol import report_reason
    item, scope, preset, workspace = await seed(db)
    item_id, owner_slot_id, workspace_id = item.id, item.owner_slot_id, workspace.id
    branch = {"ref": "task"}

    class FollowingObserver(Observer):
        async def read(self, context):
            return LocalProgressObservation(SHA, branch["ref"])

    class FollowingClient(Client):
        sha = SHA
        async def get_ref(self, *args, **kwargs):
            return {"ref": f"refs/heads/{branch['ref']}", "object": {"sha": SHA, "type": "commit"}}
    service = GithubWorkProgressService(FollowingObserver(), FollowingClient())
    first = (await service.summary(db, item_id)).remaining_work_context
    old_report = _report_from(first)
    assert report_reason(old_report, first, datetime.now(timezone.utc)) is None
    # the supported replacement path: release, reset (retry_count back to zero), prepare a new attempt, re-lease
    await db.execute(update(GithubWorkspace).where(GithubWorkspace.id == workspace_id).values(
        leased_item_id=None, lease_token=None))
    await db.commit()
    await db.refresh(item)
    await github_dispatch_service.reset_for_retry(db, item)
    await db.commit()
    await db.refresh(item)
    await github_dispatch_service.prepare_attempt(db, item, owner_slot_id=owner_slot_id, routing_method="label",
                                                  base_ref="origin/release/v4.2")
    await db.refresh(item)
    branch["ref"] = item.dispatch_head_ref   # the same owner works the same published commit on the new branch
    await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
        dispatch_status="dispatched", ack_received_at=datetime.utcnow(), retry_count=0))
    await db.execute(update(GithubWorkspace).where(GithubWorkspace.id == workspace_id).values(
        leased_item_id=item_id, lease_token="private-lease-value", leased_at=datetime.utcnow()))
    await db.commit()
    second = (await service.summary(db, item_id)).remaining_work_context
    assert second is not None and second.generation != first.generation
    assert (second.source_sha, second.owner_slot_id, second.scope_revision) == (
        first.source_sha, first.owner_slot_id, first.scope_revision)   # only the generation tells them apart
    assert report_reason(old_report, second, datetime.now(timezone.utc)) == "context_changed"


def test_old_counter_only_and_nonce_blocks_stay_invalid():
    from mcp_shim.work_remaining_protocol import parse_report
    good = RemainingReport(work_item_id=1, owner_slot_id=2, scope_revision=0, source_sha=SHA, phase="review",
        generation="a" * 64, reported_at=datetime.now(timezone.utc), reported_by="Team member 2",
        remaining="Review.", next_action="V1 reviews.")
    assert parse_report(good.markdown()) is not None
    block = good.markdown()
    counter = block.replace('"generation":"' + "a" * 64 + '"', '"attempt":0')
    nonce = block.replace('{"work_item_id"', '{"dispatch_nonce":"old-nonce","work_item_id"', 1)
    assert "attempt" in counter and parse_report(counter) is None
    assert "old-nonce" in nonce and parse_report(nonce) is None


@pytest_asyncio.fixture
async def fdb(monkeypatch, tmp_path):
    """A file-backed database with a second connection, so a change at the route's final boundary is a real one."""
    monkeypatch.setattr("app.services.github_work_progress_service.hold_code", lambda: None)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'progress.sqlite'}", hide_parameters=True,
                                 connect_args={"timeout": 5})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        session.maker = maker
        yield session
    await engine.dispose()


async def _leader_http(db, monkeypatch, item, scope, preset, service):
    """The authenticated Leader route over the same database: (client, path, headers)."""
    from app.config import settings
    from app.models.database import MailAgentSession, MailTeamMember
    from app.services.agent_mail_service import agent_mail_service
    monkeypatch.setattr(settings, "mail_capability_tokens_required", True)
    monkeypatch.setattr("app.utils.peer_process.pane_is_alive", lambda *_: True)
    owner_slot = await db.get(AgentTeamSlot, item.owner_slot_id)
    owner_slot.position = 1
    slot = AgentTeamSlot(preset_id=preset.id, position=0, display_name="Leader", role="Leader",
        provider="codex-cli", repo_id="repo", repo_path="/safe/leader", repo_name="repo")
    db.add(slot); await db.flush()
    preset.leader_slot_id = slot.id
    member = MailTeamMember(identity_key="leader", repo_id="repo", repo_path=slot.repo_path,
        repo_name="repo", display_name="Leader", participant_kind="team_slot", team_preset_id=preset.id, team_slot_id=slot.id)
    db.add(member); await db.flush()
    db.add(MailAgentSession(member_id=member.id, provider="codex-cli", source="mcp", session_key="leader",
        team_preset_id=preset.id, team_slot_id=slot.id,
        capability_token_hash=agent_mail_service.hash_capability_token("leader-token"),
        bound_pane_pid=1234, bound_pane_proc_start="1", wake_enabled=True))
    await db.commit()
    app = FastAPI(); app.include_router(api.router)
    async def session(): yield db
    app.dependency_overrides[get_db] = session
    monkeypatch.setattr(api, "github_work_progress_service", service)
    return (httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"),
            f"/github-work-items/{item.id}/remaining-work-context", {"X-Synth-City-Session-Token": "leader-token"})


_BOUNDARY_CHANGES = {
    "nonterminal": dict(dispatch_status="dispatched"),
    "replaced_head": dict(last_verified_sha=REMOTE),
    "changed_nonce": dict(dispatch_nonce="changed-dispatch-id"),
    "removed_owner": dict(owner_slot_id=None),
    "changed_revision": dict(active_scope_revision=3),
    "other_status": dict(dispatch_status="closed"),
    "reset_generation": dict(dispatch_nonce="replacement-dispatch-id", retry_count=0),
    "workspace_leased": None,   # the lease lands on the workspace row, not the item
}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", sorted(_BOUNDARY_CHANGES))
async def test_the_route_refuses_what_another_connection_changes_during_its_final_leader_check(fdb, monkeypatch, change):
    from app.services.github_coordination_service import github_coordination_service as coordination
    item, scope, preset, workspace = await _released(fdb)
    item_id, workspace_id = item.id, workspace.id
    http, path, headers = await _leader_http(fdb, monkeypatch, item, scope, preset,
                                             GithubWorkProgressService(Observer(), Client()))
    real, calls = coordination.require_leader, {"n": 0}

    async def require_leader(session, scope_id, principal):
        result = await real(session, scope_id, principal)
        calls["n"] += 1
        if calls["n"] == 2:   # the route's FINAL Leader check: a second real connection commits the change here
            async with fdb.maker() as other:
                if change == "workspace_leased":
                    await other.execute(update(GithubWorkspace).where(GithubWorkspace.id == workspace_id).values(
                        leased_item_id=item_id, lease_token="private-lease-value", leased_at=datetime.utcnow()))
                else:
                    await other.execute(update(GithubWorkItem).where(GithubWorkItem.id == item_id).values(
                        **_BOUNDARY_CHANGES[change]))
                await other.commit()
        return result
    monkeypatch.setattr(coordination, "require_leader", require_leader)
    async with http:
        response = await http.get(path, headers=headers)
    assert response.status_code == 409, (change, response.status_code, response.text[:160])
    assert response.json()["detail"] in {"progress_context_unavailable", "progress_context_changed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["in_flight", "terminal"])
async def test_the_route_response_carries_neither_the_nonce_nor_a_stale_status(fdb, monkeypatch, state):
    if state == "terminal":
        item, scope, preset, workspace = await _released(fdb)
        service = GithubWorkProgressService(Observer(), Client())
    else:
        item, scope, preset, workspace = await seed(fdb)
        service = GithubWorkProgressService(Observer(), type("C", (Client,), {"sha": SHA})())
    http, path, headers = await _leader_http(fdb, monkeypatch, item, scope, preset, service)
    async with http:
        response = await http.get(path, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert NONCE not in response.text and body["dispatch_nonce"] is None
    assert body["remaining_work_context_status"] == ("merged" if state == "terminal" else "dispatched")

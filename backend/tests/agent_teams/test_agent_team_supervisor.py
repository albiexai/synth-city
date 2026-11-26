"""An optional supervisor promises its next check without gaining authority."""
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.database import AgentTeamPreset, MailExternalActor


@pytest_asyncio.fixture
async def supervisor_client(db, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "synthetic-director-token")

    async def database():
        yield db

    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = database
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous


async def supervisor_team(db):
    team = AgentTeamPreset(name="Supervised", description="Optional supervision.")
    actor = MailExternalActor(actor_key="supervisor-fixture", display_name="Supervisor",
                              kind="external_tool", token_hash=hashlib.sha256(b"synthetic-actor-token").hexdigest())
    db.add_all([team, actor])
    await db.commit()
    return team, actor


@pytest.mark.asyncio
async def test_director_can_bind_supervisor_without_starting_automation(db, supervisor_client):
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    response = await supervisor_client.put(f"/api/v1/agent-teams/presets/{team_id}/supervisor",
        headers={"X-Synth-City-Operator-Token": "synthetic-director-token"}, json={"actor_id": actor_id})
    assert response.status_code == 200
    assert response.json()["actor_id"] == actor_id
    assert response.json()["status"] == "absent"
    await db.refresh(team)
    assert team.supervisor_actor_id == actor_id
    assert not team.autonomy_enabled


@pytest.mark.parametrize("elapsed,status", [(-1, "healthy"), (0, "late"), (1799, "late"), (1800, "absent")])
def test_supervisor_status_uses_clock_boundaries(elapsed, status):
    from app.services.agent_team_supervisor_service import supervisor_observation
    promised = datetime(2026, 10, 13, tzinfo=timezone.utc)
    team = SimpleNamespace(supervisor_actor_id=7, supervisor_checked_at=promised - timedelta(minutes=10),
        supervisor_next_check_at=promised, supervisor_grace_seconds=None, supervisor_note="Checked.")
    result = supervisor_observation(team, now=promised + timedelta(seconds=elapsed))
    assert result["status"] == status
    assert result["grace_seconds"] == 1800
    assert result["attention"] == ("Supervisor check-in overdue" if status in {"late", "absent"} else None)


def test_supervisor_status_none_is_optional_and_default_grace_uses_last_interval():
    from app.services.agent_team_supervisor_service import supervisor_observation
    promised = datetime(2026, 10, 13)
    team = SimpleNamespace(supervisor_actor_id=None, supervisor_checked_at=None,
        supervisor_next_check_at=None, supervisor_grace_seconds=None, supervisor_note=None)
    assert supervisor_observation(team, now=promised)["status"] == "none"
    team.supervisor_actor_id = 7
    assert supervisor_observation(team, now=promised)["status"] == "absent"
    team.supervisor_checked_at = promised - timedelta(hours=1)
    team.supervisor_next_check_at = promised
    assert supervisor_observation(team, now=promised)["grace_seconds"] == 7200
    team.supervisor_grace_seconds = 120
    assert supervisor_observation(team, now=promised + timedelta(seconds=120))["status"] == "absent"


@pytest.mark.parametrize("seconds", [-1, 0, 86401])
def test_supervisor_refuses_invalid_promises(seconds):
    from app.services.agent_team_supervisor_service import validate_promise
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="supervisor_next_check_invalid"):
        validate_promise(now + timedelta(seconds=seconds), now=now)


@pytest.mark.asyncio
async def test_supervisor_checkin_requires_the_bound_external_actor(db, supervisor_client):
    team, actor = await supervisor_team(db)
    path = "/api/v1/external/agent-mail/supervisor-checkin"
    payload = {"preset_id": team.id, "next_check_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
    assert (await supervisor_client.post(path, json=payload)).status_code == 401
    assert (await supervisor_client.post(path, json=payload,
        headers={"X-Synth-City-Session-Token": "synthetic-member-token"})).status_code == 401
    auth = {"Authorization": "Bearer synthetic-actor-token"}
    assert (await supervisor_client.post(path, json=payload, headers=auth)).status_code == 403
    bind = f"/api/v1/agent-teams/presets/{team.id}/supervisor"
    assert (await supervisor_client.put(bind, json={"actor_id": actor.id}, headers=auth)).status_code == 401
    await supervisor_client.put(bind, json={"actor_id": actor.id},
        headers={"X-Synth-City-Operator-Token": "synthetic-director-token"})
    response = await supervisor_client.post(path, json={**payload, "note": "token=private-note\nChecked."}, headers=auth)
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert "private-note" not in response.text
    assert response.json()["address"]["actor_id"] == actor.id
    assert (await supervisor_client.post(path, json={**payload, "status": "healthy"}, headers=auth)).status_code == 422
    other = MailExternalActor(actor_key="foreign-fixture", display_name="Foreign", kind="external_tool",
        token_hash=hashlib.sha256(b"synthetic-foreign-token").hexdigest())
    db.add(other); await db.commit()
    assert (await supervisor_client.post(path, json=payload,
        headers={"Authorization": "Bearer synthetic-foreign-token"})).status_code == 403


@pytest.mark.asyncio
async def test_supervisor_checkins_audit_changes_without_audit_noise(db):
    from sqlalchemy import select
    from app.models.database import FactoryAuditEvent
    from app.services.agent_team_supervisor_service import bind_supervisor, observe_supervisor, supervisor_checkin
    team, actor = await supervisor_team(db)
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    await bind_supervisor(db, team.id, actor_id=actor.id, grace_seconds=None, set_grace=False, now=now)
    await supervisor_checkin(db, actor, team.id, now + timedelta(minutes=10), now=now)
    await supervisor_checkin(db, actor, team.id, now + timedelta(minutes=11), now=now + timedelta(minutes=1))
    await observe_supervisor(db, team.id, now=now + timedelta(minutes=11))
    await observe_supervisor(db, team.id, now=now + timedelta(minutes=11))
    await observe_supervisor(db, team.id, now=now + timedelta(minutes=41))
    events = (await db.scalars(select(FactoryAuditEvent).order_by(FactoryAuditEvent.id))).all()
    assert len(events) == 4
    assert [event.actor_kind for event in events] == ["operator", "system", "system", "system"]
    assert [event.after_values.get("supervisor_status") for event in events[1:]] == ["healthy", "late", "absent"]
    assert events[0].after_values["supervisor_actor_id"] == actor.id
    await bind_supervisor(db, team.id, actor_id=None, grace_seconds=None, set_grace=False, now=now)
    await db.refresh(team)
    assert team.supervisor_checked_at is None and team.supervisor_next_check_at is None
    assert team.supervisor_note is None and team.supervisor_observed_status == "none"
    assert not team.autonomy_enabled


@pytest.mark.asyncio
async def test_supervisor_audit_failure_rolls_back_binding(db, monkeypatch):
    from app.services.agent_team_supervisor_service import bind_supervisor
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    async def fail(*args, **kwargs):
        raise RuntimeError("Synthetic audit failure")
    monkeypatch.setattr("app.services.agent_team_supervisor_service.audit.record_event", fail)
    with pytest.raises(RuntimeError, match="Synthetic audit failure"):
        await bind_supervisor(db, team_id, actor_id=actor_id, grace_seconds=None, set_grace=False)
    await db.refresh(team)
    assert team.supervisor_actor_id is None


async def supervisor_member(db, team):
    from app.models.database import AgentTeamSlot, MailAgentSession, MailTeamMember
    slot = AgentTeamSlot(preset_id=team.id, position=0, display_name="Member", provider="codex-cli",
        repo_id="supervisor-repo", repo_path="/tmp/supervisor-repo", repo_name="supervisor-repo")
    db.add(slot); await db.flush()
    member = MailTeamMember(identity_key=f"supervisor-member:{team.id}", repo_id=slot.repo_id,
        repo_path=slot.repo_path, repo_name=slot.repo_name, display_name="Member",
        team_preset_id=team.id, team_slot_id=slot.id)
    db.add(member); await db.flush()
    session = MailAgentSession(member_id=member.id, provider="codex-cli", source="mcp",
        session_key=f"supervisor-session:{team.id}", team_preset_id=team.id, team_slot_id=slot.id,
        capability_token_hash=hashlib.sha256(b"synthetic-member-token").hexdigest())
    db.add(session); await db.commit()
    return member, session


@pytest.mark.asyncio
async def test_team_member_can_address_only_the_current_supervisor(db, supervisor_client, monkeypatch):
    from app.services.agent_team_supervisor_service import bind_supervisor
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    _, session = await supervisor_member(db, team)
    session.bound_pane_pid = 321
    session.bound_pane_proc_start = "test-start"
    session.mailbox_status = "connected"
    await db.commit()
    monkeypatch.setattr("app.utils.peer_process.pane_is_alive", lambda *_: True)
    path = "/api/v1/agent-mail/supervisor/messages"
    auth = {"X-Synth-City-Session-Token": "synthetic-member-token"}
    message = {"actor_id": actor_id, "body_markdown": "Please inspect the incident."}
    assert (await supervisor_client.post(path, headers=auth, json=message)).status_code == 403
    await bind_supervisor(db, team_id, actor_id=actor_id, grace_seconds=None, set_grace=False)
    response = await supervisor_client.post(path, headers=auth, json=message)
    assert response.status_code == 200
    assert response.json()["sender_type"] == "member"
    assert response.json()["payload"]["supervisor_actor_id"] == actor_id
    inbox_path = "/api/v1/external/agent-mail/supervisor-inbox"
    inbox = await supervisor_client.get(inbox_path, headers={"Authorization": "Bearer synthetic-actor-token"})
    assert inbox.status_code == 200
    assert [entry["id"] for entry in inbox.json()] == [response.json()["id"]]
    thread_id = response.json()["id"]
    actor_auth = {"Authorization": "Bearer synthetic-actor-token"}
    thread_path = f"/api/v1/external/agent-mail/threads/{thread_id}"
    assert (await supervisor_client.get(thread_path, headers=actor_auth)).status_code == 200
    reply = await supervisor_client.post(thread_path + "/replies", headers=actor_auth,
        json={"body_markdown": "I will inspect the incident."})
    assert reply.status_code == 200
    foreign = MailExternalActor(actor_key="foreign-message-fixture", display_name="Foreign", kind="external_tool",
        token_hash=hashlib.sha256(b"synthetic-foreign-token").hexdigest())
    db.add(foreign); await db.commit()
    foreign_auth = {"Authorization": "Bearer synthetic-foreign-token"}
    assert (await supervisor_client.get(thread_path, headers=foreign_auth)).status_code == 403
    assert (await supervisor_client.post(thread_path + "/replies", headers=foreign_auth,
        json={"body_markdown": "Foreign message."})).status_code == 403
    assert (await supervisor_client.post(path, headers=auth, json={**message, "actor_id": actor_id + 100})).status_code == 403
    await bind_supervisor(db, team_id, actor_id=None, grace_seconds=None, set_grace=False)
    assert (await supervisor_client.get(inbox_path, headers={"Authorization": "Bearer synthetic-actor-token"})).status_code == 403
    assert (await supervisor_client.get(thread_path, headers=actor_auth)).status_code == 403


@pytest.mark.asyncio
async def test_member_identity_has_safe_supervisor_address_and_rebind_clears_old_address(db, monkeypatch):
    from app.services.agent_mail_service import agent_mail_service
    from app.services.agent_team_supervisor_service import bind_supervisor
    monkeypatch.setattr("app.services.agent_mail_service.discover_agent_sessions", lambda: [])
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    member, _ = await supervisor_member(db, team)
    member_id = member.id
    await bind_supervisor(db, team_id, actor_id=actor_id, grace_seconds=60, set_grace=True)
    me = next(value for value in await agent_mail_service.list_team(db) if value.id == member_id)
    assert me.supervisor_address.actor_id == actor_id
    assert me.supervisor_address.address == f"external_actor:{actor_id}"
    assert "synthetic-actor-token" not in me.model_dump_json()
    await bind_supervisor(db, team_id, actor_id=None, grace_seconds=None, set_grace=False)
    me = next(value for value in await agent_mail_service.list_team(db) if value.id == member_id)
    assert me.supervisor_address is None


@pytest.mark.asyncio
async def test_actor_can_supervise_multiple_teams_and_grace_is_operator_controlled(db, supervisor_client):
    from app.services.agent_team_supervisor_service import bind_supervisor, supervisor_checkin
    team, actor = await supervisor_team(db)
    second = AgentTeamPreset(name="Second supervised team")
    db.add(second); await db.commit()
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    for team_id in (team.id, second.id):
        await bind_supervisor(db, team_id, actor_id=actor.id, grace_seconds=60, set_grace=True, now=now)
        result = await supervisor_checkin(db, actor, team_id, now + timedelta(hours=24), now=now)
        assert result["status"] == "healthy" and result["grace_seconds"] == 60
    path = f"/api/v1/agent-teams/presets/{second.id}/supervisor"
    assert (await supervisor_client.put(path, json={"actor_id": actor.id, "grace_seconds": 0},
        headers={"X-Synth-City-Operator-Token": "synthetic-director-token"})).status_code == 422


def test_dispatch_brief_names_supervisor_without_changing_the_escalation_order():
    from app.models.database import GithubWorkItem, TeamGithubScope
    from app.services.github_dispatch_service import github_dispatch_service
    item = GithubWorkItem(id=7, issue_number=549, issue_title="Supervisor", issue_url="https://github.com/example/repo/issues/549",
        issue_type="autonomous", dispatch_base_ref="origin/release/v3.1", dispatch_head_ref="example-supervisor",
        dispatch_nonce="synthetic-nonce", delivery_policy={}, delivery_policy_revision=1)
    scope = TeamGithubScope(repo_owner="example", repo_name="repo", github_auth_mode="token", preset_id=1,
                            max_build_parallelism=2, builds_out_of_tree=False)
    workspace = SimpleNamespace(path="/tmp/example-supervisor", kind="worktree", lease_token="synthetic-lease")
    brief = github_dispatch_service._dispatch_brief(item, scope, workspace, owner_slot_id=1, preset_slots=[],
        supervisor={"actor_id": 9, "address": "external_actor:9", "display_name": "Supervisor"})
    assert "Supervisor address: external_actor:9 (actor_id=9)" in brief
    assert "Escalate to the Leader first. Then contact the Supervisor." in brief
    assert "Retained operator decisions require the current operator-action record." in brief


@pytest.mark.asyncio
async def test_supervisor_message_refuses_membership_changed_before_insert(db, monkeypatch):
    from sqlalchemy import select, update
    from sqlalchemy.sql.dml import Insert
    from app.models.database import MailMessage, MailTeamMember
    from app.models.schemas import SupervisorMessageRequest
    from app.services.agent_team_supervisor_service import bind_supervisor, send_supervisor_message
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    member, session = await supervisor_member(db, team)
    member_id = member.id
    await bind_supervisor(db, team_id, actor_id=actor_id, grace_seconds=None, set_grace=False)
    execute = db.execute
    changed = False
    async def concurrent_membership(statement, *args, **kwargs):
        nonlocal changed
        if isinstance(statement, Insert) and statement.table.name == "mail_messages" and not changed:
            changed = True
            await execute(update(MailTeamMember).where(MailTeamMember.id == member_id).values(team_preset_id=None))
            await db.commit()
        return await execute(statement, *args, **kwargs)
    monkeypatch.setattr(db, "execute", concurrent_membership)
    with pytest.raises(PermissionError, match="supervisor_address_not_bound"):
        await send_supervisor_message(db, session, SupervisorMessageRequest(actor_id=actor_id, body_markdown="Incident."))
    assert (await db.scalars(select(MailMessage))).all() == []


# #1298: a durable binding version makes an edit apply only to the binding that the operator reviewed.
DIRECTOR = {"X-Synth-City-Operator-Token": "synthetic-director-token"}


def supervisor_path(team_id: int) -> str:
    return f"/api/v1/agent-teams/presets/{team_id}/supervisor"


async def put_supervisor(client, team_id: int, body: dict):
    return await client.put(supervisor_path(team_id), headers=DIRECTOR, json=body)


async def read_supervisor(client, team_id: int) -> dict:
    return (await client.get(supervisor_path(team_id), headers=DIRECTOR)).json()


@pytest.mark.asyncio
async def test_binding_version_starts_at_zero_and_each_write_adds_one(db, supervisor_client):
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    assert (await read_supervisor(supervisor_client, team_id))["binding_version"] == 0
    first = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id})
    assert first.status_code == 200 and first.json()["binding_version"] == 1
    second = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 120})
    assert second.json()["binding_version"] == 2
    removed = await put_supervisor(supervisor_client, team_id, {"actor_id": None})
    assert removed.json()["binding_version"] == 3


@pytest.mark.asyncio
async def test_summary_reports_the_binding_version(db, supervisor_client):
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id})
    summary = await supervisor_client.get(f"/api/v1/agent-teams/presets/{team_id}/summary")
    assert summary.status_code == 200
    assert summary.json()["supervisor_observation"]["binding_version"] == 1


@pytest.mark.asyncio
async def test_matching_expected_version_is_accepted(db, supervisor_client):
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    response = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "expected_binding_version": 0})
    assert response.status_code == 200 and response.json()["binding_version"] == 1


@pytest.mark.asyncio
async def test_stale_expected_version_is_refused_and_changes_nothing(db, supervisor_client):
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 120})
    response = await put_supervisor(supervisor_client, team_id,
                                    {"actor_id": actor_id, "grace_seconds": 999, "expected_binding_version": 0})
    assert response.status_code == 409 and response.json()["detail"] == "supervisor_binding_changed"
    now = await read_supervisor(supervisor_client, team_id)
    assert (now["actor_id"], now["grace_seconds"], now["binding_version"]) == (actor_id, 120, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, -1, "x"])
async def test_expected_version_must_be_a_whole_number_of_zero_or_more(db, supervisor_client, value):
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    response = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "expected_binding_version": value})
    assert response.status_code == 422
    assert (await read_supervisor(supervisor_client, team_id))["binding_version"] == 0


@pytest.mark.asyncio
async def test_old_removal_cannot_remove_a_binding_of_the_same_actor_and_grace(db, supervisor_client):
    """Remove, then bind the same actor with the same grace: the state looks the same, the version does not."""
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    bound = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 60})
    reviewed = bound.json()["binding_version"]
    assert (await put_supervisor(supervisor_client, team_id, {"actor_id": None})).status_code == 200
    rebound = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 60})
    assert rebound.json()["actor_id"] == actor_id and rebound.json()["grace_seconds"] == 60
    stale = await put_supervisor(supervisor_client, team_id, {"actor_id": None, "expected_binding_version": reviewed})
    assert stale.status_code == 409
    now = await read_supervisor(supervisor_client, team_id)
    assert (now["actor_id"], now["grace_seconds"]) == (actor_id, 60)
    assert now["binding_version"] == rebound.json()["binding_version"] == 3


@pytest.mark.asyncio
async def test_two_writes_with_one_version_let_only_the_first_win(db, supervisor_client):
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    version = (await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id})).json()["binding_version"]
    first = await put_supervisor(supervisor_client, team_id,
                                 {"actor_id": actor_id, "grace_seconds": 300, "expected_binding_version": version})
    second = await put_supervisor(supervisor_client, team_id,
                                  {"actor_id": actor_id, "grace_seconds": 700, "expected_binding_version": version})
    assert first.status_code == 200 and second.status_code == 409
    assert (await read_supervisor(supervisor_client, team_id))["grace_seconds"] == 300


@pytest.mark.asyncio
async def test_a_check_in_does_not_change_the_binding_version(db, supervisor_client):
    from app.services.agent_team_supervisor_service import supervisor_checkin
    team, actor = await supervisor_team(db)
    team_id = team.id
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor.id})
    before = (await read_supervisor(supervisor_client, team_id))["binding_version"]
    result = await supervisor_checkin(db, actor, team_id, now + timedelta(hours=1), now=now)
    assert result["binding_version"] == before == 1
    assert (await read_supervisor(supervisor_client, team_id))["binding_version"] == before


@pytest.mark.asyncio
async def test_omitted_grace_is_unchanged_and_explicit_null_returns_to_the_default(db, supervisor_client):
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 120})
    omitted = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "expected_binding_version": 1})
    assert omitted.json()["grace_seconds"] == 120
    reset = await put_supervisor(supervisor_client, team_id,
                                 {"actor_id": actor_id, "grace_seconds": None, "expected_binding_version": 2})
    assert reset.json()["grace_seconds"] == 1800


@pytest.mark.asyncio
async def test_the_version_check_and_the_write_are_one_atomic_step(db, supervisor_client, monkeypatch):
    """A binding write that lands between the read and the update of a request must make that update match no row."""
    from sqlalchemy import update as sql_update
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    version = (await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 60})).json()["binding_version"]
    execute = db.execute
    raced = False

    async def racing(statement, *args, **kwargs):
        nonlocal raced
        if not raced and getattr(statement, "is_update", False):
            raced = True
            # Another client removed and rebound the same actor with the same grace: only the version differs.
            await execute(sql_update(AgentTeamPreset).where(AgentTeamPreset.id == team_id)
                          .values(supervisor_binding_version=AgentTeamPreset.supervisor_binding_version + 2))
        return await execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", racing)
    response = await put_supervisor(supervisor_client, team_id, {"actor_id": None, "expected_binding_version": version})
    assert raced
    assert response.status_code == 409 and response.json()["detail"] == "supervisor_binding_changed"
    monkeypatch.setattr(db, "execute", execute)
    now = await read_supervisor(supervisor_client, team_id)
    assert (now["actor_id"], now["grace_seconds"]) == (actor_id, 60)


@pytest.mark.asyncio
async def test_a_row_without_a_version_counts_as_version_zero_and_can_be_written(db, supervisor_client):
    """A raw insert that omits the column gets 0 from the database. An explicit NULL is also read as 0."""
    from sqlalchemy import text
    team, actor = await supervisor_team(db)
    team_id, actor_id = team.id, actor.id
    await db.execute(text("UPDATE agent_team_presets SET supervisor_binding_version = NULL WHERE id = :id"), {"id": team_id})
    await db.commit()
    assert (await read_supervisor(supervisor_client, team_id))["binding_version"] == 0
    response = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "expected_binding_version": 0})
    assert response.status_code == 200 and response.json()["binding_version"] == 1
    inserted = await db.execute(text("INSERT INTO agent_team_presets (name, created_at, updated_at, autonomy_enabled) "
                                     "VALUES ('Raw', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)"))
    await db.commit()
    assert (await read_supervisor(supervisor_client, inserted.lastrowid))["binding_version"] == 0

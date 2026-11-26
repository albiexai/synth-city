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


# #1298: a binding version and the team context key make an edit apply only to the binding that the operator reviewed.
DIRECTOR = {"X-Synth-City-Operator-Token": "synthetic-director-token"}


def supervisor_path(team_id: int) -> str:
    return f"/api/v1/agent-teams/presets/{team_id}/supervisor"


async def put_supervisor(client, team_id: int, body: dict):
    return await client.put(supervisor_path(team_id), headers=DIRECTOR, json=body)


async def read_supervisor(client, team_id: int) -> dict:
    return (await client.get(supervisor_path(team_id), headers=DIRECTOR)).json()


async def reviewed(client, team_id: int) -> dict:
    """What a dialog sends: the version and the lifetime of the binding that the operator saw."""
    now = await read_supervisor(client, team_id)
    return {"expected_binding_version": now["binding_version"], "expected_team_context_key": now["team_context_key"]}


async def keyed_team(db):
    """An ORM fixture team plus an active context key, as the create path gives it."""
    from app.services.factory_audit_service import context_key_for
    team, actor = await supervisor_team(db)
    await context_key_for(db, "team", team.id)
    await db.commit()
    return team, actor


async def make_actor(db, key: str) -> int:
    actor = MailExternalActor(actor_key=key, display_name=key, kind="external_tool",
                              token_hash=hashlib.sha256(key.encode()).hexdigest())
    db.add(actor)
    await db.commit()
    return actor.id


async def make_team(client, name: str) -> int:
    return (await client.post("/api/v1/agent-teams/presets", headers=DIRECTOR, json={"name": name})).json()["id"]


async def raw_team(db, name: str = "Raw", extra: str = "", value: str = "") -> int:
    from sqlalchemy import text
    result = await db.execute(text(f"INSERT INTO agent_team_presets (name, created_at, updated_at, autonomy_enabled{extra}) "
                                   f"VALUES ('{name}', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0{value})"))
    await db.commit()
    return result.lastrowid


@pytest.mark.asyncio
async def test_version_starts_at_zero_the_team_has_a_key_and_each_write_adds_one_to_the_version(db, supervisor_client):
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    start = await read_supervisor(supervisor_client, team_id)
    assert start["binding_version"] == 0 and start["team_context_key"]
    first = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id})
    assert first.status_code == 200 and first.json()["binding_version"] == 1
    assert (await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 120})).json()["binding_version"] == 2
    removed = await put_supervisor(supervisor_client, team_id, {"actor_id": None})
    assert removed.json()["binding_version"] == 3 and removed.json()["team_context_key"] == start["team_context_key"]


@pytest.mark.asyncio
async def test_summary_reports_the_version_and_the_lifetime(db, supervisor_client):
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id})
    summary = (await supervisor_client.get(f"/api/v1/agent-teams/presets/{team_id}/summary")).json()["supervisor_observation"]
    assert summary["binding_version"] == 1 and summary["team_context_key"] == (await read_supervisor(supervisor_client, team_id))["team_context_key"]


@pytest.mark.asyncio
async def test_a_matching_pair_is_accepted_and_neither_field_keeps_the_legacy_behaviour(db, supervisor_client):
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    accepted = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, **await reviewed(supervisor_client, team_id)})
    assert accepted.status_code == 200 and accepted.json()["binding_version"] == 1
    legacy = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 90})
    assert legacy.status_code == 200 and legacy.json()["grace_seconds"] == 90


@pytest.mark.asyncio
async def test_a_stale_version_or_a_wrong_key_is_refused_and_changes_nothing(db, supervisor_client):
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    old = await reviewed(supervisor_client, team_id)
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 120})
    stale = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 999, **old})
    assert stale.status_code == 409 and stale.json()["detail"] == "supervisor_binding_changed"
    now = await read_supervisor(supervisor_client, team_id)
    wrong = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 999,
        "expected_binding_version": now["binding_version"], "expected_team_context_key": now["team_context_key"] + "x"})
    assert wrong.status_code == 409
    after = await read_supervisor(supervisor_client, team_id)
    assert (after["actor_id"], after["grace_seconds"], after["binding_version"]) == (actor_id, 120, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"expected_binding_version": 0}, {"expected_team_context_key": "team:1:k"},
    {"expected_binding_version": None, "expected_team_context_key": "team:1:k"}, {"expected_binding_version": 0, "expected_team_context_key": None},
    {"expected_binding_version": -1, "expected_team_context_key": "team:1:k"}, {"expected_binding_version": 0, "expected_team_context_key": ""},
    {"expected_binding_version": "x", "expected_team_context_key": "team:1:k"}, {"expected_binding_version": 0, "expected_team_context_key": 5},
])
async def test_the_two_expected_fields_come_together_as_a_whole_number_and_a_key(db, supervisor_client, body):
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    response = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, **body})
    assert response.status_code == 422
    assert (await read_supervisor(supervisor_client, team_id))["binding_version"] == 0


@pytest.mark.asyncio
async def test_old_removal_cannot_remove_a_binding_of_the_same_actor_and_grace(db, supervisor_client):
    """Remove, then bind the same actor with the same grace: the state looks the same, the version does not."""
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 60})
    old = await reviewed(supervisor_client, team_id)
    assert (await put_supervisor(supervisor_client, team_id, {"actor_id": None})).status_code == 200
    rebound = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 60})
    assert rebound.json()["actor_id"] == actor_id and rebound.json()["grace_seconds"] == 60
    stale = await put_supervisor(supervisor_client, team_id, {"actor_id": None, **old})
    assert stale.status_code == 409
    now = await read_supervisor(supervisor_client, team_id)
    assert (now["actor_id"], now["grace_seconds"], now["binding_version"]) == (actor_id, 60, 3)


@pytest.mark.asyncio
async def test_two_writes_with_one_reviewed_pair_let_only_the_first_win(db, supervisor_client):
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id})
    pair = await reviewed(supervisor_client, team_id)
    first = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 300, **pair})
    second = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 700, **pair})
    assert first.status_code == 200 and second.status_code == 409
    assert (await read_supervisor(supervisor_client, team_id))["grace_seconds"] == 300


@pytest.mark.asyncio
async def test_a_check_in_does_not_change_the_version_or_the_lifetime(db, supervisor_client):
    from app.services.agent_team_supervisor_service import supervisor_checkin
    team, actor = await keyed_team(db)
    team_id = team.id
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor.id})
    before = await reviewed(supervisor_client, team_id)
    result = await supervisor_checkin(db, actor, team_id, now + timedelta(hours=1), now=now)
    assert (result["binding_version"], result["team_context_key"]) == (before["expected_binding_version"], before["expected_team_context_key"])
    assert await reviewed(supervisor_client, team_id) == before


@pytest.mark.asyncio
async def test_omitted_grace_is_unchanged_and_explicit_null_returns_to_the_default(db, supervisor_client):
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 120})
    omitted = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, **await reviewed(supervisor_client, team_id)})
    assert omitted.json()["grace_seconds"] == 120
    reset = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": None, **await reviewed(supervisor_client, team_id)})
    assert reset.json()["grace_seconds"] == 1800


@pytest.mark.asyncio
@pytest.mark.parametrize("raced", ["supervisor_binding_version", "key_retired"])
async def test_the_checks_and_the_write_are_one_atomic_step(db, supervisor_client, monkeypatch, raced):
    """A change that lands between the read and the update of a request makes that update match no row."""
    from sqlalchemy import update as sql_update
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "grace_seconds": 60})
    pair = await reviewed(supervisor_client, team_id)
    execute = db.execute
    fired = False

    async def racing(statement, *args, **kwargs):
        nonlocal fired
        if not fired and getattr(statement, "is_update", False):
            fired = True
            if raced == "key_retired":
                from sqlalchemy import text
                await execute(text("UPDATE factory_context_keys SET retired_at = CURRENT_TIMESTAMP "
                                   "WHERE key_kind = 'team' AND numeric_id = :id AND retired_at IS NULL"), {"id": team_id})
            else:
                await execute(sql_update(AgentTeamPreset).where(AgentTeamPreset.id == team_id)
                              .values(supervisor_binding_version=AgentTeamPreset.supervisor_binding_version + 1000))
        return await execute(statement, *args, **kwargs)

    monkeypatch.setattr(db, "execute", racing)
    response = await put_supervisor(supervisor_client, team_id, {"actor_id": None, **pair})
    assert fired
    assert response.status_code == 409 and response.json()["detail"] == "supervisor_binding_changed"
    monkeypatch.setattr(db, "execute", execute)
    now = await read_supervisor(supervisor_client, team_id)
    assert (now["actor_id"], now["grace_seconds"]) == (actor_id, 60)


@pytest.mark.asyncio
async def test_the_database_gives_a_raw_insert_version_zero_and_refuses_an_explicit_null(db, supervisor_client):
    """The version column is NOT NULL with DEFAULT 0: a raw insert that omits it reads as 0, and a NULL cannot be stored."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    team, _actor = await supervisor_team(db)
    team_id = team.id
    assert (await read_supervisor(supervisor_client, await raw_team(db)))["binding_version"] == 0
    with pytest.raises(IntegrityError):
        await db.execute(text("UPDATE agent_team_presets SET supervisor_binding_version = NULL WHERE id = :id"), {"id": team_id})
        await db.commit()
    await db.rollback()


@pytest.mark.asyncio
async def test_a_stale_removal_from_a_deleted_team_cannot_clear_the_team_that_reuses_its_id(db, supervisor_client):
    """V1, #1356: SQLite reuses a numeric team id, so the lifetime must tell the team lifetimes apart."""
    actor_id = await make_actor(db, "lifetime-a")
    first_id = await make_team(supervisor_client, "First lifetime")
    await put_supervisor(supervisor_client, first_id, {"actor_id": actor_id, "grace_seconds": 60})
    old = await reviewed(supervisor_client, first_id)
    assert (await supervisor_client.delete(f"/api/v1/agent-teams/presets/{first_id}", headers=DIRECTOR)).status_code == 204
    second_id = await make_team(supervisor_client, "Second lifetime")
    assert second_id == first_id, "the test needs SQLite to reuse the numeric team id"
    await put_supervisor(supervisor_client, second_id, {"actor_id": actor_id, "grace_seconds": 60})
    now = await reviewed(supervisor_client, second_id)
    assert now["expected_binding_version"] == old["expected_binding_version"], "same version in both lifetimes"
    assert now["expected_team_context_key"] != old["expected_team_context_key"]
    stale = await put_supervisor(supervisor_client, second_id, {"actor_id": None, **old})
    assert stale.status_code == 409 and stale.json()["detail"] == "supervisor_binding_changed"
    after = await read_supervisor(supervisor_client, second_id)
    assert (after["actor_id"], after["grace_seconds"]) == (actor_id, 60)


@pytest.mark.asyncio
async def test_a_stale_bind_from_a_deleted_unbound_team_cannot_bind_the_team_that_reuses_its_id(db, supervisor_client):
    actor_id = await make_actor(db, "lifetime-b")
    first_id = await make_team(supervisor_client, "First")
    old = await reviewed(supervisor_client, first_id)
    assert (await supervisor_client.delete(f"/api/v1/agent-teams/presets/{first_id}", headers=DIRECTOR)).status_code == 204
    second_id = await make_team(supervisor_client, "Second")
    assert second_id == first_id
    assert (await reviewed(supervisor_client, second_id))["expected_binding_version"] == old["expected_binding_version"]
    stale = await put_supervisor(supervisor_client, second_id, {"actor_id": actor_id, **old})
    assert stale.status_code == 409
    assert (await read_supervisor(supervisor_client, second_id))["actor_id"] is None


@pytest.mark.asyncio
async def test_a_team_without_a_key_shows_null_and_refuses_a_protected_write(db, supervisor_client):
    team, actor = await supervisor_team(db)  # an ORM fixture: no create path, no event, so no key
    team_id, actor_id = team.id, actor.id
    assert (await read_supervisor(supervisor_client, team_id))["team_context_key"] is None
    refused = await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id, "expected_binding_version": 0,
                                                                "expected_team_context_key": "team:1:made-up"})
    assert refused.status_code == 409
    assert (await read_supervisor(supervisor_client, team_id))["actor_id"] is None


@pytest.mark.asyncio
async def test_the_create_and_duplicate_paths_give_each_team_its_own_active_key(db, supervisor_client):
    created = await make_team(supervisor_client, "Api")
    copy = (await supervisor_client.post(f"/api/v1/agent-teams/presets/{created}/duplicate", headers=DIRECTOR, json={"name": "Copy"})).json()["id"]
    keys = [(await read_supervisor(supervisor_client, team_id))["team_context_key"] for team_id in (created, copy)]
    assert all(keys) and keys[0] != keys[1]
    assert all(key.startswith(f"team:{team_id}:") for key, team_id in zip(keys, (created, copy)))


@pytest.mark.asyncio
async def test_deleting_a_team_retires_its_key_and_the_recreated_team_gets_another(db, supervisor_client):
    from sqlalchemy import select
    from app.models.database import FactoryContextKey
    first_id = await make_team(supervisor_client, "First")
    old = (await read_supervisor(supervisor_client, first_id))["team_context_key"]
    assert (await supervisor_client.delete(f"/api/v1/agent-teams/presets/{first_id}", headers=DIRECTOR)).status_code == 204
    retired = (await db.scalars(select(FactoryContextKey.retired_at).where(FactoryContextKey.context_key == old))).first()
    assert retired is not None
    second_id = await make_team(supervisor_client, "Second")
    assert second_id == first_id
    assert (await read_supervisor(supervisor_client, second_id))["team_context_key"] not in (None, old)


@pytest.mark.asyncio
async def test_the_three_observations_read_the_key_in_the_same_statement_as_the_team(db, supervisor_client):
    """V1, #1356: a separate key lookup lets a replacement team's key meet the old team's supervisor."""
    from sqlalchemy import event
    from app.services.agent_team_supervisor_service import supervisor_checkin
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id})
    now = datetime.now(timezone.utc)  # the real clock: the status stays healthy, so the reads write no event
    await supervisor_checkin(db, actor, team_id, now + timedelta(hours=1), now=now)  # records the one status event
    expected = (await read_supervisor(supervisor_client, team_id))["team_context_key"]
    statements: list[str] = []
    bind = db.get_bind()
    engine = getattr(bind, "sync_engine", bind)

    def capture(_conn, _cursor, statement, *_rest):
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        read = await read_supervisor(supervisor_client, team_id)
        summary = (await supervisor_client.get(f"/api/v1/agent-teams/presets/{team_id}/summary")).json()["supervisor_observation"]
        actor = await db.get(MailExternalActor, actor_id, populate_existing=True)
        checkin = await supervisor_checkin(db, actor, team_id, now + timedelta(hours=2), now=now + timedelta(seconds=1))
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert read["team_context_key"] == summary["team_context_key"] == checkin["team_context_key"] == expected
    # The key is a column of the statement that reads the team. A statement that reads only the keys is a second read.
    separate = [s for s in statements if s.upper().startswith("SELECT") and "FROM factory_context_keys" in s
                and "FROM agent_team_presets" not in s and "agent_team_presets" not in s.split("FROM factory_context_keys")[0]]
    assert separate == [], separate


@pytest.mark.asyncio
async def test_a_check_in_whose_team_is_deleted_before_the_response_gets_the_defined_refusal(db, supervisor_client, monkeypatch):
    """V1, #1356: the team can disappear after the check-in commits. The caller gets 409 and refreshes, not a 500."""
    from fastapi import HTTPException
    from sqlalchemy import text
    import app.services.agent_team_supervisor_service as service
    from app.api.v1.external_agent_mail import checkin_supervisor
    from app.models.schemas import SupervisorCheckinRequest
    team, actor = await keyed_team(db)
    team_id, actor_id = team.id, actor.id
    await put_supervisor(supervisor_client, team_id, {"actor_id": actor_id})
    real, calls, committed = service._team_and_key, [], {}

    async def deleting_before_the_final_read(connection, preset_id):
        calls.append(preset_id)
        if len(calls) == 2:  # the first call is the observation at the start of the check-in; the second is the final read
            committed["checked_at"] = (await connection.execute(text(
                "SELECT supervisor_checked_at FROM agent_team_presets WHERE id = :id"), {"id": preset_id})).scalar_one()
            await connection.execute(text("DELETE FROM agent_team_presets WHERE id = :id"), {"id": preset_id})
            await connection.commit()
        return await real(connection, preset_id)

    monkeypatch.setattr(service, "_team_and_key", deleting_before_the_final_read)
    actor = await db.get(MailExternalActor, actor_id, populate_existing=True)
    request = SupervisorCheckinRequest(preset_id=team_id, next_check_at=datetime.now(timezone.utc) + timedelta(hours=1))
    with pytest.raises(HTTPException) as refusal:
        await checkin_supervisor(request, actor, db)
    assert len(calls) == 2
    assert committed["checked_at"] is not None, "the check-in was committed before the team was deleted"
    assert refusal.value.status_code == 409 and refusal.value.detail == "supervisor_binding_changed"

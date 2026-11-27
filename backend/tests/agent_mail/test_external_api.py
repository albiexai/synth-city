"""External Agent Mail orchestration API behavior."""
import asyncio
import hashlib
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, configure_sqlite_connection, get_db
from app.main import app
from app.models.database import AgentTeamPreset, AgentTeamSlot, FactoryAuditEvent, MailAgentSession, MailTeamMember
from app.services.agent_mail_service import agent_mail_service
from app.services.external_agent_mail_service import ExternalAgentMailAuthError, external_agent_mail_service
from app.utils import peer_process
from app.utils.repo_utils import derive_repo_identity


@pytest_asyncio.fixture
async def client(db, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "test-director-key")
    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_external_rate_limits(monkeypatch):
    external_agent_mail_service._send_windows.clear()
    monkeypatch.setattr("app.services.agent_mail_service.discover_agent_sessions", lambda: [])


async def _member(db, repo_id, name):
    member = MailTeamMember(
        identity_key=f"repo:{repo_id}",
        repo_id=repo_id,
        repo_path=f"/tmp/{name}",
        repo_name=name,
        display_name=name,
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)
    return member


async def _actor_token(client, key="openclaw", name="OpenClaw"):
    resp = await client.post(
        "/api/v1/external/agent-mail/actors",
        headers={"X-Synth-City-Operator-Token": "test-director-key"},
        json={
            "actor_key": key,
            "display_name": name,
            "kind": "external_tool",
            "description": "local orchestrator",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    return body["token"], body["actor"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


ACTORS_PATH = "/api/v1/external/agent-mail/actors"
DIRECTOR_HEADERS = {"X-Synth-City-Operator-Token": "test-director-key"}


@pytest.mark.asyncio
async def test_external_actor_credential_lifecycle_and_audit(client, db):
    old, actor = await _actor_token(client)
    path = f"{ACTORS_PATH}/{actor['id']}"
    me = f"{ACTORS_PATH}/me"

    rotated = await client.post(f"{path}/rotate", headers=DIRECTOR_HEADERS)
    assert rotated.status_code == 200
    assert rotated.headers["Cache-Control"] == "no-store"
    rotated_token = rotated.json()["token"]
    assert rotated.json()["actor"]["id"] == actor["id"]
    assert (await client.get(me, headers=_auth(old))).status_code == 401
    assert (await client.get(me, headers=_auth(rotated_token))).status_code == 200

    revoked = await client.post(f"{path}/revoke", headers=DIRECTOR_HEADERS)
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert "token" not in revoked.json()
    assert (await client.get(me, headers=_auth(rotated_token))).status_code == 401
    assert (await client.get("/api/v1/external/agent-mail/supervisor-inbox",
                             headers=_auth(rotated_token))).status_code == 401
    assert (await client.post("/api/v1/external/agent-mail/supervisor-checkin",
                              headers=_auth(rotated_token), json={"preset_id": 1})).status_code == 401
    assert (await client.post(f"{path}/rotate", headers=DIRECTOR_HEADERS)).status_code == 409
    payload = {"actor_key": actor["actor_key"], "display_name": "Updated name"}
    assert (await client.post(ACTORS_PATH, headers=DIRECTOR_HEADERS, json=payload)).status_code == 409

    restored = await client.post(f"{path}/restore", headers=DIRECTOR_HEADERS)
    assert restored.status_code == 200
    assert restored.json()["actor"]["id"] == actor["id"]
    restored_token = restored.json()["token"]
    assert (await client.get(me, headers=_auth(rotated_token))).status_code == 401
    assert (await client.get(me, headers=_auth(restored_token))).status_code == 200
    assert (await client.post(f"{path}/restore", headers=DIRECTOR_HEADERS)).status_code == 409

    upsert = await client.post(ACTORS_PATH, headers=DIRECTOR_HEADERS, json=payload)
    assert upsert.status_code == 200
    assert upsert.json()["actor"]["id"] == actor["id"]
    assert upsert.json()["actor"]["display_name"] == "Updated name"
    assert (await client.get(me, headers=_auth(restored_token))).status_code == 401
    assert (await client.get(me, headers=_auth(upsert.json()["token"]))).status_code == 200

    events = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.source.in_(
            ("external_agent_mail.rotate_actor", "external_agent_mail.revoke_actor", "external_agent_mail.restore_actor")
        ),
    ).order_by(FactoryAuditEvent.id))).all()
    assert [event.source for event in events] == [
        f"external_agent_mail.{action}_actor" for action in ("rotate", "revoke", "restore", "rotate")
    ]
    for event in events:
        assert event.actor_kind == "operator" and event.via == "api"
        assert event.context_snapshot["external_actor_id"] == actor["id"]
        stored = repr([getattr(event, column.name) for column in FactoryAuditEvent.__table__.columns])
        for token in (old, rotated_token, restored_token, upsert.json()["token"]):
            assert token not in stored
            assert hashlib.sha256(token.encode()).hexdigest() not in stored


@pytest.mark.asyncio
async def test_actor_credential_writes_require_director_and_keep_fresh_bootstrap(client, db, monkeypatch):
    old, actor = await _actor_token(client)
    path = f"{ACTORS_PATH}/{actor['id']}"
    member = await _member(db, "repo-actor-rotation", "actor-rotation")
    session_token = "synthetic-mail-session"
    db.add(MailAgentSession(member_id=member.id, provider="codex-cli", source="mcp",
        session_key="mcp:actor-rotation", cwd=member.repo_path, mailbox_status="connected",
        capability_token_hash=agent_mail_service.hash_capability_token(session_token)))
    await db.commit()
    endpoints = [(f"{path}/{action}", None) for action in ("revoke", "rotate", "restore")]
    endpoints.append((ACTORS_PATH, {"actor_key": actor["actor_key"], "display_name": "Changed"}))
    for endpoint, payload in endpoints:
        assert (await client.post(endpoint, json=payload)).status_code == 401
        mail = await client.post(endpoint, json=payload, headers={
            **DIRECTOR_HEADERS, "X-Synth-City-Session-Token": session_token,
        })
        assert mail.status_code == 403
    monkeypatch.setattr(settings, "operator_token", "")
    for endpoint, payload in endpoints:
        assert (await client.post(endpoint, json=payload)).status_code == 503
    fresh = await client.post(ACTORS_PATH, json={"actor_key": "fresh-bootstrap", "display_name": "Bootstrap"})
    assert fresh.status_code == 200
    assert (await client.get(f"{ACTORS_PATH}/me", headers=_auth(fresh.json()["token"]))).status_code == 200
    assert (await client.get(f"{ACTORS_PATH}/me", headers=_auth(old))).status_code == 200


@pytest_asyncio.fixture
async def concurrent_actor_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", "test-director-key")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'actors.sqlite'}")
    event.listen(engine.sync_engine, "connect", lambda connection, _: configure_sqlite_connection(connection))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async def override():
            async with sessions() as session:
                yield session

        app.dependency_overrides[get_db] = override
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_fresh_bootstrap_cannot_rotate_the_winning_actor(concurrent_actor_client, monkeypatch):
    client, _ = concurrent_actor_client
    monkeypatch.setattr(settings, "operator_token", "")
    lookup = external_agent_mail_service.actor_id_for_key
    both_saw_absence = asyncio.Event()
    arrivals = 0

    async def wait_after_absent_lookup(db, key):
        nonlocal arrivals
        actor_id = await lookup(db, key)
        if actor_id is None:
            arrivals += 1
            if arrivals == 2:
                both_saw_absence.set()
            await asyncio.wait_for(both_saw_absence.wait(), timeout=5)
        return actor_id

    monkeypatch.setattr(external_agent_mail_service, "actor_id_for_key", wait_after_absent_lookup)
    responses = await asyncio.gather(*(client.post(ACTORS_PATH, json={
        "actor_key": "raced-bootstrap", "display_name": name,
    }) for name in ("First caller", "Second caller")))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json() for response in responses if response.status_code == 200)
    refused = next(response.json() for response in responses if response.status_code == 409)
    assert refused == {"detail": "external_actor_key_exists"}
    me = await client.get(f"{ACTORS_PATH}/me", headers=_auth(winner["token"]))
    assert me.json()["id"] == winner["actor"]["id"]
    assert me.json()["display_name"] == winner["actor"]["display_name"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["rotate", "revoke"])
async def test_actor_authentication_fences_a_credential_change_after_the_lookup(
    concurrent_actor_client, monkeypatch, action,
):
    client, sessions = concurrent_actor_client
    old, actor = await _actor_token(client)
    changed = None
    async with sessions() as reader:
        execute = reader.execute

        async def change_credential_after_lookup(*args, **kwargs):
            nonlocal changed
            result = await execute(*args, **kwargs)
            changed = await client.post(f"{ACTORS_PATH}/{actor['id']}/{action}", headers=DIRECTOR_HEADERS)
            assert changed.status_code == 200
            return result

        monkeypatch.setattr(reader, "execute", change_credential_after_lookup)
        with pytest.raises(ExternalAgentMailAuthError, match="Invalid bearer token"):
            await external_agent_mail_service.authenticate_actor(reader, old)
    assert changed is not None
    me = f"{ACTORS_PATH}/me"
    assert (await client.get(me, headers=_auth(old))).status_code == 401
    if action == "rotate":
        assert (await client.get(me, headers=_auth(changed.json()["token"]))).status_code == 200


@pytest.mark.asyncio
async def test_actor_rotation_rolls_back_if_audit_write_fails(client, monkeypatch):
    from app.services import factory_audit_service

    old, actor = await _actor_token(client)

    async def refuse_audit(*args, **kwargs):
        raise factory_audit_service.AuditWriteError("synthetic failure")

    monkeypatch.setattr(factory_audit_service, "record_event", refuse_audit)
    result = await client.post(f"{ACTORS_PATH}/{actor['id']}/rotate", headers=DIRECTOR_HEADERS)
    assert result.status_code == 503
    assert result.json() == {"detail": "external_actor_audit_unavailable"}
    assert (await client.get(f"{ACTORS_PATH}/me", headers=_auth(old))).status_code == 200


@pytest.mark.asyncio
async def test_external_actor_registration_and_auth(client, db):
    await _member(db, "repo-alpha", "alpha")
    token, actor = await _actor_token(client)

    assert actor["display_name"] == "OpenClaw"

    unauthenticated = await client.get("/api/v1/external/agent-mail/members")
    assert unauthenticated.status_code == 401

    resp = await client.get("/api/v1/external/agent-mail/members", headers=_auth(token))
    assert resp.status_code == 200
    members = resp.json()["members"]
    assert members[0]["display_name"] == "alpha"
    assert "wake_state" in members[0]
    assert members[0]["agent_status"]["session_state"] == "offline"
    assert members[0]["agent_status"]["wake_state"] == "cannot_wake"
    assert members[0]["agent_status"]["wake_reason"] == "wake_target_unbound"

    me = await client.get("/api/v1/external/agent-mail/actors/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["actor_key"] == "openclaw"


@pytest.mark.asyncio
async def test_external_broadcast_requires_scoped_audience(client, db):
    scoped_member = await _member(db, "broadcast-repo", "broadcast-repo")
    other_member = await _member(db, "other-repo", "other-repo")
    token, _ = await _actor_token(client)
    endpoint = "/api/v1/external/agent-mail/broadcasts"
    base = {"subject": "Scoped notice", "body_markdown": "For one repository."}

    anonymous = await client.post(endpoint, json={
        **base,
        "audience_type": "repository",
        "audience_id": "broadcast-repo",
    })
    implicit_global = await client.post(
        endpoint,
        headers=_auth(token),
        json=base,
    )
    operator_global = await client.post(
        endpoint,
        headers=_auth(token),
        json={**base, "audience_type": "operator_global"},
    )
    sent = await client.post(
        endpoint,
        headers=_auth(token),
        json={
            **base,
            "audience_type": "repository",
            "audience_id": "broadcast-repo",
        },
    )

    assert anonymous.status_code == 401
    assert (implicit_global.status_code, implicit_global.json()["detail"]) == (
        400,
        "broadcast_audience_required",
    )
    assert (operator_global.status_code, operator_global.json()["detail"]) == (
        403,
        "operator_global_forbidden",
    )
    assert sent.status_code == 200
    assert sent.json()["message"]["audience_type"] == "repository"
    assert sent.json()["message"]["audience_id"] == "broadcast-repo"
    assert {recipient["member_id"] for recipient in sent.json()["recipients"]} == {
        scoped_member.id
    }
    assert other_member.id not in {
        recipient["member_id"] for recipient in sent.json()["recipients"]
    }


@pytest.mark.asyncio
async def test_external_context_request_preserves_sender_attribution(client, db):
    recipient = await _member(db, "repo-beta", "beta")
    token, _ = await _actor_token(client)

    resp = await client.post(
        "/api/v1/external/agent-mail/context-requests",
        headers=_auth(token),
        json={
            "recipient_member_id": recipient.id,
            "subject": "Need repo context",
            "body_markdown": "Please inspect the API owner.",
            "why_needed": "OpenClaw validation",
            "files_or_symbols": ["backend/app/api/v1/agent_mail.py"],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["actor"]["display_name"] == "OpenClaw"
    assert body["message"]["sender_name"] == "OpenClaw"
    assert body["message"]["sender_type"] == "external_actor"
    assert body["message"]["sender_member_id"] is None
    assert body["message"]["sender_actor_id"] == body["actor"]["id"]
    assert body["recipients"][0]["member_id"] == recipient.id

    messages = await client.get("/api/v1/agent-mail/messages", headers={"X-Synth-City-Operator-Token": "test-director-key"})
    assert messages.status_code == 200
    assert messages.json()[0]["sender_name"] == "OpenClaw"
    assert messages.json()[0]["sender_type"] == "external_actor"


@pytest.mark.asyncio
async def test_legacy_message_create_cannot_spoof_external_actor(client, db):
    recipient = await _member(db, "repo-beta", "beta")
    _, actor = await _actor_token(client)

    resp = await client.post(
        "/api/v1/agent-mail/messages",
        json={
            "sender_actor_id": actor["id"],
            "recipient_member_id": recipient.id,
            "body_markdown": "Pretend to be OpenClaw.",
        },
    )

    assert resp.status_code == 401
    assert resp.json()["detail"] == "session_token_required"


@pytest.mark.asyncio
async def test_external_actor_cannot_read_other_actor_threads(client, db):
    recipient = await _member(db, "repo-beta", "beta")
    token, _ = await _actor_token(client, key="openclaw", name="OpenClaw")
    other_token, _ = await _actor_token(client, key="runner", name="Runner")
    created = await client.post(
        "/api/v1/external/agent-mail/context-requests",
        headers=_auth(token),
        json={
            "recipient_member_id": recipient.id,
            "subject": "Private request",
            "body_markdown": "Only OpenClaw should read this.",
        },
    )
    message_id = created.json()["message"]["id"]

    thread = await client.get(
        f"/api/v1/external/agent-mail/threads/{message_id}",
        headers=_auth(other_token),
    )
    status = await client.get(
        f"/api/v1/external/agent-mail/requests/{message_id}/status",
        headers=_auth(other_token),
    )
    wait = await client.get(
        f"/api/v1/external/agent-mail/requests/{message_id}/wait?timeout_seconds=0",
        headers=_auth(other_token),
    )

    assert thread.status_code == 403
    assert status.status_code == 403
    assert wait.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["codex-cli", "claude-code"])
async def test_external_delivery_reports_non_tmux_agent_as_delivered_waiting(client, db, provider):
    recipient = await _member(db, "repo-beta", "beta")
    db.add(
        MailAgentSession(
            member_id=recipient.id,
            provider=provider,
            source="mcp",
            session_key="mcp:beta",
            cwd=recipient.repo_path,
            mailbox_status="connected",
        )
    )
    await db.commit()
    token, _ = await _actor_token(client)

    resp = await client.post(
        "/api/v1/external/agent-mail/context-requests",
        headers=_auth(token),
        json={
            "recipient_member_id": recipient.id,
            "subject": "Need repo context",
            "body_markdown": "Please inspect this.",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["delivery_state"] == "delivered_waiting"
    assert body["recipients"][0]["status"] == "delivered_waiting"
    assert body["recipients"][0]["wake_state"] == "delivered_waiting"
    assert body["recipients"][0]["wake_attempted"] is False
    assert body["recipients"][0]["wake_succeeded"] is False
    roster = await client.get("/api/v1/external/agent-mail/members", headers=_auth(token))
    member = next(m for m in roster.json()["members"] if m["id"] == recipient.id)
    assert member["agent_status"]["session_state"] == "online"
    assert member["agent_status"]["wake_state"] == "cannot_wake"
    assert member["agent_status"]["wake_reason"] == "wake_target_unbound"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "display_name"),
    [("codex-cli", "Codex"), ("claude-code", "Claude Code")],
)
async def test_external_delivery_reports_tmux_wake_success(
    client,
    db,
    tmp_path,
    monkeypatch,
    provider,
    display_name,
):
    cwd = tmp_path / "repo-beta"
    cwd.mkdir()
    fake = [
        {
            "provider": provider,
            "provider_display_name": display_name,
            "tmux_target": "deck:0.1",
            "session_name": "deck",
            "window_name": "main",
            "pane_id": "%7",
            "cwd": str(cwd),
            "pid": "4242",
            "status": "active",
        }
    ]
    preset = AgentTeamPreset(name="External wake team")
    db.add(preset)
    await db.flush()
    identity = derive_repo_identity(str(cwd))
    slot = AgentTeamSlot(
        preset_id=preset.id, position=0, display_name="Recipient",
        provider=provider, repo_id=identity["repo_id"],
        repo_path=identity["repo_root"], repo_name=identity["repo_name"],
    )
    db.add(slot)
    await db.flush()
    fake[0]["team_preset_id"] = preset.id
    fake[0]["team_slot_id"] = slot.id
    recipient = await agent_mail_service.get_or_create_slot_member(db, slot)
    db.add(MailAgentSession(
        member_id=recipient.id, provider=provider, source="mcp",
        session_key=f"mcp:external:{provider}", cwd=str(cwd),
        team_preset_id=preset.id, team_slot_id=slot.id,
        mailbox_status="connected", last_seen_at=datetime.utcnow(),
        capability_token_hash=agent_mail_service.hash_capability_token("external-bound-token"),
        bound_pane_pid=4242, bound_pane_proc_start="external-start",
        wake_enabled=True,
    ))
    await db.commit()
    monkeypatch.setattr(peer_process, "pane_is_alive", lambda _pid, _start: True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            stdout="%7|4242" if command[1] == "display-message" else "",
            stderr="", returncode=0,
        )

    monkeypatch.setattr("app.services.agent_mail_service.discover_agent_sessions", lambda: fake)
    monkeypatch.setattr("app.services.agent_mail_service.subprocess.run", fake_run)
    monkeypatch.setattr("app.services.agent_mail_service.time.sleep", lambda delay: None)

    token, _ = await _actor_token(client)
    members = await client.get("/api/v1/external/agent-mail/members", headers=_auth(token))
    recipient_id = members.json()["members"][0]["id"]

    resp = await client.post(
        "/api/v1/external/agent-mail/messages",
        headers=_auth(token),
        json={
            "recipient_member_id": recipient_id,
            "subject": "Wake check",
            "body_markdown": "Please check your inbox.",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["delivery_state"] == "wake_succeeded"
    assert body["recipients"][0]["status"] == "wake_succeeded"
    assert body["recipients"][0]["wake_method"] == "tmux"
    tmux_calls = [call for call in calls if call[0][0] == "tmux"]
    assert len(tmux_calls) == 4
    assert tmux_calls[1][0][3] == "%7"
    assert [call[0][1] for call in tmux_calls] == [
        "display-message", "send-keys", "display-message", "send-keys"
    ]


@pytest.mark.asyncio
async def test_external_request_status_wait_and_ack_lifecycle(client, db):
    recipient = await _member(db, "repo-beta", "beta")
    recipient_token = "recipient-session-token"
    db.add(
        MailAgentSession(
            member_id=recipient.id,
            provider="codex-cli",
            source="mcp",
            session_key="mcp:external-answer",
            cwd=recipient.repo_path,
            mailbox_status="connected",
            capability_token_hash=agent_mail_service.hash_capability_token(
                recipient_token
            ),
        )
    )
    await db.commit()
    token, _ = await _actor_token(client)
    created = await client.post(
        "/api/v1/external/agent-mail/context-requests",
        headers=_auth(token),
        json={
            "recipient_member_id": recipient.id,
            "subject": "Round trip",
            "body_markdown": "Please answer.",
        },
    )
    message_id = created.json()["message"]["id"]

    pending = await client.get(
        f"/api/v1/external/agent-mail/requests/{message_id}/status",
        headers=_auth(token),
    )
    assert pending.status_code == 200
    assert pending.json()["request_status"] == "pending"

    answer = await client.post(
        "/api/v1/agent-mail/messages",
        headers={"X-Synth-City-Session-Token": recipient_token},
        json={
            "kind": "answer",
            "sender_member_id": recipient.id,
            "thread_root_id": message_id,
            "body_markdown": "Here is the answer.",
        },
    )
    assert answer.status_code == 200

    waited = await client.get(
        f"/api/v1/external/agent-mail/requests/{message_id}/wait?timeout_seconds=1",
        headers=_auth(token),
    )
    assert waited.status_code == 200
    assert waited.json()["answered"] is True
    assert waited.json()["request_status"] == "answered"

    acked = await client.post(
        f"/api/v1/external/agent-mail/requests/{message_id}/ack",
        headers=_auth(token),
    )
    assert acked.status_code == 200
    assert acked.json()["acknowledged"] is True
    assert acked.json()["request_status"] == "acknowledged"


@pytest.mark.asyncio
async def test_external_actor_can_reply_in_own_thread(client, db):
    recipient = await _member(db, "repo-beta", "beta")
    token, _ = await _actor_token(client)
    created = await client.post(
        "/api/v1/external/agent-mail/context-requests",
        headers=_auth(token),
        json={
            "recipient_member_id": recipient.id,
            "subject": "Thread",
            "body_markdown": "Initial request.",
        },
    )
    message_id = created.json()["message"]["id"]

    reply = await client.post(
        f"/api/v1/external/agent-mail/threads/{message_id}/replies",
        headers=_auth(token),
        json={
            "body_markdown": "Adding external follow-up detail.",
        },
    )

    assert reply.status_code == 200
    assert reply.json()["message"]["thread_root_id"] == message_id

    thread = await client.get(
        f"/api/v1/external/agent-mail/threads/{message_id}",
        headers=_auth(token),
    )
    assert thread.status_code == 200
    assert len(thread.json()["replies"]) == 1
    assert thread.json()["replies"][0]["sender_name"] == "OpenClaw"


@pytest.mark.asyncio
async def test_external_message_rate_limit_returns_429(client, db, monkeypatch):
    recipient = await _member(db, "repo-beta", "beta")
    token, _ = await _actor_token(client)
    monkeypatch.setattr(
        "app.services.external_agent_mail_service.EXTERNAL_RATE_LIMIT_MAX_MESSAGES",
        1,
    )

    first = await client.post(
        "/api/v1/external/agent-mail/messages",
        headers=_auth(token),
        json={
            "recipient_member_id": recipient.id,
            "body_markdown": "one",
        },
    )
    second = await client.post(
        "/api/v1/external/agent-mail/messages",
        headers=_auth(token),
        json={
            "recipient_member_id": recipient.id,
            "body_markdown": "two",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "external_agent_mail_rate_limited"
    assert int(second.headers["retry-after"]) >= 1


# --- #752: external actors and canonical thread roots ----------------------------------------

@pytest.mark.asyncio
async def test_external_actor_reply_with_a_nested_id_is_stored_under_the_canonical_root(client, db):
    recipient = await _member(db, "repo-nested-ext", "nested-ext")
    token, _ = await _actor_token(client)
    created = await client.post(
        "/api/v1/external/agent-mail/context-requests", headers=_auth(token),
        json={"recipient_member_id": recipient.id, "subject": "Thread", "body_markdown": "Initial request."})
    root_id = created.json()["message"]["id"]
    first = await client.post(
        f"/api/v1/external/agent-mail/threads/{root_id}/replies", headers=_auth(token),
        json={"body_markdown": "First follow-up."})
    first_id = first.json()["message"]["id"]
    nested = await client.post(
        f"/api/v1/external/agent-mail/threads/{first_id}/replies", headers=_auth(token),
        json={"body_markdown": "Follow-up by a reply id."})
    assert nested.status_code == 200, nested.text
    assert nested.json()["message"]["thread_root_id"] == root_id
    assert [item["member_id"] for item in nested.json()["recipients"]] == [recipient.id]
    thread = await client.get(f"/api/v1/external/agent-mail/threads/{root_id}", headers=_auth(token))
    assert len(thread.json()["replies"]) == 2


@pytest.mark.asyncio
async def test_external_actor_cannot_reach_a_foreign_thread_through_a_nested_id(client, db):
    from app.models.database import MailMessage

    member = await _member(db, "repo-foreign-ext", "foreign-ext")
    other = await _member(db, "repo-foreign-ext-2", "foreign-ext-2")
    owner_token, _ = await _actor_token(client)
    other_token, other_actor = await _actor_token(client, key="runner", name="Runner")
    created = await client.post(
        "/api/v1/external/agent-mail/context-requests", headers=_auth(owner_token),
        json={"recipient_member_id": member.id, "subject": "Thread", "body_markdown": "Owner request."})
    root_id = created.json()["message"]["id"]
    # An older release could leave another actor's own row inside this thread.
    planted = MailMessage(kind="message", thread_root_id=root_id, sender_actor_id=other_actor["id"],
                          body_markdown="planted", audience_type="member", audience_id=str(member.id))
    db.add(planted)
    await db.commit()
    before = (await client.get(f"/api/v1/external/agent-mail/threads/{root_id}",
                               headers=_auth(owner_token))).json()["replies"]
    for target in (root_id, planted.id):
        refused = await client.post(
            f"/api/v1/external/agent-mail/threads/{target}/replies", headers=_auth(other_token),
            json={"body_markdown": "Not mine."})
        assert refused.status_code in {403, 404}, (target, refused.status_code, refused.text)
    after = (await client.get(f"/api/v1/external/agent-mail/threads/{root_id}",
                              headers=_auth(owner_token))).json()["replies"]
    assert [item["id"] for item in after] == [item["id"] for item in before]
    assert other.id != member.id


@pytest.mark.asyncio
async def test_external_service_refuses_a_request_with_a_thread_id_and_accepts_a_plain_message(client, db):
    from app.models.database import MailExternalActor
    from app.models.schemas import ExternalAgentMailMessageRequest

    member = await _member(db, "repo-ext-req", "ext-req")
    token, actor_data = await _actor_token(client, key="ext-req", name="ExtReq")
    created = await client.post(
        "/api/v1/external/agent-mail/context-requests", headers=_auth(token),
        json={"recipient_member_id": member.id, "subject": "Thread", "body_markdown": "Owner request."})
    root_id = created.json()["message"]["id"]
    actor = await db.get(MailExternalActor, actor_data["id"])
    request = ExternalAgentMailMessageRequest(recipient_member_id=member.id, body_markdown="Nested request")
    from sqlalchemy import func, select
    from app.models.database import MailMessage
    before = await db.scalar(select(func.count()).select_from(MailMessage))
    with pytest.raises(ValueError, match="a request starts its own thread"):
        await external_agent_mail_service.send_message(
            db, actor, request, kind="context_request", recipient_member_id=member.id, thread_root_id=root_id)
    assert await db.scalar(select(func.count()).select_from(MailMessage)) == before
    ok = await client.post(f"/api/v1/external/agent-mail/threads/{root_id}/replies", headers=_auth(token),
                           json={"body_markdown": "A plain follow-up."})
    assert ok.status_code == 200 and ok.json()["message"]["thread_root_id"] == root_id

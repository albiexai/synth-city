"""Operator write attribution and the Director-action boundary.

The operator key stays the authority for the write. ``via`` is attribution: ``mail_tool`` is a fact the server derives from a
validated Mail session; ``ui`` and ``api`` are labels from the browser's Origin header and are not proof of who called. A bad
session token never refuses an otherwise authorized write; the event records it as unattributed with ``session_invalid``.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models.database import AgentTeamPreset, FactoryAuditEvent, MailAgentSession
from app.models.schemas import MailAgentRegisterRequest
from app.services import factory_audit_service as audit
from app.services.agent_mail_service import agent_mail_service

OPERATOR_KEY = "audit-via-test-operator-key"
SESSION_TOKEN = "audit-via-test-session-token"
UI_ORIGIN = "http://localhost:5173"
PATH = "/api/v1/agent-teams/presets"
MAIL_REFUSAL = "director_action_not_via_mail"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def client(db, monkeypatch):
    monkeypatch.setattr(settings, "operator_token", OPERATOR_KEY)

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"X-Synth-City-Operator-Token": OPERATOR_KEY}) as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def preset(db):
    row = AgentTeamPreset(name="Audit via")
    db.add(row)
    await db.commit()
    return row


async def _session(db, tmp_path, **changes) -> MailAgentSession:
    request = MailAgentRegisterRequest(source="mcp", provider="pi-cli", cwd=str(tmp_path),
                                       session_key="mcp:audit-via", pid=os.getpid())
    _member, session = await agent_mail_service.register_session(db, request)
    session.capability_token_hash = agent_mail_service.hash_capability_token(SESSION_TOKEN)
    for name, value in changes.items():
        setattr(session, name, value)
    await db.commit()
    return session


async def _events(db, preset, kind="policy_change"):
    rows = await db.execute(select(FactoryAuditEvent).where(
        FactoryAuditEvent.team_preset_id == preset.id, FactoryAuditEvent.event_kind == kind)
        .order_by(FactoryAuditEvent.id))
    return list(rows.scalars().all())


async def _campaign(client, preset, milestone="v4.2", **headers):
    return await client.patch(f"{PATH}/{preset.id}", json={"backlog_milestone": milestone}, headers=headers)


# --- derive_actor ---------------------------------------------------------------------------------------------------------


def test_the_operator_actor_keeps_its_shape_when_no_access_path_is_given():
    assert audit.derive_actor(actor_kind="operator") == {
        "actor_kind": "operator", "actor_member_id": None, "actor_session_id": None,
        "actor_reference": "shared-operator-credential"}


def test_the_access_path_is_recorded_and_only_known_paths_are_accepted():
    for via in ("ui", "mail_tool", "api"):
        assert audit.derive_actor(actor_kind="operator", via=via)["via"] == via
    with pytest.raises(ValueError):
        audit.derive_actor(actor_kind="operator", via="curl")


def test_an_operator_never_asserts_a_member_and_asserts_a_session_only_through_a_mail_tool():
    with pytest.raises(ValueError, match="operator_role_cannot_assert_identity"):
        audit.derive_actor(actor_kind="operator", member_id=3)
    with pytest.raises(ValueError, match="operator_role_cannot_assert_identity"):
        audit.derive_actor(actor_kind="operator", session_id=9, via="ui")
    actor = audit.derive_actor(actor_kind="operator", session_id=9, via="mail_tool")
    assert actor["actor_session_id"] == 9 and actor["actor_member_id"] is None


def test_an_external_actor_records_its_kind_and_actor_id_and_nothing_else():
    actor = audit.derive_actor(actor_kind="external_actor", external_actor_id=6)
    assert actor == {"actor_kind": "external_actor", "actor_member_id": None, "actor_session_id": None,
                     "actor_reference": "external_actor:6"}
    with pytest.raises(ValueError):
        audit.derive_actor(actor_kind="external_actor")
    with pytest.raises(ValueError):
        audit.derive_actor(actor_kind="external_actor", external_actor_id=6, member_id=1)


@pytest.mark.asyncio
async def test_an_external_actor_event_is_stored_with_the_actor_id(db, preset):
    await audit.record_event(
        db, event_kind="policy_change", source="test", occurred_at=datetime.utcnow(),
        actor=audit.derive_actor(actor_kind="external_actor", external_actor_id=6), team_preset_id=preset.id)
    await db.commit()
    (event,) = await _events(db, preset)
    assert (event.actor_kind, event.actor_reference, event.via) == ("external_actor", "external_actor:6", None)


# --- the route records how a change was made ------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,expected", [
    ({"Origin": UI_ORIGIN}, "ui"),
    ({}, "api"),
    ({"Origin": "https://not-configured.example"}, "api"),
])
async def test_a_campaign_change_records_the_label_from_the_origin(client, db, preset, headers, expected):
    response = await _campaign(client, preset, **headers)
    assert response.status_code == 200
    (event,) = await _events(db, preset)
    assert (event.via, event.actor_kind, event.actor_session_id) == (expected, "operator", None)
    assert event.after_values["backlog_milestone"] == "v4.2" and event.before_values["backlog_milestone"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_key", ["", "wrong-key", OPERATOR_KEY])
async def test_campaign_update_through_mail_is_refused_and_audited(client, db, preset, tmp_path, operator_key):
    session = await _session(db, tmp_path)
    await _campaign(client, preset, "v4.2", Origin=UI_ORIGIN)
    response = await _campaign(client, preset, "v4.3", **{
        "X-Synth-City-Session-Token": SESSION_TOKEN, "X-Synth-City-Operator-Token": operator_key,
    })
    assert response.status_code == 403 and response.json()["detail"]["code"] == MAIL_REFUSAL
    assert "UI" in response.json()["detail"]["message"]
    await db.refresh(preset)
    assert preset.backlog_milestone == "v4.2"
    ui, tool = await _events(db, preset)
    assert (ui.via, ui.actor_session_id) == ("ui", None)
    assert (tool.via, tool.actor_session_id) == ("mail_tool", session.id)
    expected_actor = ("operator", None) if operator_key == OPERATOR_KEY else ("member", session.member_id)
    assert (tool.actor_kind, tool.actor_member_id) == expected_actor
    assert tool.action_outcome == "rejected" and tool.sanitized_reason == MAIL_REFUSAL


@pytest.mark.asyncio
async def test_a_valid_session_wins_over_the_origin_and_caller_claims_are_never_read(client, db, preset, tmp_path):
    session = await _session(db, tmp_path)
    response = await client.patch(
        f"{PATH}/{preset.id}", json={"backlog_milestone": "v4.2", "actor_session_id": 999, "via": "ui"},
        headers={"X-Synth-City-Session-Token": SESSION_TOKEN, "Origin": UI_ORIGIN, "X-Synth-City-Session-Id": "999"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == MAIL_REFUSAL
    (event,) = await _events(db, preset)
    assert (event.via, event.actor_session_id) == ("mail_tool", session.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("case", ["unknown", "closed", "stale"])
async def test_a_bad_session_token_never_refuses_the_write_and_is_recorded_as_unattributed(
        client, db, preset, tmp_path, monkeypatch, case, strict):
    """The attribution rule does not depend on whether Mail enforces capability tokens (review 5930749403, P2)."""
    monkeypatch.setattr(settings, "mail_capability_tokens_required", strict)
    changes = {"closed": {"closed_at": datetime.utcnow()}, "stale": {"mailbox_status": "offline"}}.get(case, {})
    await _session(db, tmp_path, **changes)
    token = "not-the-token" if case == "unknown" else SESSION_TOKEN
    response = await _campaign(client, preset, Origin=UI_ORIGIN, **{"X-Synth-City-Session-Token": token})
    assert response.status_code == 200 and response.json()["backlog_milestone"] == "v4.2"
    (event,) = await _events(db, preset)
    assert (event.via, event.actor_kind, event.actor_session_id, event.actor_member_id) == ("ui", "operator", None, None)
    assert event.context_snapshot["session_invalid"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("pane_alive,expected", [(True, "mail_tool"), (False, "api")])
async def test_a_role_bearing_session_is_attributed_only_while_its_pane_is_alive(
        client, db, preset, tmp_path, monkeypatch, strict, pane_alive, expected):
    """A slot-bound session needs its bound pane alive, under either Mail setting. The pane reader is faked."""
    from app.api.v1 import deps
    monkeypatch.setattr(settings, "mail_capability_tokens_required", strict)
    monkeypatch.setattr(deps.peer_process, "pane_is_alive", lambda pid, start: pane_alive)
    session = await _session(db, tmp_path, team_slot_id=999, bound_pane_pid=424242, bound_pane_proc_start="fake-start")
    response = await _campaign(client, preset, **{"X-Synth-City-Session-Token": SESSION_TOKEN})
    assert response.status_code == (403 if expected == "mail_tool" else 200)
    (event,) = await _events(db, preset)
    assert event.via == expected
    assert event.actor_session_id == (session.id if expected == "mail_tool" else None)
    assert bool((event.context_snapshot or {}).get("session_invalid")) is (expected == "api")


@pytest.mark.asyncio
async def test_a_role_bearing_session_without_a_recorded_pane_is_unattributed(client, db, preset, tmp_path):
    await _session(db, tmp_path, team_slot_id=999, bound_pane_pid=None, bound_pane_proc_start=None)
    await _campaign(client, preset, **{"X-Synth-City-Session-Token": SESSION_TOKEN})
    (event,) = await _events(db, preset)
    assert (event.via, event.actor_session_id, event.context_snapshot["session_invalid"]) == ("api", None, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [True, False])
async def test_a_valid_session_is_attributed_under_either_setting(client, db, preset, tmp_path, monkeypatch, strict):
    monkeypatch.setattr(settings, "mail_capability_tokens_required", strict)
    session = await _session(db, tmp_path)
    response = await _campaign(client, preset, **{"X-Synth-City-Session-Token": SESSION_TOKEN})
    assert response.status_code == 403
    (event,) = await _events(db, preset)
    assert (event.via, event.actor_session_id) == ("mail_tool", session.id)


@pytest.mark.asyncio
async def test_a_valid_session_does_not_carry_the_invalid_marker(client, db, preset, tmp_path):
    await _session(db, tmp_path)
    await _campaign(client, preset, **{"X-Synth-City-Session-Token": SESSION_TOKEN})
    (event,) = await _events(db, preset)
    assert not (event.context_snapshot or {}).get("session_invalid")


async def _refusals(db, preset):
    rows = await db.execute(select(FactoryAuditEvent).where(
        FactoryAuditEvent.team_preset_id == preset.id, FactoryAuditEvent.action_outcome == "rejected")
        .order_by(FactoryAuditEvent.id))
    return list(rows.scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize("token,flagged", [("bad-token", True), (SESSION_TOKEN, False)])
async def test_the_refusal_observation_keeps_the_bounded_invalid_session_flag(
        client, db, preset, tmp_path, token, flagged):
    """A valid Mail session is stopped before business rules; invalid attribution stays advisory."""
    session = await _session(db, tmp_path)
    response = await client.patch(f"{PATH}/{preset.id}", json={"autonomy_enabled": True},
                                  headers={"X-Synth-City-Session-Token": token, "Origin": UI_ORIGIN})
    assert response.status_code == (400 if flagged else 403)
    reason = "leader_assignment_required" if flagged else MAIL_REFUSAL
    assert reason in response.text
    await db.refresh(preset)
    assert preset.autonomy_enabled is not True
    (event,) = await _refusals(db, preset)
    assert bool((event.context_snapshot or {}).get("session_invalid")) is flagged
    assert (event.via, event.actor_session_id) == (("ui", None) if flagged else ("mail_tool", session.id))
    assert token not in json.dumps([str(v) for v in (event.context_snapshot, event.before_values, event.after_values)])


@pytest.mark.asyncio
async def test_the_route_function_still_works_when_called_directly_without_a_context(db, preset):
    """The hosted suite calls update_preset(id, request, None, db) by position; that call keeps working."""
    from app.api.v1.agent_teams import update_preset
    from app.models.schemas import AgentTeamPresetUpdate
    renamed = await update_preset(preset.id, AgentTeamPresetUpdate(name="Direct"), None, db)
    assert renamed.name == "Direct"
    await update_preset(preset.id, AgentTeamPresetUpdate(backlog_milestone="v4.2"), None, db)
    (event,) = await _events(db, preset)
    assert (event.actor_kind, event.via, event.actor_session_id) == ("operator", None, None)


@pytest.mark.asyncio
async def test_an_invalid_session_does_not_remove_the_operator_requirement(client, db, preset):
    response = await client.patch(f"{PATH}/{preset.id}", json={"backlog_milestone": "v4.2"},
                                  headers={"X-Synth-City-Operator-Token": "wrong", "X-Synth-City-Session-Token": SESSION_TOKEN})
    assert response.status_code == 401 and response.json()["detail"] == "operator_token_invalid"
    assert await _events(db, preset) == []


@pytest.mark.asyncio
async def test_a_campaign_change_records_one_event_only_when_a_field_changes(client, db, preset):
    await _campaign(client, preset, "v4.2")
    await _campaign(client, preset, "v4.2")
    assert len(await _events(db, preset)) == 1
    await client.patch(f"{PATH}/{preset.id}", json={"planned_decisions": [{"text": "Ship slice 1", "state": "now"}]})
    events = await _events(db, preset)
    assert len(events) == 2 and events[1].after_values["planned_decisions_count"] == 1
    assert "Ship slice 1" not in json.dumps(events[1].after_values)


@pytest.mark.asyncio
async def test_turning_off_autonomy_and_updating_parent_link_stay_available(client, db, preset, tmp_path):
    await _session(db, tmp_path)
    link = "https://github.com/fixture/repo/issues/42"
    response = await client.patch(f"{PATH}/{preset.id}", json={"autonomy_enabled": False, "parent_issue_url": link},
                                  headers={"X-Synth-City-Session-Token": SESSION_TOKEN})
    assert response.status_code == 200
    assert response.json()["parent_issue_url"] == link
    (event,) = await _events(db, preset)
    assert event.after_values == {"autonomy_enabled": False} and event.via == "mail_tool"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"backlog_milestone": None}, {"planned_decisions": None},
                                      {"autonomy_enabled": True, "name": "Must not be saved"}])
async def test_mixed_preset_patch_refuses_director_fields_before_any_change(client, db, preset, tmp_path, payload):
    await _session(db, tmp_path)
    response = await client.patch(f"{PATH}/{preset.id}", json=payload,
                                  headers={"X-Synth-City-Session-Token": SESSION_TOKEN})
    assert response.status_code == 403 and response.json()["detail"]["code"] == MAIL_REFUSAL
    await db.refresh(preset)
    assert preset.name == "Audit via" and not preset.autonomy_enabled


@pytest.mark.asyncio
async def test_preset_patch_requires_operator_when_attribution_is_unavailable(client, db, preset, monkeypatch):
    from app.api.v1.deps import operator_write_context

    async def unavailable_context():
        return None

    monkeypatch.setitem(app.dependency_overrides, operator_write_context, unavailable_context)
    response = await _campaign(client, preset, **{"X-Synth-City-Operator-Token": ""})
    assert response.status_code == 401
    assert response.json()["detail"] == "operator_token_required"
    await db.refresh(preset)
    assert preset.backlog_milestone is None


@pytest.mark.asyncio
async def test_oversized_preset_id_still_gets_an_audited_mail_refusal(client, db, tmp_path):
    session = await _session(db, tmp_path)
    response = await client.patch(
        f"{PATH}/{2**63}", json={"backlog_milestone": "v4.4"},
        headers={"X-Synth-City-Session-Token": SESSION_TOKEN, "X-Synth-City-Operator-Token": ""},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == MAIL_REFUSAL
    event = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.sanitized_reason == MAIL_REFUSAL,
    ))).one()
    assert event.team_preset_id is None
    assert (event.via, event.actor_kind, event.actor_session_id) == ("mail_tool", "member", session.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_key", ["", OPERATOR_KEY])
@pytest.mark.parametrize("method,path,body", [
    ("POST", "/presets", {"name": "Director fixture"}),
    ("POST", "/presets/from-agent-mail", {"name": "Mail import fixture", "member_ids": [999]}),
    ("POST", "/presets/from-agent-bridge", {"name": "Bridge import fixture"}),
    ("POST", "/presets/999/duplicate", {"name": "Copy fixture"}),
    ("DELETE", "/presets/999", None),
    ("PUT", "/presets/999/leader", {"leader_slot_id": 1, "reason": "Fixture",
                                    "expected_updated_at": "2026-01-01T00:00:00Z"}),
    ("POST", "/presets/999/slots", {"display_name": "Fixture", "provider": "codex-cli", "repo_path": "/fixture"}),
    ("PATCH", "/slots/999", {"launch_options": {"model": "fixture-model"}}),
    ("DELETE", "/slots/999", None),
    ("POST", "/presets/999/slots/reorder", {"slot_ids": [1]}),
    ("POST", "/presets/999/github-scopes", {"repo_owner": "fixture", "repo_name": "repo", "repo_path": "/fixture"}),
    ("PATCH", "/github-scopes/999", {"max_auto_merges_per_day": 2, "merge_policy": "auto"}),
    ("PATCH", "/github-scopes/999/continuation-policy", {"continuation_enabled": True,
        "max_continuation_revisions": 1, "max_continuation_failed_heads": 1, "max_failed_heads_per_revision": 1,
        "max_scope_paths": 1, "max_scope_commands": 1}),
    ("PATCH", "/github-scopes/999/delivery-policy", {"expected_revision": 1, "policy": {}, "reason": "Fixture"}),
    ("PATCH", "/github-work-items/999/delivery-policy", {"expected_dispatch_nonce": "fixture",
        "expected_scope_revision": 0, "target_policy_revision": 1, "reason": "Fixture"}),
    ("PUT", "/presets/999/supervisor", {"actor_id": None}),
])
async def test_director_routes_reject_a_current_mail_session(client, db, tmp_path, method, path, body, operator_key):
    session = await _session(db, tmp_path)
    path = "/api/v1/agent-teams" + path
    refused = await client.request(method, path, json=body,
                                   headers={"X-Synth-City-Session-Token": SESSION_TOKEN,
                                            "X-Synth-City-Operator-Token": operator_key})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == MAIL_REFUSAL
    audit_event = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.sanitized_reason == MAIL_REFUSAL))).one()
    assert (audit_event.via, audit_event.actor_session_id, audit_event.action_outcome) == ("mail_tool", session.id, "rejected")
    assert audit_event.actor_kind == ("operator" if operator_key else "member")
    # The synthetic target can be absent. A request without Mail reaches the route's normal result.
    direct = await client.request(method, path, json=body)
    assert direct.status_code in {200, 204, 400, 404}


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expected", [({}, 400), ({"confirm": "wrong"}, 400),
                                           ({"confirm": 42}, 400), ({"confirm": "fixture-setting"}, 200)])
async def test_typed_director_confirmation_uses_the_top_level_action_name(db, monkeypatch, body, expected):
    from app.api.v1.deps import require_director_action

    monkeypatch.setattr(settings, "operator_token", OPERATOR_KEY)
    fixture = FastAPI()

    async def fixture_db():
        yield db

    fixture.dependency_overrides[get_db] = fixture_db

    @fixture.post("/confirmation", dependencies=[Depends(require_director_action(confirm="fixture-setting"))])
    async def confirmation(payload: dict):
        return {"confirmed": payload["confirm"]}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture), base_url="http://fixture") as client:
        response = await client.post("/confirmation", json=body, headers={"X-Synth-City-Operator-Token": OPERATOR_KEY})
    assert response.status_code == expected
    assert response.json() == ({"confirmed": "fixture-setting"} if expected == 200 else
                               {"detail": {"code": "director_confirmation_required", "confirm": "fixture-setting"}})


@pytest.mark.asyncio
async def test_no_key_token_or_hash_appears_in_any_audit_row(client, db, preset, tmp_path):
    await _session(db, tmp_path)
    await _campaign(client, preset, "v4.2", **{"X-Synth-City-Session-Token": SESSION_TOKEN})
    await _campaign(client, preset, "v4.3", **{"X-Synth-City-Session-Token": "bad-" + SESSION_TOKEN})
    rows = (await db.execute(text("SELECT * FROM factory_audit_events"))).all()
    dump = json.dumps([[str(value) for value in row] for row in rows])
    for secret in (OPERATOR_KEY, SESSION_TOKEN, "bad-" + SESSION_TOKEN, agent_mail_service.hash_capability_token(SESSION_TOKEN)):
        assert secret not in dump


@pytest.mark.asyncio
async def test_the_response_does_not_gain_the_new_field(client, db, preset):
    body = (await _campaign(client, preset)).json()
    assert "via" not in body
    from app.models import schemas
    assert not any("via" in getattr(model, "model_fields", {}) for model in vars(schemas).values()
                   if isinstance(model, type) and model.__name__.startswith("FactoryAudit"))


# --- storage ----------------------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_old_database_without_the_column_is_migrated(tmp_path):
    from app.database import _run_sqlite_compat_migrations, _sqlite_columns
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE factory_audit_events DROP COLUMN via"))
    async with engine.begin() as conn:
        await _run_sqlite_compat_migrations(conn)
    async with engine.begin() as conn:
        columns = await _sqlite_columns(conn, "factory_audit_events")
    await engine.dispose()
    assert "via" in columns

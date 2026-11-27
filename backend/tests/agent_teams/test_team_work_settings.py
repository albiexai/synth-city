import pytest
from pydantic import ValidationError
from sqlalchemy import event, select

from app.config import settings
from app.models.database import AgentTeamPreset, FactoryAuditEvent
from app.models.schemas import AgentTeamPresetUpdate, TeamWorkSettings
from app.services import factory_audit_service as audit
from app.services.agent_team_service import agent_team_service
from tests.agent_teams.test_github_workspace_api import OPERATOR_HEADERS, client, operator_token  # noqa: F401
from tests.test_audit_via import SESSION_TOKEN, _session


@pytest.mark.asyncio
async def test_work_settings_http_protection_validation_and_history(client, db, monkeypatch):
    teams = [AgentTeamPreset(name=name) for name in ("A", "B")]
    db.add_all(teams)
    await db.commit()
    path, other = [f"/api/v1/agent-teams/presets/{team.id}" for team in teams]
    patch = {"mail_request_unanswered_seconds": 90, "github_team_idle_timeout_seconds": 60,
             "github_publish_summaries_enabled": False, "github_owner_idle_timeout_seconds": 600,
             "github_nudge_grace_seconds": 120, "wake_followup_enabled": False,
             "wake_followup_delay_seconds": 60}
    headers = OPERATOR_HEADERS

    assert (await client.get(f"{path}/work-settings")).status_code == 401
    assert (await client.patch(path, json=patch)).status_code == 401
    for name, value in zip(patch, (59, "60", 0, 59, 3601, 0, 3601)):
        assert (await client.patch(path, headers=headers, json={name: value})).status_code == 422
    assert (await client.patch(path, headers=headers, json=patch)).status_code == 200
    assert (await client.patch(path, headers=headers, json={"name": "Renamed", **patch})).status_code == 200
    assert (await client.patch(other, headers=headers, json={next(iter(patch)): 600})).status_code == 200
    cleared = {name: None for name in patch}
    assert (await client.patch(path, headers=headers, json=cleared)).status_code == 200
    monkeypatch.setattr(settings, "github_publish_summaries_enabled", True)
    monkeypatch.setattr(settings, "github_nudge_grace_seconds", 4000)

    response = await client.get(f"{path}/work-settings", headers=headers, params={"limit": 1})
    current = response.json()
    assert response.status_code == 200
    assert current["overrides"] == cleared
    assert current["defaults"] == {name: getattr(settings, name) for name in patch}
    assert current["history"][0]["changes"] == {
        name: {"before": value, "after": None} for name, value in patch.items()
    }
    previous = await client.get(f"{path}/work-settings", headers=headers,
                                params={"before_id": current["next_before_id"]})
    entries = previous.json()["history"]
    assert len(entries) == 1
    assert entries[0]["actor_kind"] == "operator"
    assert entries[0]["changes"] == {name: {"before": None, "after": value} for name, value in patch.items()}


@pytest.mark.asyncio
async def test_duplicate_preset_preserves_work_overrides(db):
    original = AgentTeamPreset(name="Original team")
    db.add(original)
    await db.commit()
    overrides = TeamWorkSettings(
        mail_request_unanswered_seconds=120,
        github_team_idle_timeout_seconds=90,
        github_publish_summaries_enabled=False,
        github_owner_idle_timeout_seconds=600,
        github_nudge_grace_seconds=120,
        wake_followup_enabled=False,
        wake_followup_delay_seconds=60,
    )
    await agent_team_service.update_preset(db, original.id, work_settings=overrides)

    duplicate = await agent_team_service.duplicate_preset(db, original.id)
    copied = await db.get(AgentTeamPreset, duplicate.id)
    assert copied.id != original.id
    assert {name: getattr(copied, name) for name in TeamWorkSettings.model_fields} == overrides.model_dump()


@pytest.mark.parametrize(("field", "value", "error_type"), [
    ("mail_request_unanswered_seconds", True, "int_type"),
    ("mail_request_unanswered_seconds", "60", "int_type"),
    ("github_team_idle_timeout_seconds", True, "int_type"),
    ("github_team_idle_timeout_seconds", "30", "int_type"),
    ("github_publish_summaries_enabled", "false", "bool_type"),
    ("github_publish_summaries_enabled", 0, "bool_type"),
    ("github_owner_idle_timeout_seconds", True, "int_type"),
    ("github_owner_idle_timeout_seconds", "60", "int_type"),
    ("github_nudge_grace_seconds", True, "int_type"),
    ("github_nudge_grace_seconds", "30", "int_type"),
    ("wake_followup_enabled", "false", "bool_type"),
    ("wake_followup_enabled", 0, "bool_type"),
    ("wake_followup_delay_seconds", True, "int_type"),
    ("wake_followup_delay_seconds", "30", "int_type"),
])
def test_work_overrides_reject_coerced_types(field, value, error_type):
    with pytest.raises(ValidationError) as invalid:
        AgentTeamPresetUpdate(**{field: value})
    assert [(error["loc"], error["type"]) for error in invalid.value.errors()] == [((field,), error_type)]


@pytest.mark.parametrize(("field", "minimum"), [
    ("mail_request_unanswered_seconds", 60),
    ("github_team_idle_timeout_seconds", 30),
    ("github_owner_idle_timeout_seconds", 60),
    ("github_nudge_grace_seconds", 30),
    ("wake_followup_delay_seconds", 30),
])
def test_work_timeout_accepts_its_minimum_and_refuses_one_second_less(field, minimum):
    assert getattr(AgentTeamPresetUpdate(**{field: minimum}), field) == minimum
    with pytest.raises(ValidationError, match="greater than or equal to"):
        AgentTeamPresetUpdate(**{field: minimum - 1})


@pytest.mark.parametrize(("field", "maximum"), [
    ("mail_request_unanswered_seconds", 86400),
    ("github_team_idle_timeout_seconds", 86400),
    ("github_owner_idle_timeout_seconds", 86400),
    ("github_nudge_grace_seconds", 3600),
    ("wake_followup_delay_seconds", 3600),
])
def test_work_timeout_accepts_its_maximum_and_refuses_one_second_more(field, maximum):
    assert getattr(AgentTeamPresetUpdate(**{field: maximum}), field) == maximum
    with pytest.raises(ValidationError, match="less than or equal to"):
        AgentTeamPresetUpdate(**{field: maximum + 1})


@pytest.mark.asyncio
async def test_work_settings_history_excludes_same_team_autonomy_audit(client, db):
    preset = AgentTeamPreset(name="Audit scope", autonomy_enabled=True)
    db.add(preset)
    await db.commit()
    await agent_team_service.update_preset(
        db, preset.id, work_settings=TeamWorkSettings(github_team_idle_timeout_seconds=120))
    await agent_team_service.update_preset(db, preset.id, autonomy_enabled=False)

    response = await client.get(f"/api/v1/agent-teams/presets/{preset.id}/work-settings",
                                headers=OPERATOR_HEADERS)
    assert response.status_code == 200
    assert [entry["changes"] for entry in response.json()["history"]] == [
        {"github_team_idle_timeout_seconds": {"before": None, "after": 120}},
    ]


@pytest.mark.asyncio
async def test_work_setting_is_locked_before_mutation_and_through_audit(db, monkeypatch):
    preset = AgentTeamPreset(name="Locked work settings")
    db.add(preset)
    await db.commit()
    acquire = agent_team_service._lock_preset_for_mutation
    record = audit.record_event
    locked_preset = None
    transaction = None
    audit_calls = []
    writer_claims = []

    def observe_writer_claim(_conn, _cursor, statement, *_):
        if statement.startswith("UPDATE agent_team_presets SET id=agent_team_presets.id "):
            writer_claims.append(statement)

    async def acquire_lock(session, preset_id):
        nonlocal locked_preset, transaction
        locked_preset = await acquire(session, preset_id)
        assert locked_preset.mail_request_unanswered_seconds is None
        transaction = session.sync_session.get_transaction()
        assert transaction is not None
        return locked_preset

    async def record_locked_change(session, **kwargs):
        assert locked_preset is not None
        assert len(writer_claims) == 1
        assert session.sync_session.get_transaction() is transaction
        assert locked_preset.mail_request_unanswered_seconds == 600
        audit_calls.append(kwargs["after_values"])
        return await record(session, **kwargs)

    monkeypatch.setattr(agent_team_service, "_lock_preset_for_mutation", acquire_lock)
    monkeypatch.setattr(audit, "record_event", record_locked_change)
    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", observe_writer_claim)
    try:
        await agent_team_service.update_preset(
            db, preset.id, work_settings=TeamWorkSettings(mail_request_unanswered_seconds=600))
    finally:
        event.remove(engine, "before_cursor_execute", observe_writer_claim)
    assert audit_calls == [{"mail_request_unanswered_seconds": 600}]



@pytest.mark.asyncio
async def test_dispatch_pause_is_a_director_action_and_records_history(client, db, tmp_path):
    team = AgentTeamPreset(name="Drain", autonomy_enabled=True)
    db.add(team)
    await db.commit()
    path = f"/api/v1/agent-teams/presets/{team.id}"
    pause = {"paused": True, "reason": "Drain the queue"}
    assert (await client.put(f"{path}/dispatch-pause", json=pause)).status_code == 401
    await _session(db, tmp_path)
    session = {"X-Synth-City-Session-Token": SESSION_TOKEN}
    for headers in (session, {**OPERATOR_HEADERS, **session}):
        refused = await client.put(f"{path}/dispatch-pause", json=pause, headers=headers)
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "director_action_not_via_mail"
    for invalid in ({"paused": True, "reason": "   "}, {"paused": "true", "reason": "Drain"}):
        assert (await client.put(f"{path}/dispatch-pause", json=invalid, headers=OPERATOR_HEADERS)).status_code == 422

    secret = "ghp_" + "a" * 36
    pause["reason"] += f" ({secret})"
    assert (await client.put(f"{path}/dispatch-pause", json=pause, headers=OPERATOR_HEADERS)).status_code == 200
    assert (await client.put(f"{path}/dispatch-pause", json=pause, headers=OPERATOR_HEADERS)).status_code == 200
    active = (await client.get(path)).json()
    assert active["dispatch_paused"] is True and active["autonomy_enabled"] is True
    assert active["dispatch_paused_by"] == "operator"
    assert active["dispatch_pause_reason"].startswith("Drain the queue") and secret not in str(active)
    resume = await client.put(f"{path}/dispatch-pause", json={"paused": False, "reason": "Drain complete"},
                              headers=OPERATOR_HEADERS)
    assert resume.status_code == 200
    assert resume.json()["dispatch_paused"] is False and resume.json()["autonomy_enabled"] is True
    assert resume.json()["dispatch_pause_reason"] is None and resume.json()["dispatch_paused_by"] is None
    history = (await db.scalars(select(FactoryAuditEvent).where(
        FactoryAuditEvent.source == "agent_team_service.set_dispatch_pause").order_by(FactoryAuditEvent.id))).all()
    assert [(event.before_values, event.after_values) for event in history] == [
        ({"dispatch_paused": False}, {"dispatch_paused": True}),
        ({"dispatch_paused": True}, {"dispatch_paused": False}),
    ]
    assert all(event.actor_reference == "shared-operator-credential" for event in history)
    assert [event.sanitized_reason for event in history] == [active["dispatch_pause_reason"], "Drain complete"]


@pytest.mark.asyncio
async def test_dispatch_pause_rolls_back_when_audit_write_fails(db, monkeypatch):
    team = AgentTeamPreset(name="Audit unavailable")
    db.add(team)
    await db.commit()
    team_id = team.id

    async def fail(session, **kwargs):
        await session.flush()
        raise audit.AuditWriteError("injected audit error")

    monkeypatch.setattr(audit, "record_event", fail)
    with pytest.raises(audit.AuditWriteError):
        await agent_team_service.set_dispatch_pause(db, team_id, paused=True, reason="Drain queue")
    assert (await db.get(AgentTeamPreset, team_id, populate_existing=True)).dispatch_paused is False

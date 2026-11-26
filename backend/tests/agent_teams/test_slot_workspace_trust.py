"""Slot trust provisioning through a synthetic restricted helper."""
from __future__ import annotations

import importlib
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text, update

from app.config import settings
from app.database import _run_sqlite_compat_migrations
from app.models.database import AgentTeamSlot
from app.models.schemas import AgentTeamLaunchRequest, AgentTeamPresetCreate, AgentTeamSlotCreate, AgentTeamSlotUpdate
from app.services.agent_team_service import agent_team_service


@pytest.fixture
def helper():
    return importlib.import_module("app.services.agent_config_client")


@pytest.mark.parametrize(("status", "result", "outcome"), [
    (0, {"ok": True, "changed": True}, True),
    (0, {"ok": True, "changed": False}, False),
    (75, {"ok": False, "error": "agent_config_busy"}, "busy"),
    (1, {"ok": True, "changed": True}, "refused"),
    (75, {"ok": False, "error": "unexpected"}, "refused"),
    (0, {"ok": True, "changed": 1}, "refused"),
])
def test_fixed_helper_command_and_typed_response(helper, monkeypatch, status, result, outcome):
    def run(argv, **kwargs):
        assert argv == ["/usr/bin/sudo", "-n", "-u", "deckproduct", "--",
                        "/opt/albiex-synth-city-product/libexec/agent-config"]
        assert json.loads(kwargs["input"]) == {
            "operation": "folder_trust", "provider": "codex-cli", "folder": "/work/example path",
        }
        assert kwargs["timeout"] > 0 and kwargs.get("shell", False) is False
        return subprocess.CompletedProcess(argv, status, json.dumps(result) + "\n", "private fixture")
    monkeypatch.setattr(helper.subprocess, "run", run)
    if outcome == "busy":
        with pytest.raises(helper.AgentConfigBusy):
            helper.prepare_workspace_trust_via_helper("codex-cli", "/work/example path")
    elif outcome == "refused":
        with pytest.raises(helper.AgentConfigError, match="Workspace trust could not be prepared") as failure:
            helper.prepare_workspace_trust_via_helper("codex-cli", "/work/example path")
        assert "private fixture" not in str(failure.value)
    else:
        assert helper.prepare_workspace_trust_via_helper("codex-cli", "/work/example path") is outcome


@pytest.fixture
def integrated_helper(helper, monkeypatch):
    monkeypatch.setattr(settings, "team_workspace_trust_helper_enabled", True)
    calls = []
    errors = []

    def prepare(provider, folder):
        calls.append((provider, folder))
        if errors and (error := errors.pop(0)):
            raise error()
        return True

    monkeypatch.setattr("app.services.agent_team_service.prepare_workspace_trust_via_helper", prepare)
    return calls, errors


@pytest.mark.asyncio
async def test_slot_creation_and_actual_binding_changes_prepare_trust(db, tmp_path, integrated_helper):
    calls, _ = integrated_helper
    other = tmp_path / "other"
    other.mkdir()
    request = AgentTeamSlotCreate(display_name="Builder", provider="codex-cli", repo_path=str(tmp_path))
    preset = await agent_team_service.create_preset(db, AgentTeamPresetCreate(name="Trust team", slots=[request]))
    assert preset.slots[0].workspace_trust_status == "ready"
    preset = await agent_team_service.add_slot(db, preset.id, request.model_copy(update={"display_name": "Verifier"}))
    slot = preset.slots[-1]
    assert slot.workspace_trust_status == "ready"
    await agent_team_service.update_slot(db, slot.id, AgentTeamSlotUpdate(role="Verifier"))
    updated = await agent_team_service.update_slot(db, slot.id, AgentTeamSlotUpdate(repo_path=str(other)))
    assert updated.slots[-1].workspace_trust_status == "ready"
    await agent_team_service.update_slot(db, slot.id, AgentTeamSlotUpdate(provider="claude-code"))
    assert calls == [("codex-cli", str(tmp_path)), ("codex-cli", str(tmp_path)),
                     ("codex-cli", str(other)), ("claude-code", str(other))]


@pytest.mark.asyncio
async def test_busy_persists_and_launch_retries_before_spawning(db, tmp_path, integrated_helper, helper, monkeypatch):
    calls, errors = integrated_helper
    errors.extend([helper.AgentConfigBusy, helper.AgentConfigBusy, None, helper.AgentConfigError])
    created = await agent_team_service.create_preset(db, AgentTeamPresetCreate(
        name="Busy team",
        slots=[AgentTeamSlotCreate(display_name="Builder", provider="claude-code", repo_path=str(tmp_path))],
    ))
    slot = created.slots[0]
    assert slot.workspace_trust_status == "pending"
    spawn_calls = []
    item = SimpleNamespace(slot_id=slot.id, action="spawn", warnings=[])
    plan = SimpleNamespace(can_launch=True, plan_hash="fixture", items=[item],
                           reuse_count=0, adopt_count=0, spawn_count=1, skipped_count=0, blocked_count=0)
    monkeypatch.setattr(agent_team_service, "plan_launch", AsyncMock(return_value=plan))
    monkeypatch.setattr(agent_team_service, "_bootstrap_prompt", AsyncMock(return_value=None))

    def spawn(provider, options, *, workspace_trust_prepared, **kwargs):
        spawn_calls.append((provider, options.directory, workspace_trust_prepared))
        return {"session_name": "fixture", "tmux_target": "fixture:0.0"}

    monkeypatch.setattr("app.services.agent_team_service.spawn_session", spawn)
    request = AgentTeamLaunchRequest(skip_plan_confirmation=True, reuse_existing=False)
    busy = await agent_team_service.launch(db, created.id, request)
    assert busy.items[0].block_code == "agent_config_busy"
    assert not spawn_calls
    assert (await agent_team_service.get_preset(db, created.id)).slots[0].workspace_trust_status == "pending"

    spawned = await agent_team_service.launch(db, created.id, request)
    assert spawned.items[0].status == "pending_registration"
    assert spawn_calls == [("claude-code", str(tmp_path), True)]
    assert (await agent_team_service.get_preset(db, created.id)).slots[0].workspace_trust_status == "ready"

    override = tmp_path / "override"
    override.mkdir()
    refused = await agent_team_service.launch(db, created.id, request.model_copy(
        update={"repo_path_override": str(override)},
    ))
    assert refused.items[0].block_code == "agent_config_refused"
    assert len(spawn_calls) == 1
    assert (await agent_team_service.get_preset(db, created.id)).slots[0].workspace_trust_status == "ready"
    assert calls == [("claude-code", str(tmp_path))] * 3 + [("claude-code", str(override))]

    await agent_team_service.launch(db, created.id, request)
    assert spawn_calls[-1] == ("claude-code", str(tmp_path), False)
    assert len(calls) == 4

    original_binding = await db.get(AgentTeamSlot, slot.id)
    await db.execute(update(AgentTeamSlot).where(AgentTeamSlot.id == slot.id).values(
        repo_path=str(override),
    ).execution_options(synchronize_session=False))
    await db.commit()
    assert original_binding.repo_path == str(tmp_path)
    stale = await agent_team_service.launch(db, created.id, request)
    assert stale.items[0].error == "The slot workspace changed. Review a new launch plan."
    assert len(spawn_calls) == 2 and len(calls) == 4


@pytest.mark.asyncio
async def test_older_slots_can_be_read_after_compatible_sqlite_upgrade(db, tmp_path):
    created = await agent_team_service.create_preset(db, AgentTeamPresetCreate(
        name="Older team",
        slots=[AgentTeamSlotCreate(display_name="Builder", provider="codex-cli", repo_path=str(tmp_path))],
    ))
    await db.execute(text("ALTER TABLE agent_team_slots DROP COLUMN workspace_trust_status"))
    await db.commit()
    async with db.bind.connect() as connection:
        await _run_sqlite_compat_migrations(connection)
        await connection.commit()
    db.expire_all()
    current = await agent_team_service.get_preset(db, created.id)
    assert current.slots[0].id == created.slots[0].id
    assert current.slots[0].workspace_trust_status is None


@pytest.mark.parametrize("reply", [
    '{"ok": true, "changed": true}\nother output\n',
    '{"ok": 0, "error": "agent_config_busy"}\n',
])
def test_unexpected_helper_output_fails_closed(helper, monkeypatch, reply):
    status = 0 if "true" in reply else 75
    monkeypatch.setattr(helper.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, status, reply, ""))
    with pytest.raises(helper.AgentConfigError) as caught:
        helper.prepare_workspace_trust_via_helper("claude-code", "/work/example")
    assert caught.value.code == "agent_config_refused"


@pytest.mark.parametrize("prepared", [None, False])
def test_dedicated_spawn_requires_prepared_trust_for_the_same_directory(helper, monkeypatch, tmp_path, prepared):
    from app.services.agent_bridge import spawn
    from app.services.providers.base import SpawnCommandOptions

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(settings, "team_workspace_trust_helper_enabled", True)
    monkeypatch.setattr(spawn, "prepare_workspace_trust", lambda *args: pytest.fail("local writer fallback"))
    monkeypatch.setattr(spawn.subprocess, "run", lambda *args, **kwargs: pytest.fail("tmux spawn before trust"))
    directory = target if prepared is None else link
    with pytest.raises(helper.AgentConfigError):
        spawn.spawn_session(
            "codex-cli", SpawnCommandOptions(directory=str(directory), mode="plain"),
            extra_env={"SYNTH_CITY_TEAM_SLOT_ID": "1"}, workspace_trust_prepared=prepared,
        )

"""#960 slice 1: a member whose Mail tool list is older than the deployed one is flagged, as a display fact only.

The shim reports a fingerprint of the tools it registered when its session registers. The backend compares it with the
fingerprint of the deployed list. The comparison grants no authority and changes no wake or delivery rule.
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import Base
from app.models.database import MailAgentSession
from app.models.schemas import MailAgentRegisterRequest
from app.services.agent_mail_service import agent_mail_service
from app.services.agent_status_service import agent_status_service
from app.services.tool_catalog import STALE_REASON, catalog_fingerprint, current_catalog_fingerprint, deployed_schemas
from tests.agent_teams.test_agent_status import seed_agent

CURRENT = current_catalog_fingerprint()
# #1146: a session started before #1100, same tool names, but the extension tool without its two new parameters.
def _before_1100() -> dict:
    schemas = {name: dict(schema) for name, schema in deployed_schemas().items()}
    extend = schemas["synth_city_extend_effort_limit"]
    extend["properties"] = {key: value for key, value in extend["properties"].items()
                            if key not in {"expected_extension_minutes", "evidence"}}
    extend["required"] = [key for key in extend.get("required", []) if key not in {"expected_extension_minutes", "evidence"}]
    return schemas


OLD = catalog_fingerprint(_before_1100())


# --- the fingerprint and its drift guard -------------------------------------------------------------------------------


def test_the_fingerprint_is_stable_order_independent_and_sensitive_to_the_list():
    a, b = {"type": "object", "properties": {"x": {"type": "integer"}}}, {"type": "object", "properties": {}}
    assert catalog_fingerprint({"b": b, "a": a}) == catalog_fingerprint({"a": a, "b": b})
    assert catalog_fingerprint({"a": a, "b": b}) != catalog_fingerprint({"a": a, "b": b, "c": b})
    assert catalog_fingerprint({"a": a}) != catalog_fingerprint({"b": a})
    assert len(catalog_fingerprint({"a": a})) == 32 and int(catalog_fingerprint({"a": a}), 16) >= 0


def test_a_changed_parameter_under_the_same_name_changes_the_fingerprint():
    # #1146: #1100 added required parameters to synth_city_extend_effort_limit under the same name.
    assert set(_before_1100()) == set(deployed_schemas())
    assert OLD != CURRENT
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    retyped = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    assert catalog_fingerprint({"t": schema}) != catalog_fingerprint({"t": retyped})
    # Key order inside a schema does not matter.
    assert catalog_fingerprint({"t": {"required": ["x"], "properties": {"x": {"type": "integer"}}, "type": "object"}}) == \
        catalog_fingerprint({"t": schema})


@pytest.mark.asyncio
async def test_the_backends_expected_tool_list_is_the_one_the_shim_registers():
    """The drift guard: a tool added to one side only changes one fingerprint and fails here."""
    import mcp_shim.agent_mail_server as shim
    registered = {tool.name: tool.inputSchema for tool in await shim.mcp.list_tools() if tool.name.startswith("synth_city_")}
    from app.services.provider_upgrade import _SUFFIXES
    assert sorted(registered) == sorted("synth_city_" + suffix for suffix in _SUFFIXES)
    # #1146: the committed snapshot carries every tool's input schema; regenerate it with scripts/tool-catalog-schemas.py.
    assert registered == deployed_schemas()
    assert shim.tool_catalog_fingerprint() == CURRENT == catalog_fingerprint(registered)


# --- registration stores it ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registering_stores_the_reported_fingerprint_and_a_later_one_replaces_it(db, tmp_path):
    request = MailAgentRegisterRequest(source="mcp", provider="pi-cli", cwd=str(tmp_path), session_key="mcp:catalog",
                                       pid=os.getpid(), tool_catalog=OLD)
    _member, session = await agent_mail_service.register_session(db, request)
    assert session.tool_catalog == OLD
    _member, session = await agent_mail_service.register_session(db, request.model_copy(update={"tool_catalog": CURRENT}))
    assert session.tool_catalog == CURRENT


@pytest.mark.asyncio
async def test_a_registration_without_the_field_stores_none(db, tmp_path):
    request = MailAgentRegisterRequest(source="mcp", provider="pi-cli", cwd=str(tmp_path), session_key="mcp:older-shim",
                                       pid=os.getpid())
    _member, session = await agent_mail_service.register_session(db, request)
    assert request.tool_catalog is None and session.tool_catalog is None


@pytest.mark.asyncio
async def test_an_old_database_without_the_column_is_migrated(tmp_path):
    from sqlalchemy import text

    from app.database import _run_sqlite_compat_migrations, _sqlite_columns
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE mail_agent_sessions DROP COLUMN tool_catalog"))
    async with engine.begin() as conn:
        await _run_sqlite_compat_migrations(conn)
    async with engine.begin() as conn:
        columns = await _sqlite_columns(conn, "mail_agent_sessions")
    await engine.dispose()
    assert "tool_catalog" in columns


# --- the projection ----------------------------------------------------------------------------------------------------


async def _status(db, member):
    return (await agent_status_service.for_members(db, [member]))[member.id]


async def _set_catalog(db, session, value):
    await db.execute(update(MailAgentSession).where(MailAgentSession.id == session.id).values(tool_catalog=value))
    await db.commit()


@pytest.mark.asyncio
async def test_a_session_started_with_the_deployed_list_is_current(db, monkeypatch):
    _team, _slot, member, _pane, mcp, *_ = await seed_agent(db, monkeypatch)
    await _set_catalog(db, mcp, CURRENT)
    status = await _status(db, member)
    assert status.tool_catalog_state == "current" and status.tool_catalog_reason is None


@pytest.mark.asyncio
async def test_a_session_started_with_an_older_list_is_stale_with_the_restart_text(db, monkeypatch):
    _team, _slot, member, _pane, mcp, *_ = await seed_agent(db, monkeypatch)
    await _set_catalog(db, mcp, OLD)
    status = await _status(db, member)
    assert status.tool_catalog_state == "stale" and status.tool_catalog_reason == STALE_REASON
    assert STALE_REASON == "tool catalog stale: restart in resume mode"


@pytest.mark.asyncio
async def test_a_session_that_never_reported_one_is_unknown_not_stale(db, monkeypatch):
    _team, _slot, member, _pane, mcp, *_ = await seed_agent(db, monkeypatch)
    await _set_catalog(db, mcp, None)
    status = await _status(db, member)
    assert status.tool_catalog_state == "unknown" and status.tool_catalog_reason is None


@pytest.mark.asyncio
async def test_a_member_with_no_connected_session_is_unknown(db, monkeypatch):
    _team, _slot, member, _pane, mcp, *_ = await seed_agent(db, monkeypatch)
    await db.execute(update(MailAgentSession).where(MailAgentSession.id == mcp.id)
                     .values(mailbox_status="closed", closed_at=datetime.utcnow(), tool_catalog=OLD))
    await db.commit()
    assert (await _status(db, member)).tool_catalog_state == "unknown"


async def _second_session(db, member, mcp, catalog, **changes):
    values = dict(member_id=member.id, provider=mcp.provider, source="mcp", session_key=f"mcp:second-{catalog}",
                  team_preset_id=mcp.team_preset_id, team_slot_id=mcp.team_slot_id, mailbox_status="connected",
                  last_seen_at=datetime.utcnow(), capability_token_hash="synthetic-private-hash-2",
                  bound_pane_pid=mcp.bound_pane_pid, bound_pane_proc_start=mcp.bound_pane_proc_start,
                  cwd=mcp.cwd, tool_catalog=catalog)
    values.update(changes)
    db.add(MailAgentSession(**values))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second,expected", [
    (CURRENT, CURRENT, "current"),
    (CURRENT, OLD, "stale"),
    (OLD, CURRENT, "stale"),
    (OLD, OLD, "stale"),
    (CURRENT, None, "unknown"),
    (None, CURRENT, "unknown"),
    (None, None, "unknown"),
    (OLD, None, "stale"),
])
async def test_several_connected_sessions_are_judged_together_and_conservatively(
        db, monkeypatch, first, second, expected):
    """Current only when every connected session reported the deployed list; stale when any reported another."""
    _team, _slot, member, _pane, mcp, *_ = await seed_agent(db, monkeypatch)
    await _set_catalog(db, mcp, first)
    await _second_session(db, member, mcp, second or "none")
    await db.execute(update(MailAgentSession).where(MailAgentSession.session_key == "mcp:second-" + (second or "none"))
                     .values(tool_catalog=second))
    await db.commit()
    assert (await _status(db, member)).tool_catalog_state == expected


@pytest.mark.asyncio
async def test_a_closed_stale_session_does_not_make_a_current_member_stale(db, monkeypatch):
    _team, _slot, member, _pane, mcp, *_ = await seed_agent(db, monkeypatch)
    await _set_catalog(db, mcp, CURRENT)
    await _second_session(db, member, mcp, OLD, mailbox_status="closed", closed_at=datetime.utcnow())
    assert (await _status(db, member)).tool_catalog_state == "current"


@pytest.mark.asyncio
async def test_the_catalog_state_never_changes_wake_delivery_or_authority(db, monkeypatch):
    """Display only: the same member reads the same wake facts whatever its catalog state."""
    _team, _slot, member, pane, mcp, item, workspace, _idle = await seed_agent(db, monkeypatch)
    facts = []
    for catalog in (None, CURRENT, OLD):
        await _set_catalog(db, mcp, catalog)
        status = await _status(db, member)
        facts.append((status.session_state, status.activity_state, status.wake_state, status.wake_reason))
    assert facts[0] == facts[1] == facts[2]
    before = (item.dispatch_status, item.ack_received_at, workspace.lease_token)
    await db.refresh(item)
    await db.refresh(workspace)
    assert (item.dispatch_status, item.ack_received_at, workspace.lease_token) == before

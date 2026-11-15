"""PreToolUse edit guard: unread stop/hold Mail blocks edits until it is read (#930)."""
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import update

from app.database import get_db
from app.main import app
from app.models.database import (
    AgentTeamPreset,
    AgentTeamSlot,
    GithubWorkItem,
    MailAgentSession,
    MailExternalActor,
    MailMessage,
    MailReceipt,
    MailTeamMember,
    TeamGithubScope,
)

ROUTE = "/api/v1/agent-mail/hooks/pre-tool-use"
MAIL = "mcp__albiex-synth-city-mail__"
SHIM = Path(__file__).resolve().parents[2] / "mcp_shim" / "team_question_hook.py"


@pytest_asyncio.fixture
async def client(db):
    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _slot(preset_id, position, name):
    return AgentTeamSlot(
        preset_id=preset_id, position=position, display_name=name, provider="claude-code",
        repo_id="r", repo_path="/tmp/r", repo_name="r",
    )


def _member(slot, preset_id):
    return MailTeamMember(
        identity_key=f"default:{slot.id}", repo_id="r", repo_path="/tmp/r", repo_name="r",
        display_name=slot.display_name, team_preset_id=preset_id, team_slot_id=slot.id,
    )


class World:
    """A team with a builder, a Leader, a peer, a bound Supervisor and one leased item."""

    def __init__(self, db):
        self.db = db

    async def build(self):
        db = self.db
        actor = MailExternalActor(actor_key="sup", display_name="Supervisor", kind="supervisor", token_hash="x")
        db.add(actor)
        preset = AgentTeamPreset(name="team")
        db.add(preset)
        await db.flush()
        preset.supervisor_actor_id = actor.id
        slots = [_slot(preset.id, i, name) for i, name in enumerate(("Builder", "Leader", "Peer", "Other"))]
        db.add_all(slots)
        await db.flush()
        preset.leader_slot_id = slots[1].id
        members = [_member(slot, preset.id) for slot in slots]
        db.add_all(members)
        scope = TeamGithubScope(preset_id=preset.id, repo_owner="o", repo_name="r", repo_path="/tmp/r")
        db.add(scope)
        await db.flush()

        def item(number, owner):
            return GithubWorkItem(
                scope_id=scope.id, issue_number=number, issue_title="t", issue_url="u",
                github_updated_at=datetime.utcnow(), owner_slot_id=owner.id,
            )

        mine, theirs = item(1, slots[0]), item(2, slots[3])
        db.add_all([mine, theirs])
        await db.flush()
        self.actor = actor
        self.builder_slot, self.builder = slots[0], members[0]
        self.leader, self.peer, self.other = members[1], members[2], members[3]
        self.my_item, self.other_item = mine.id, theirs.id
        db.add(MailAgentSession(
            member_id=self.builder.id, provider="claude-code", source="hook",
            session_key=f"cc:s1:team-slot:{self.builder_slot.id}",
        ))
        await db.commit()
        return self

    async def mail(self, *, sender="leader", subject="STOP: hold your edits", payload=None,
                   decision=None, to=None, body="private body text"):
        sender_kwargs = {
            "leader": {"sender_member_id": self.leader.id, "sender_type": "member"},
            "peer": {"sender_member_id": self.peer.id, "sender_type": "member"},
            "supervisor": {"sender_actor_id": self.actor.id, "sender_type": "external_actor"},
            "system": {"sender_type": "system"},
        }[sender]
        recipient = to or self.builder
        message = MailMessage(
            kind="message", subject=subject, body_markdown=body, payload=payload, decision=decision,
            recipient_member_id=recipient.id, audience_type="member", audience_id=str(recipient.id),
            **sender_kwargs,
        )
        self.db.add(message)
        await self.db.flush()
        self.db.add(MailReceipt(message_id=message.id, member_id=recipient.id))
        await self.db.commit()
        return message.id

    async def read(self, message_id):
        await self.db.execute(
            update(MailReceipt).where(MailReceipt.message_id == message_id).values(read_at=datetime.utcnow())
        )
        await self.db.commit()

    def event(self, tool="Edit", **extra):
        return {
            "session_id": "s1", "provider": "claude-code", "tool_name": tool,
            "team_slot_id": self.builder_slot.id, **extra,
        }


@pytest_asyncio.fixture
async def world(db):
    return await World(db).build()


def _denied(response):
    assert response.status_code == 200
    body = response.json()
    output = body.get("hookSpecificOutput") or {}
    return output if output.get("permissionDecision") == "deny" else None


@pytest.mark.asyncio
async def test_unread_stop_refuses_edit_then_allows_after_read(client, world):
    message_id = await world.mail()
    denial = _denied(await client.post(ROUTE, json=world.event("Edit")))
    assert denial is not None
    assert denial["hookEventName"] == "PreToolUse"
    assert str(message_id) in denial["permissionDecisionReason"]
    assert "inbox" in denial["permissionDecisionReason"]

    await world.read(message_id)
    allowed = await client.post(ROUTE, json=world.event("Edit"))
    assert _denied(allowed) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [
    "Edit", "Write", "MultiEdit", "NotebookEdit",
    MAIL + "synth_city_send_message", MAIL + "synth_city_report_dispatch_status",
    MAIL + "synth_city_request_work_item_approval",
])
async def test_edit_and_state_changing_tools_are_refused(client, world, tool):
    await world.mail()
    assert _denied(await client.post(ROUTE, json=world.event(tool))) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [
    "Read", "Grep", "Glob", "Bash", "WebFetch", "Agent",
    MAIL + "synth_city_check_inbox", MAIL + "synth_city_whoami",
    MAIL + "synth_city_get_work_item_context", MAIL + "synth_city_list_work_items",
    MAIL + "synth_city_render_github_summary", MAIL + "synth_city_prepare_work_remaining_summary",
    None,
])
async def test_read_only_tools_are_never_refused(client, world, tool):
    await world.mail()
    event = world.event(tool or "Read")
    if tool is None:
        del event["tool_name"]
    assert _denied(await client.post(ROUTE, json=event)) is None


@pytest.mark.asyncio
async def test_reason_never_carries_subject_or_body(client, world):
    await world.mail(subject="STOP: very private subject", body="very private body")
    reason = _denied(await client.post(ROUTE, json=world.event()))["permissionDecisionReason"]
    assert "private" not in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["Status update", "Review finished", "Question about the plan"])
async def test_plain_authority_chat_does_not_block(client, world, subject):
    await world.mail(subject=subject)
    assert _denied(await client.post(ROUTE, json=world.event())) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("subject", ["HOLD", "Please pause", "Halt now", "Approval revoked", "Work cancelled"])
async def test_stop_hold_keywords_from_the_leader_block(client, world, subject):
    await world.mail(subject=subject)
    assert _denied(await client.post(ROUTE, json=world.event())) is not None


@pytest.mark.asyncio
async def test_stop_message_from_a_peer_does_not_block(client, world):
    await world.mail(sender="peer", subject="STOP")
    assert _denied(await client.post(ROUTE, json=world.event())) is None


@pytest.mark.asyncio
async def test_bound_supervisor_stop_blocks(client, world):
    await world.mail(sender="supervisor", subject="STOP: #564 deferred")
    assert _denied(await client.post(ROUTE, json=world.event())) is not None


@pytest.mark.asyncio
async def test_a_stop_from_a_supervisor_bound_to_another_team_does_not_block(client, db, world):
    stranger = MailExternalActor(actor_key="other", display_name="Other", kind="supervisor", token_hash="y")
    db.add(stranger)
    await db.flush()
    message = MailMessage(
        kind="message", subject="STOP", body_markdown="b", sender_actor_id=stranger.id,
        sender_type="external_actor", recipient_member_id=world.builder.id,
        audience_type="member", audience_id=str(world.builder.id),
    )
    db.add(message)
    await db.flush()
    db.add(MailReceipt(message_id=message.id, member_id=world.builder.id))
    await db.commit()
    assert _denied(await client.post(ROUTE, json=world.event())) is None


@pytest.mark.asyncio
async def test_routine_system_notice_does_not_block(client, world):
    await world.mail(sender="system", subject="Dispatch label removed")
    await world.mail(sender="system", subject="Release needed: issue #564")
    assert _denied(await client.post(ROUTE, json=world.event())) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"subject": "STOP: operator hold"},
    {"subject": "Dispatch label removed", "payload": {"block_edits": True}},
])
async def test_system_notice_that_says_stop_or_flags_it_blocks(client, world, kwargs):
    await world.mail(sender="system", **kwargs)
    assert _denied(await client.post(ROUTE, json=world.event())) is not None


@pytest.mark.asyncio
async def test_a_members_own_stop_never_blocks_itself(client, db, world):
    db.add(MailAgentSession(
        member_id=world.leader.id, provider="claude-code", source="hook",
        session_key=f"cc:s2:team-slot:{world.leader.team_slot_id}",
    ))
    message = MailMessage(
        kind="message", subject="STOP all work", body_markdown="b", sender_member_id=world.leader.id,
        sender_type="member", audience_type="team", audience_id="1",
    )
    db.add(message)
    await db.flush()
    db.add(MailReceipt(message_id=message.id, member_id=world.leader.id))
    await db.commit()
    event = world.event(session_id="s2", team_slot_id=world.leader.team_slot_id)
    assert _denied(await client.post(ROUTE, json=event)) is None
    # The same broadcast does stop the builder it also reached.
    db.add(MailReceipt(message_id=message.id, member_id=world.builder.id))
    await db.commit()
    assert _denied(await client.post(ROUTE, json=world.event())) is not None


@pytest.mark.asyncio
async def test_a_flag_from_a_peer_is_not_authority(client, world):
    await world.mail(sender="peer", subject="note", payload={"block_edits": True})
    assert _denied(await client.post(ROUTE, json=world.event())) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_leader_decision_is_newer_authority_until_read(client, world, decision):
    message_id = await world.mail(subject="Re: plan", decision=decision)
    assert _denied(await client.post(ROUTE, json=world.event())) is not None
    await world.read(message_id)
    assert _denied(await client.post(ROUTE, json=world.event())) is None


@pytest.mark.asyncio
async def test_message_about_another_members_item_does_not_block(client, world):
    await world.mail(subject="STOP", payload={"work_item_id": world.other_item})
    assert _denied(await client.post(ROUTE, json=world.event())) is None
    await world.mail(subject="STOP", payload={"work_item_id": world.my_item})
    assert _denied(await client.post(ROUTE, json=world.event())) is not None


@pytest.mark.asyncio
async def test_message_to_another_member_does_not_block_this_member(client, world):
    await world.mail(subject="STOP", to=world.peer)
    assert _denied(await client.post(ROUTE, json=world.event())) is None


@pytest.mark.asyncio
async def test_unknown_session_and_non_team_events_are_allowed(client, world):
    await world.mail()
    unknown = world.event(session_id="nope")
    assert _denied(await client.post(ROUTE, json=unknown)) is None
    anonymous = world.event()
    del anonymous["team_slot_id"]
    assert _denied(await client.post(ROUTE, json=anonymous)) is None
    assert (await client.post(ROUTE, json={})).json() == {}


@pytest.mark.asyncio
async def test_closed_session_is_not_guarded(client, db, world):
    await world.mail()
    await db.execute(update(MailAgentSession).values(closed_at=datetime.utcnow()))
    await db.commit()
    assert _denied(await client.post(ROUTE, json=world.event())) is None


@pytest.mark.asyncio
async def test_a_lookup_failure_allows(client, world, monkeypatch):
    from app.services.agent_mail_service import agent_mail_service

    async def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    await world.mail()
    monkeypatch.setattr(agent_mail_service, "blocking_unread_authority", boom)
    response = await client.post(ROUTE, json=world.event())
    assert response.status_code == 200
    assert _denied(response) is None


# --- The shim: what a Claude Code session actually runs ------------------------------------


class _Backend:
    """A loopback stand-in for Synth City that records the pre-tool-use calls."""

    def __init__(self, status=200, body=None, delay=0.0):
        self.calls = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.calls.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                if delay:
                    import time
                    time.sleep(delay)
                payload = json.dumps(body if body is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _run_shim(tmp_path, tool, url, *, team="23", extra_env=None):
    environment = {"HOME": str(tmp_path), "PATH": os.defpath}
    if team is not None:
        environment["SYNTH_CITY_TEAM_SLOT_ID"] = team
    environment.update(extra_env or {})
    command = [sys.executable, str(SHIM)]
    if url is not None:
        command += ["--synth-city-url", url]
    return subprocess.run(
        command, input=json.dumps({"tool_name": tool, "session_id": "s1"}),
        env=environment, text=True, capture_output=True, timeout=10,
    )


def _closed_port_url():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


DENY = {"hookSpecificOutput": {
    "hookEventName": "PreToolUse", "permissionDecision": "deny",
    "permissionDecisionReason": "unread stop message 14745: read your inbox first",
}}


def test_shim_passes_a_denial_through(tmp_path):
    with _Backend(body=DENY) as backend:
        result = _run_shim(tmp_path, "Edit", backend.url)
    assert result.returncode == 0
    assert json.loads(result.stdout) == DENY
    path, sent = backend.calls[0]
    assert path == "/api/v1/agent-mail/hooks/pre-tool-use"
    assert sent["tool_name"] == "Edit" and sent["team_slot_id"] == 23 and sent["provider"] == "claude-code"


def test_shim_allows_silently_when_the_backend_allows(tmp_path):
    with _Backend(body={}) as backend:
        result = _run_shim(tmp_path, "Write", backend.url)
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert len(backend.calls) == 1


@pytest.mark.parametrize("tool", ["Read", "Grep", "Bash", MAIL + "synth_city_check_inbox", MAIL + "synth_city_get_work_item_context"])
def test_shim_never_asks_for_read_only_tools(tmp_path, tool):
    with _Backend(body=DENY) as backend:
        result = _run_shim(tmp_path, tool, backend.url)
    assert result.returncode == 0
    assert result.stdout == ""
    assert backend.calls == []


@pytest.mark.parametrize("team", [None, "", "0", "bad"])
def test_shim_ignores_non_team_sessions(tmp_path, team):
    with _Backend(body=DENY) as backend:
        result = _run_shim(tmp_path, "Edit", backend.url, team=team)
    assert result.returncode == 0
    assert result.stdout == ""
    assert backend.calls == []


def _assert_fail_open_with_warning(result):
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert "hookSpecificOutput" not in output
    assert "unread stop" in output["systemMessage"].lower() or "guard" in output["systemMessage"].lower()
    assert "not checked" in output["systemMessage"].lower()


def test_shim_fails_open_with_a_warning_when_the_backend_is_down(tmp_path):
    _assert_fail_open_with_warning(_run_shim(tmp_path, "Edit", _closed_port_url()))


def test_shim_fails_open_with_a_warning_on_a_server_error(tmp_path):
    with _Backend(status=500, body={"detail": "boom"}) as backend:
        _assert_fail_open_with_warning(_run_shim(tmp_path, "Edit", backend.url))


def test_shim_fails_open_with_a_warning_on_a_bad_response(tmp_path):
    with _Backend(status=200, body=["not", "an", "object"]) as backend:
        _assert_fail_open_with_warning(_run_shim(tmp_path, "Edit", backend.url))


def test_shim_fails_open_with_a_warning_on_a_timeout(tmp_path):
    with _Backend(body=DENY, delay=2.0) as backend:
        result = _run_shim(tmp_path, "Edit", backend.url)
    _assert_fail_open_with_warning(result)


def test_shim_uses_the_environment_url_when_no_argument_is_given(tmp_path):
    with _Backend(body=DENY) as backend:
        result = _run_shim(tmp_path, "Edit", None, extra_env={"SYNTH_CITY_URL": backend.url})
    assert json.loads(result.stdout) == DENY


def test_shim_still_refuses_the_question_tool_without_the_backend(tmp_path):
    result = _run_shim(tmp_path, "AskUserQuestion", _closed_port_url())
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "Agent Mail" in output["permissionDecisionReason"]

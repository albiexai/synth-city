"""Agent Mail for Antigravity CLI (#736), from the behaviour #952 and #736 P1 observed on agy 1.5.9.

- `~/.gemini/config/mcp_config.json` loads a plain stdio entry: `{command, args, env}`.
- `~/.gemini/config/hooks.json` takes named groups. Events without a matcher take flat command
  entries; `PreToolUse` takes `{"matcher": ..., "hooks": [...]}`.
- agy rejects Claude-style hook output, but a `PreToolUse` hook printing
  `{"decision": "deny", "reason": ...}` blocks the tool call.
"""
import asyncio
import importlib.util
import io
import json
import logging
from pathlib import Path
import re

import httpx
import pytest

from app.services import agent_mail_install_service as svc


@pytest.fixture
def gemini(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(svc, "antigravity_cli_available", lambda: True)
    scopes = []

    async def backup(db, scope):
        scopes.append(scope)

    monkeypatch.setattr(svc, "_backup_before_mutation", backup)
    config = tmp_path / ".gemini" / "config"
    return config / "mcp_config.json", config / "hooks.json", scopes


@pytest.mark.asyncio
async def test_install_keeps_other_servers_and_groups_and_uninstall_removes_only_ours(gemini):
    mcp, hooks, scopes = gemini
    mcp.parent.mkdir(parents=True)
    mine = {"command": "/usr/bin/other", "args": []}
    other_group = {"my-linter": {"PostToolUse": [{"matcher": "run_command", "hooks": [
        {"type": "command", "command": "./lint.sh", "timeout": 10}]}]}}
    mcp.write_text(json.dumps({"mcpServers": {"mine": mine}}))
    hooks.write_text(json.dumps(other_group))
    status = await svc.apply_antigravity_install(None)
    servers = json.loads(mcp.read_text())["mcpServers"]
    assert servers["mine"] == mine
    entry = servers["synth-city-mail"]
    # The plain shape agy loaded in #736 P1: no "type" or "transport".
    assert set(entry) == {"command", "args", "env"}
    assert entry["env"]["SYNTH_CITY_PROVIDER"] == "antigravity-cli"
    doc = json.loads(hooks.read_text())
    assert doc["my-linter"] == other_group["my-linter"]
    assert status.antigravity_mcp_installed and status.antigravity_hooks_missing == []
    assert sorted(status.antigravity_hooks) == ["PreInvocation", "PreToolUse", "SessionStart"]
    assert status.antigravity_mcp_config_path == str(mcp) and status.antigravity_hooks_path == str(hooks)
    await svc.apply_antigravity_install(None)  # idempotent
    assert json.loads(hooks.read_text()) == doc
    await svc.uninstall_antigravity(None)
    assert json.loads(mcp.read_text()) == {"mcpServers": {"mine": mine}}
    assert json.loads(hooks.read_text()) == other_group
    assert scopes == ["antigravity", "antigravity", "antigravity"]


@pytest.mark.asyncio
async def test_hooks_use_the_observed_layout(gemini):
    _mcp, hooks, _scopes = gemini
    await svc.apply_antigravity_install(None)
    doc = json.loads(hooks.read_text())
    session = doc["albiex-synth-city-mail-session-start"]["SessionStart"]
    turn = doc["albiex-synth-city-mail-pre-invocation"]["PreInvocation"]
    question = doc["albiex-synth-city-mail-question"]["PreToolUse"]
    # Events without a matcher take flat entries; a {"hooks": [...]} wrapper there is rejected by agy.
    for entries, slug in ((session, "session-start"), (turn, "user-prompt-submit")):
        assert len(entries) == 1 and entries[0]["type"] == "command" and "hooks" not in entries[0]
        assert f"--event {slug}" in entries[0]["command"]
        assert "--provider antigravity-cli" in entries[0]["command"]
    assert question[0]["matcher"] == "ask_question"
    assert "--event pre-tool-use" in question[0]["hooks"][0]["command"]


@pytest.mark.asyncio
async def test_empty_or_missing_files_mean_no_servers_and_no_groups(gemini):
    mcp, hooks, _scopes = gemini
    mcp.parent.mkdir(parents=True)
    mcp.write_text("")
    await svc.apply_antigravity_install(None)
    assert list(json.loads(mcp.read_text())["mcpServers"]) == ["synth-city-mail"]
    assert hooks.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,content", [
    ("mcp_config.json", "{not json"),
    ("mcp_config.json", "[]"),
    ("mcp_config.json", '{"mcpServers": []}'),
    ("hooks.json", "{not json"),
    ("hooks.json", "[]"),
    ("hooks.json", '{"group": []}'),
])
async def test_malformed_files_are_refused_and_not_rewritten(gemini, name, content):
    mcp, hooks, scopes = gemini
    mcp.parent.mkdir(parents=True)
    target = mcp if name == "mcp_config.json" else hooks
    target.write_text(content)
    with pytest.raises(ValueError):
        await svc.apply_antigravity_install(None)
    assert target.read_text() == content
    assert scopes == []


@pytest.mark.asyncio
async def test_install_needs_the_agy_binary(gemini, monkeypatch):
    monkeypatch.setattr(svc, "antigravity_cli_available", lambda: False)
    with pytest.raises(ValueError):
        await svc.apply_antigravity_install(None)


def test_status_is_not_installed_without_files(gemini):
    assert not svc.antigravity_mcp_installed()
    assert svc.installed_antigravity_hooks() == []


def test_hook_session_key_uses_the_antigravity_prefix():
    from app.api.v1.agent_mail import _hook_provider, _hook_session_key
    payload = {"provider": "antigravity-cli", "session_id": "f186e5b5-90ce-4d7b-8367-41b199160657"}
    assert _hook_provider(payload) == "antigravity-cli"
    assert _hook_session_key(payload) == "antigravity:f186e5b5-90ce-4d7b-8367-41b199160657"


def test_route_names_the_antigravity_tool_and_logs_no_question_content(caplog):
    from app.api.v1.agent_mail import _hook_tool_name, _record_antigravity_question_denied
    payload = {"provider": "antigravity-cli", "conversationId": "c-1", "team_slot_id": 7,
               "toolCall": {"name": "ask_question", "args": {"questions": [{"question": "secret plan?"}]}}}
    assert _hook_tool_name(payload) == "ask_question"
    assert _hook_tool_name({"provider": "claude-code", "tool_name": "Edit"}) == "Edit"
    assert _hook_tool_name({"provider": "antigravity-cli", "toolCall": "bad"}) is None
    with caplog.at_level(logging.INFO):
        _record_antigravity_question_denied(payload)
    assert "antigravity_question_denied" in caplog.text
    assert "secret plan" not in caplog.text


# --- Hook shim: the question deny is local and does not depend on HTTP -------------------------

QUESTION = {"hookEventName": "PreToolUse", "conversationId": "c-1",
            "toolCall": {"name": "ask_question", "id": "t-1",
                         "args": {"questions": [{"question": "secret plan?", "options": ["A", "B"]}]}}}


def run_hook(monkeypatch, payload, *, event="pre-tool-use", slot="7", post=None):
    from mcp_shim import agent_mail_hook as hook
    monkeypatch.setattr("sys.argv", ["agent_mail_hook.py", "--provider", "antigravity-cli", "--event", event,
                                     "--synth-city-url", "http://127.0.0.1:9"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload) if not isinstance(payload, str) else payload))
    if slot is None:
        monkeypatch.delenv("SYNTH_CITY_TEAM_SLOT_ID", raising=False)
    else:
        monkeypatch.setenv("SYNTH_CITY_TEAM_SLOT_ID", slot)
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append(json)
        return post(url) if post else httpx.Response(200, json={}, request=httpx.Request("POST", url))

    monkeypatch.setattr(hook.httpx, "post", fake_post)
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    assert hook.main() == 0
    return out.getvalue().strip(), sent


def _raise(exc):
    def post(url):
        raise exc
    return post


@pytest.mark.parametrize("post", [
    _raise(httpx.ConnectError("refused")),
    _raise(httpx.ReadTimeout("slow")),
    lambda url: httpx.Response(500, request=httpx.Request("POST", url)),
    lambda url: httpx.Response(200, text="not json", request=httpx.Request("POST", url)),
    lambda url: httpx.Response(200, json={"guard": "allow"}, request=httpx.Request("POST", url)),
    lambda url: httpx.Response(200, json={}, request=httpx.Request("POST", url)),
])
def test_team_member_question_is_denied_whatever_the_backend_does(monkeypatch, post):
    out, _sent = run_hook(monkeypatch, QUESTION, post=post)
    body = json.loads(out)
    # The flat shape agy accepted in #736 P1, never Claude's hookSpecificOutput.
    assert set(body) == {"decision", "reason"} and body["decision"] == "deny"
    assert "Agent Mail" in body["reason"]


def test_question_content_is_not_forwarded(monkeypatch):
    _out, sent = run_hook(monkeypatch, QUESTION)
    assert sent and "secret plan" not in json.dumps(sent)
    assert sent[0]["toolCall"] == {"name": "ask_question"}


@pytest.mark.parametrize("payload,slot", [
    (QUESTION, None),                       # a user's own agy session: no team marker
    (QUESTION, "not-a-number"),             # malformed marker
    (QUESTION, "0"),
    ({**QUESTION, "toolCall": {"name": "write_file"}}, "7"),
    ({**QUESTION, "toolCall": "bad"}, "7"),
    ({k: v for k, v in QUESTION.items() if k != "toolCall"}, "7"),
    ("not json", "7"),
])
def test_other_cases_print_nothing(monkeypatch, payload, slot):
    out, _sent = run_hook(monkeypatch, payload, slot=slot)
    assert out == ""


@pytest.mark.parametrize("event", ["session-start", "user-prompt-submit"])
def test_lifecycle_hooks_print_nothing_even_when_the_backend_answers(monkeypatch, event):
    answer = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "Check your inbox."}}
    out, _sent = run_hook(monkeypatch, {"conversationId": "c-1"}, event=event,
                          post=lambda url: httpx.Response(200, json=answer, request=httpx.Request("POST", url)))
    # agy rejects Claude-style output (#736 P1), so nothing is printed.
    assert out == ""


# --- V1 review 5918275928 corrections --------------------------------------------------------

USER_ENTRY = {"type": "command", "command": "/usr/bin/my-session-logger", "timeout": 3}


@pytest.mark.asyncio
async def test_user_entries_inside_synth_city_groups_are_kept(gemini):
    _mcp, hooks, _scopes = gemini
    hooks.parent.mkdir(parents=True)
    user_question = {"matcher": "write_file", "hooks": [dict(USER_ENTRY)]}
    hooks.write_text(json.dumps({
        "albiex-synth-city-mail-session-start": {"SessionStart": [dict(USER_ENTRY)], "Stop": [dict(USER_ENTRY)]},
        "albiex-synth-city-mail-question": {"PreToolUse": [user_question]},
    }))
    await svc.apply_antigravity_install(None)
    await svc.apply_antigravity_install(None)  # idempotent: Synth City's entries are not duplicated
    doc = json.loads(hooks.read_text())
    session = doc["albiex-synth-city-mail-session-start"]
    assert session["Stop"] == [USER_ENTRY]
    assert session["SessionStart"][0] == USER_ENTRY and len(session["SessionStart"]) == 2
    question = doc["albiex-synth-city-mail-question"]["PreToolUse"]
    assert question[0] == user_question and len(question) == 2
    assert sorted(svc.installed_antigravity_hooks()) == ["PreInvocation", "PreToolUse", "SessionStart"]
    await svc.uninstall_antigravity(None)
    assert json.loads(hooks.read_text()) == {
        "albiex-synth-city-mail-session-start": {"SessionStart": [USER_ENTRY], "Stop": [USER_ENTRY]},
        "albiex-synth-city-mail-question": {"PreToolUse": [user_question]},
    }


@pytest.mark.asyncio
async def test_user_hook_inside_the_question_matcher_group_is_kept(gemini):
    _mcp, hooks, _scopes = gemini
    await svc.apply_antigravity_install(None)
    doc = json.loads(hooks.read_text())
    doc["albiex-synth-city-mail-question"]["PreToolUse"][0]["hooks"].append(dict(USER_ENTRY))
    hooks.write_text(json.dumps(doc))
    await svc.apply_antigravity_install(None)
    groups = json.loads(hooks.read_text())["albiex-synth-city-mail-question"]["PreToolUse"]
    assert sum(USER_ENTRY in group.get("hooks", []) for group in groups) == 1
    assert "PreToolUse" in svc.installed_antigravity_hooks()
    await svc.uninstall_antigravity(None)
    assert json.loads(hooks.read_text()) == {
        "albiex-synth-city-mail-question": {"PreToolUse": [{"matcher": "ask_question", "hooks": [USER_ENTRY]}]}}


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    {"albiex-synth-city-mail-session-start": {"SessionStart": "not a list"}},
    {"albiex-synth-city-mail-question": {"PreToolUse": {"matcher": "ask_question"}}},
    {"albiex-synth-city-mail-question": {"PreToolUse": [{"matcher": "ask_question", "hooks": "x"}]}},
])
async def test_unsafe_shapes_in_synth_city_groups_are_refused_before_writing(gemini, content):
    mcp, hooks, scopes = gemini
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        await svc.apply_antigravity_install(None)
    with pytest.raises(ValueError):
        await svc.uninstall_antigravity(None)
    assert json.loads(hooks.read_text()) == content and not mcp.exists() and scopes == []


@pytest.mark.parametrize("entry", [
    {"command": "python3", "args": None},
    {"command": "python3", "args": "agent_mail_server.py"},
    {"command": "python3", "args": 7},
    "not an object",
    None,
])
def test_malformed_mcp_entry_is_not_installed_and_does_not_raise(gemini, entry):
    mcp, _hooks, _scopes = gemini
    mcp.parent.mkdir(parents=True)
    mcp.write_text(json.dumps({"mcpServers": {"albiex-synth-city-mail": entry}}))
    assert svc.antigravity_mcp_installed() is False


@pytest.mark.parametrize("slot,logged", [(7, True), (None, False), (0, False), (-1, False), ("x", False)])
def test_question_denial_is_logged_only_for_a_team_member(caplog, slot, logged):
    import asyncio
    from app.api.v1.agent_mail import hook_pre_tool_use
    payload = {"provider": "antigravity-cli", "conversationId": "c-1", "toolCall": {"name": "ask_question"}}
    if slot is not None:
        payload["team_slot_id"] = slot
    with caplog.at_level(logging.INFO):
        assert asyncio.run(hook_pre_tool_use(None, payload, None)) == {"guard": "allow"}
    assert ("antigravity_question_denied" in caplog.text) is logged


# --- V1 review 5918454086: presence needs a Synth City command; other groups are kept as they are ---

EMPTY_QUESTION = {"matcher": "ask_question", "hooks": []}


def test_empty_question_group_alone_is_not_installed(gemini):
    _mcp, hooks, _scopes = gemini
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps({"albiex-synth-city-mail-question": {"PreToolUse": [dict(EMPTY_QUESTION)]},
                                 "albiex-synth-city-mail-session-start": {"SessionStart": [dict(USER_ENTRY)]}}))
    assert svc.installed_antigravity_hooks() == []


@pytest.mark.asyncio
async def test_empty_question_group_is_kept_by_install_and_uninstall(gemini):
    _mcp, hooks, _scopes = gemini
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps({"albiex-synth-city-mail-question": {"PreToolUse": [dict(EMPTY_QUESTION)]}}))
    await svc.apply_antigravity_install(None)
    await svc.apply_antigravity_install(None)
    groups = json.loads(hooks.read_text())["albiex-synth-city-mail-question"]["PreToolUse"]
    assert groups[0] == EMPTY_QUESTION and len(groups) == 2
    assert "PreToolUse" in svc.installed_antigravity_hooks()
    await svc.uninstall_antigravity(None)
    assert json.loads(hooks.read_text()) == {"albiex-synth-city-mail-question": {"PreToolUse": [EMPTY_QUESTION]}}


# --- #991: agy-safe server key ----------------------------------------------------------------

AGY_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")  # agy 1.5.9 rejects any other mcp_<server>_<tool>
OLD_KEY, NEW_KEY = "albiex-synth-city-mail", "synth-city-mail"


def _shim_tools(monkeypatch):
    monkeypatch.setenv("SYNTH_CITY_PROVIDER", "antigravity-cli")
    location = Path(__file__).resolve().parents[2] / "mcp_shim/agent_mail_server.py"
    spec = importlib.util.spec_from_file_location("antigravity_name_check", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return [tool.name for tool in asyncio.run(module.mcp.list_tools())]


def test_every_mail_tool_name_fits_the_agy_limit(monkeypatch):
    assert svc.ANTIGRAVITY_MCP_SERVER_KEY == NEW_KEY
    names = _shim_tools(monkeypatch)
    assert len(names) >= 47
    too_long = [name for name in names if not AGY_TOOL_NAME.fullmatch(f"mcp_{NEW_KEY}_{name}")]
    assert too_long == []
    # The shared key that agy reported as invalid for four tools (#980 P1) would not fit.
    assert [n for n in names if not AGY_TOOL_NAME.fullmatch(f"mcp_{OLD_KEY}_{n}")]


def _ours():
    return {"command": "/venv/bin/python", "args": [svc.shim_path()], "env": {"SYNTH_CITY_PROVIDER": "antigravity-cli"}}


USER_SERVER = {"command": "/usr/bin/someone-else", "args": ["--serve"]}


@pytest.mark.asyncio
async def test_install_migrates_only_our_old_entry(gemini):
    mcp, _hooks, _scopes = gemini
    mcp.parent.mkdir(parents=True)
    mcp.write_text(json.dumps({"mcpServers": {OLD_KEY: _ours(), "mine": USER_SERVER}}))
    status = await svc.apply_antigravity_install(None)
    servers = json.loads(mcp.read_text())["mcpServers"]
    assert OLD_KEY not in servers and set(servers) == {NEW_KEY, "mine"}
    assert servers["mine"] == USER_SERVER and status.antigravity_mcp_installed


@pytest.mark.asyncio
async def test_install_keeps_a_users_entry_under_the_old_key(gemini):
    mcp, _hooks, _scopes = gemini
    mcp.parent.mkdir(parents=True)
    mcp.write_text(json.dumps({"mcpServers": {OLD_KEY: USER_SERVER}}))
    await svc.apply_antigravity_install(None)
    servers = json.loads(mcp.read_text())["mcpServers"]
    assert servers[OLD_KEY] == USER_SERVER and NEW_KEY in servers


@pytest.mark.asyncio
async def test_install_refuses_a_users_entry_under_the_new_key_without_writing(gemini):
    mcp, hooks, scopes = gemini
    mcp.parent.mkdir(parents=True)
    original = json.dumps({"mcpServers": {NEW_KEY: USER_SERVER}})
    mcp.write_text(original)
    with pytest.raises(ValueError):
        await svc.apply_antigravity_install(None)
    assert mcp.read_text() == original and not hooks.exists() and scopes == []


@pytest.mark.parametrize("servers,installed", [
    ({NEW_KEY: "ours"}, True),
    ({OLD_KEY: "ours"}, True),
    ({NEW_KEY: "user"}, False),
    ({OLD_KEY: "user"}, False),
    ({}, False),
])
def test_status_reads_both_keys_and_only_our_entries(gemini, servers, installed):
    mcp, _hooks, _scopes = gemini
    mcp.parent.mkdir(parents=True)
    doc = {key: (_ours() if who == "ours" else USER_SERVER) for key, who in servers.items()}
    mcp.write_text(json.dumps({"mcpServers": doc}))
    assert svc.antigravity_mcp_installed() is installed


@pytest.mark.asyncio
@pytest.mark.parametrize("old_owner", ["ours", "user"])
async def test_uninstall_removes_our_entries_under_both_keys_only(gemini, old_owner):
    mcp, _hooks, _scopes = gemini
    mcp.parent.mkdir(parents=True)
    old = _ours() if old_owner == "ours" else USER_SERVER
    mcp.write_text(json.dumps({"mcpServers": {OLD_KEY: old, NEW_KEY: _ours(), "mine": USER_SERVER}}))
    await svc.uninstall_antigravity(None)
    servers = json.loads(mcp.read_text())["mcpServers"]
    expected = {"mine": USER_SERVER} if old_owner == "ours" else {OLD_KEY: USER_SERVER, "mine": USER_SERVER}
    assert servers == expected

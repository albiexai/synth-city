import pytest

from app.services.agent_mail_service import TMUX_WAKE_PROVIDERS
from app.services.providers import get_provider
from app.services.providers.base import ProviderLaunchError, SpawnCommandOptions
from app.services.providers.antigravity_cli import exact_resumed_conversation, is_agy_executable
from app.services.providers.capabilities import capability_flags, normalize_capability_matrix
from app.services.providers.launch_contract import (
    MODEL_EXAMPLES,
    launch_modes_for,
    option_keys_for,
    reasoning_efforts_for,
    supports_bedrock,
)

# Conversation ids are UUIDs (`conversations/<uuid>.db`; observed live on agy 1.5.9, #952), so
# anything else is refused rather than passed to argv.
CID = "6f1c2b8e-4d3a-4b7f-9c2e-1a2b3c4d5e6f"


def build(**kwargs):
    return get_provider("antigravity-cli").build_spawn_command(SpawnCommandOptions(directory="/w", **kwargs))


def test_registry_and_contract():
    provider = get_provider("antigravity-cli")
    assert (provider.display_name, provider.binary_name) == ("Antigravity CLI", "agy")
    assert launch_modes_for("antigravity-cli") == ("plain", "resume")
    assert reasoning_efforts_for("antigravity-cli") == ("low", "medium", "high", "xhigh", "max")
    assert set(option_keys_for("antigravity-cli")) == {
        "model", "reasoning_effort", "skip_permissions", "prompt", "session_id"}
    assert not supports_bedrock("antigravity-cli")
    # No official page names model ids, so no example is invented.
    assert "antigravity-cli" not in MODEL_EXAMPLES


def test_capabilities_claim_only_what_this_provider_implements():
    flags = capability_flags("antigravity-cli")
    assert flags["spawn"] and flags["resume"]
    assert not any(flags[key] for key in ("mcp", "hooks", "permissions", "config"))
    # Conversation state is observed read-only from the member's own files (#737).
    assert normalize_capability_matrix("antigravity-cli")["sessions"]["state"] == "read_only"
    matrix = normalize_capability_matrix("antigravity-cli")
    assert matrix["hooks"]["state"] == "unknown"
    assert matrix["permissions"]["state"] == "unknown"


def test_tmux_wake_recognises_antigravity():
    assert "antigravity-cli" in TMUX_WAKE_PROVIDERS


def test_plain_launch():
    assert build(model="m-1", reasoning_effort="high", skip_permissions=True, prompt="Hello") == [
        "agy", "--model", "m-1", "--effort", "high", "--dangerously-skip-permissions", "-i", "Hello"]
    assert build() == ["agy"]


def test_resume_launch():
    assert build(mode="resume", session_id=CID, model="m-1", reasoning_effort="max") == [
        "agy", "--model", "m-1", "--effort", "max", "--conversation", CID]
    assert build(mode="resume", session_id=CID.upper()) == ["agy", "--conversation", CID.upper()]


def test_resume_launch_takes_a_prompt():
    # Observed on agy 1.5.9 (#952): `--conversation <id> -i <prompt>` keeps the id and its history.
    assert build(mode="resume", session_id=CID, prompt="Continue") == [
        "agy", "--conversation", CID, "-i", "Continue"]
    assert build(mode="resume", session_id=CID, model="m-1", skip_permissions=True, prompt="Go on") == [
        "agy", "--model", "m-1", "--dangerously-skip-permissions", "--conversation", CID, "-i", "Go on"]


@pytest.mark.parametrize("session_id", [
    "last", "abc", "", "-x", f"{CID}x", f"{CID} --model y", CID[:-1], "ses_e2e111c0dffe5XAqhRWBFFBZDf",
])
def test_resume_refuses_a_non_uuid_conversation_id(session_id):
    with pytest.raises(ProviderLaunchError) as error:
        build(mode="resume", session_id=session_id)
    assert error.value.block_code == "antigravity_session_invalid"


@pytest.mark.parametrize("kwargs,code", [
    ({"mode": "resume"}, "antigravity_session_invalid"),
    ({"session_id": CID}, "antigravity_session_invalid"),
    ({"mode": "resume", "session_id": CID, "prompt": "-rf"}, "antigravity_literal_prompt_required"),
    ({"mode": "worktree"}, "unsupported_launch_mode"),
    ({"mode": "fork"}, "unsupported_launch_mode"),
    ({"reasoning_effort": "ultra"}, "invalid_reasoning_effort"),
    ({"reasoning_effort": "none"}, "invalid_reasoning_effort"),
    ({"model": "bad\nmodel"}, "invalid_model"),
    ({"model": "two words"}, "invalid_model"),
    ({"model": " "}, "invalid_model"),
    ({"model": "-x"}, "invalid_model"),
    ({"platform": "bedrock"}, "antigravity_platform_unsupported"),
    ({"bedrock_model": "x"}, "antigravity_option_unsupported"),
    ({"aws_region": "us-east-1"}, "antigravity_option_unsupported"),
    ({"profile": "p"}, "antigravity_option_unsupported"),
    ({"sandbox": "workspace-write"}, "antigravity_option_unsupported"),
    ({"agent": "a"}, "antigravity_option_unsupported"),
    ({"plan": True}, "antigravity_option_unsupported"),
    ({"no_ask_user": True}, "antigravity_option_unsupported"),
    ({"allow_all": True}, "antigravity_option_unsupported"),
    ({"use_last": True}, "antigravity_option_unsupported"),
    ({"worktree_name": "w"}, "antigravity_option_unsupported"),
    ({"project_folder": "p"}, "antigravity_option_unsupported"),
    ({"dangerously_bypass_approvals_and_sandbox": True}, "antigravity_option_unsupported"),
    ({"prompt": "-rf"}, "antigravity_literal_prompt_required"),
])
def test_refusals(kwargs, code):
    with pytest.raises(ProviderLaunchError) as error:
        build(**kwargs)
    assert error.value.block_code == code


@pytest.mark.parametrize("platform", ["", "anthropic", "google"])
def test_neutral_platform_values_are_accepted(platform):
    assert build(platform=platform) == ["agy"]


@pytest.mark.parametrize("command,match", [
    ("agy", True),
    ("agy --conversation x", True),
    ("/home/u/.local/bin/agy -i hi", True),
    ("agyx", False),
    ("gemini", False),
    ("python3 agy.py", False),
])
def test_process_match(command, match):
    assert get_provider("antigravity-cli").is_process_match(command, "1") is match


@pytest.mark.parametrize("argv0,exact", [
    ("agy", True), ("/home/u/.local/bin/agy", True),
    ("agy arbitrary-file", False), ("/tmp/agy arbitrary-file", False), ("agyx", False), ("agy-old", False),
    ("antigravity-cli", False), ("", False), ("/tmp/agy/", False),
])
def test_agy_executable_is_the_exact_basename(argv0, exact):
    assert is_agy_executable(argv0) is exact


@pytest.mark.parametrize("model", [None, "m-1"])
@pytest.mark.parametrize("effort", [None, "low", "max"])
@pytest.mark.parametrize("skip", [False, True])
@pytest.mark.parametrize("prompt", [None, "Continue", "two words"])
def test_every_built_resume_command_is_recognized_with_its_id(model, effort, skip, prompt):
    command = build(mode="resume", session_id=CID, model=model, reasoning_effort=effort, skip_permissions=skip,
                    prompt=prompt)
    assert exact_resumed_conversation(command) == CID
    assert exact_resumed_conversation(["/home/u/.local/bin/agy", *command[1:]]) == CID


@pytest.mark.parametrize("argv", [
    [], ["agy"], ["agy", "--conversation"], ["agy", "-i", "hi"],
    ["agy", "--conversation", CID, "--conversation", CID],
    ["agy", "--", "--conversation", CID],
    ["agy", "-i", "--conversation", CID],
    ["agy", "--model", "--conversation", CID],
    ["agy", f"--conversation={CID}"],
    ["agy", "--conversation", CID, "extra"],
    ["agy", "--conversation", CID, "-i"],
    ["agy", "--conversation", CID, "-i", ""],
    ["agy", "--model", "m-1", "--conversation", CID, "-i", ""],
    ["agy", "--conversation", CID, "-i", "-x"],
    ["agy", "--conversation", CID, "-i", "hi", "extra"],
    ["agy", "--conversation", CID, "-i", "hi", "-i", "again"],
    ["agy", "--conversation", CID, "-p", "hi"],
    ["agy", "-i", "hi", "--conversation", CID],
    ["agy", "--conversation", "last"],
    ["agy", "--dangerously-skip-permissions", "--model", "m-1", "--conversation", CID],
    ["agy", "--effort", "high", "--model", "m-1", "--conversation", CID],
    ["agy", "--model", "m-1", "--model", "m-2", "--conversation", CID],
    ["agy", "--dangerously-skip-permissions", "--dangerously-skip-permissions", "--conversation", CID],
    ["agy", "--effort", "ultra", "--conversation", CID],
    ["agy", "--model", "-x", "--conversation", CID],
    ["agy", "--model", "two words", "--conversation", CID],
    ["/tmp/agy arbitrary-file", "--conversation", CID],
    ["antigravity-cli", "--conversation", CID],
])
def test_anything_but_the_built_resume_form_is_not_a_resumed_conversation(argv):
    assert exact_resumed_conversation(argv) is None


@pytest.mark.parametrize("cmdline,accepted", [
    (b"agy\0--conversation\0" + CID.encode() + b"\0-i\0Continue\0", True),
    (b"agy\0--conversation\0" + CID.encode() + b"\0-i\0two words\0", True),
    # The builder omits -i for an empty prompt, so an empty prompt argument is not this member (V1 review).
    (b"agy\0--conversation\0" + CID.encode() + b"\0-i\0\0", False),
    (b"agy\0--conversation\0" + CID.encode() + b"\0-i\0-x\0", False),
    (b"agy\0--conversation\0" + CID.encode() + b"\0-i\0Continue\0extra\0", False),
])
def test_owner_identity_reads_the_resume_prompt_form_from_the_process_cmdline(monkeypatch, cmdline, accepted):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from app.services import owner_observation_pause as pause
    monkeypatch.setattr(pause.activity, "_process", lambda _pid: ("S", "fixture-start"))
    monkeypatch.setattr(pause.activity, "_process_started_at", lambda _start: datetime.now(timezone.utc) - timedelta(minutes=2))
    # A fake cmdline reader: no host /proc entry is read.
    monkeypatch.setattr(pause, "Path", lambda _path: SimpleNamespace(read_bytes=lambda: cmdline))
    slot = SimpleNamespace(provider="antigravity-cli", launch_options={"session_id": CID})
    session = SimpleNamespace(pid=12, created_at=datetime.utcnow(), cwd="/tmp/fixture")
    workspace = SimpleNamespace(leased_owner_pid=11, leased_owner_proc_start="fixture-start")
    if accepted:
        assert pause._process_identity(slot, session, workspace) == "fixture-start"
    else:
        with pytest.raises(ValueError, match="owner_native_identity_changed"):
            pause._process_identity(slot, session, workspace)

import pytest

from app.services.agent_mail_service import TMUX_WAKE_PROVIDERS
from app.services.providers import get_provider
from app.services.providers.base import ProviderLaunchError, SpawnCommandOptions
from app.services.providers.capabilities import capability_flags, normalize_capability_matrix
from app.services.providers.launch_contract import (
    MODEL_EXAMPLES,
    launch_modes_for,
    option_keys_for,
    reasoning_efforts_for,
    supports_bedrock,
)

# Conversation ids are UUIDs on agy 1.3.5 (`conversations/<uuid>.db`, #734); the format is not
# proven by a live run (#952), so anything else is refused rather than passed to argv.
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
    assert not any(flags[key] for key in ("mcp", "hooks", "permissions", "sessions", "config"))
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
    # The resume prompt rule is not proven (#952), so a resumed member gets no prompt.
    ({"mode": "resume", "session_id": CID, "prompt": "Continue"}, "antigravity_option_unsupported"),
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

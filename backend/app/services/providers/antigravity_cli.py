"""Antigravity CLI provider (Google, binary ``agy``).

Built from ``docs/superpowers/specs/2026-11-15-antigravity-cli-harness-design.md``. #952 observed
agy 1.5.9 in an isolated runtime; anything still unproven is refused rather than guessed.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path

from app.services.providers.base import AgentProvider, ProviderLaunchError, SpawnCommandOptions, argv0_name
from app.services.providers.launch_contract import ANTIGRAVITY_EFFORTS

# Conversation ids are UUIDs (`conversations/<uuid>.db`; observed live on agy 1.5.9, #952).
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def antigravity_home() -> Path:
    return Path("~/.gemini").expanduser()


# The Director's human trial on #995 recorded that completing onboarding writes this file with
# "onboardingComplete": true. "consumerOnboardingComplete" may come from a sign-in instead, and the
# sign-in files are never read. Synth City only reads this file; it never writes or presets it.
_ONBOARDING_SIGNAL_MAX_BYTES = 65536


def antigravity_onboarding_signal_path() -> Path:
    return antigravity_home() / "antigravity-cli" / "cache" / "onboarding.json"


def antigravity_onboarding_complete() -> bool:
    """True only for a regular file holding a JSON object whose onboardingComplete is exactly true.

    Anything else (missing, unreadable, a symlink, too large, malformed, false) means onboarding is not
    completed: the signal lives under cache/, so its absence is never an error.
    """
    try:
        fd = os.open(antigravity_onboarding_signal_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _ONBOARDING_SIGNAL_MAX_BYTES:
            return False
        data = os.read(fd, _ONBOARDING_SIGNAL_MAX_BYTES + 1)
    except OSError:
        return False
    finally:
        os.close(fd)
    if len(data) > _ONBOARDING_SIGNAL_MAX_BYTES:
        return False
    try:
        document = json.loads(data)
    except (ValueError, RecursionError):  # a deeply nested document under the size cap recurses
        return False
    return isinstance(document, dict) and document.get("onboardingComplete") is True


def is_antigravity_command(command: str) -> bool:
    return argv0_name(command) == "agy"


def is_agy_executable(argv0: str) -> bool:
    """argv[0] names the agy binary itself; it is never split on whitespace."""
    return argv0.rsplit("/", 1)[-1] == "agy"


def _valid_model(model: str) -> bool:
    return bool(model) and not model.startswith("-") and not any(character in model for character in "\n\r\x00 ")


def exact_resumed_conversation(argv: Sequence[str]) -> str | None:
    """The conversation id of an argv that is exactly this provider's resume command, else None.

    Only the form built below is accepted, in its order:
    ``agy [--model <m>] [--effort <tier>] [--dangerously-skip-permissions] --conversation <uuid> [-i <prompt>]``.
    """
    argv = list(argv)
    # The builder adds `-i` only for a nonempty prompt that does not start with "-".
    if len(argv) >= 5 and argv[-2] == "-i" and argv[-1] and not argv[-1].startswith("-"):
        argv = argv[:-2]
    if len(argv) < 3 or not is_agy_executable(argv[0]) or argv[-2] != "--conversation":
        return None
    options, conversation = argv[1:-2], argv[-1]
    if not _UUID.fullmatch(conversation):
        return None
    if options[:1] == ["--model"]:
        if len(options) < 2 or not _valid_model(options[1]):
            return None
        options = options[2:]
    if options[:1] == ["--effort"]:
        if len(options) < 2 or options[1] not in ANTIGRAVITY_EFFORTS:
            return None
        options = options[2:]
    if options[:1] == ["--dangerously-skip-permissions"]:
        options = options[1:]
    return conversation if not options else None


class AntigravityCliProvider(AgentProvider):
    id = "antigravity-cli"
    display_name = "Antigravity CLI"
    binary_name = "agy"

    def get_config_paths(self, project_path: str | None = None) -> dict:
        # Documented locations (#734 spec); Albiex Synth City reads none of them yet.
        root = antigravity_home()
        return {"root": str(root / "antigravity-cli"), "user_config": str(root / "antigravity-cli" / "settings.json"),
                "hooks": str(root / "config" / "hooks.json"), "mcp": str(root / "config" / "mcp_config.json")}

    def is_process_match(self, command: str, pid: str) -> bool:
        return is_antigravity_command(command)

    def build_spawn_command(self, options: SpawnCommandOptions) -> list[str]:
        if options.mode not in {"plain", "resume"}:
            raise ProviderLaunchError("Antigravity CLI supports plain and resume only", "unsupported_launch_mode")
        # "anthropic" is the neutral default of SpawnCommandOptions.platform.
        if options.platform.strip() not in {"", "anthropic", "google"}:
            raise ProviderLaunchError("Antigravity CLI runs only on Google", "antigravity_platform_unsupported")
        unsupported = (
            "worktree_name", "project_folder", "profile", "profile_v2", "sandbox", "approval_policy",
            "search", "no_alt_screen", "dangerously_bypass_approvals_and_sandbox", "aws_region",
            "aws_profile", "bedrock_model", "agent", "context_tier", "plan", "remote", "allow_all",
            "no_ask_user", "use_last",
        )
        if any(getattr(options, field) not in (None, False, "") for field in unsupported):
            raise ProviderLaunchError("Unsupported Antigravity CLI launch option", "antigravity_option_unsupported")
        command = ["agy"]
        if options.model is not None:
            model = options.model.strip()
            if not _valid_model(model):
                raise ProviderLaunchError("Invalid Antigravity model", "invalid_model")
            command += ["--model", model]
        if options.reasoning_effort:
            if options.reasoning_effort not in ANTIGRAVITY_EFFORTS:
                raise ProviderLaunchError("Invalid Antigravity effort", "invalid_reasoning_effort")
            command += ["--effort", options.reasoning_effort]
        if options.skip_permissions:
            command.append("--dangerously-skip-permissions")
        if options.mode == "resume":
            if not options.session_id or not _UUID.fullmatch(options.session_id):
                raise ProviderLaunchError("Antigravity resume requires an exact conversation UUID",
                                          "antigravity_session_invalid")
            command += ["--conversation", options.session_id]
        elif options.session_id:
            raise ProviderLaunchError("A conversation id requires resume mode", "antigravity_session_invalid")
        if options.prompt:
            # Observed on 1.5.9 (#952): `-i` also sends a prompt into a resumed conversation.
            if options.prompt.startswith("-"):
                raise ProviderLaunchError("An Antigravity prompt cannot start with -", "antigravity_literal_prompt_required")
            command += ["-i", options.prompt]
        return command

    def get_allowed_cli_commands(self) -> list[str]:
        return []

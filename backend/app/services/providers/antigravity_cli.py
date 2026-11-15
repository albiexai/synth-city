"""Antigravity CLI provider (Google, binary ``agy``).

Built from the documented behaviour in
``docs/superpowers/specs/2026-11-15-antigravity-cli-harness-design.md``. The live behaviour is
not observed yet (#952), so anything unproven is refused rather than guessed.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services.providers.base import AgentProvider, ProviderLaunchError, SpawnCommandOptions, argv0_name
from app.services.providers.launch_contract import ANTIGRAVITY_EFFORTS

# Conversation ids were UUIDs on agy 1.3.5 (`conversations/<uuid>.db`, #734). Not proven live.
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def antigravity_home() -> Path:
    return Path("~/.gemini").expanduser()


def is_antigravity_command(command: str) -> bool:
    return argv0_name(command) == "agy"


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
            if not model or model.startswith("-") or any(character in model for character in "\n\r\x00 "):
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
            if options.prompt:
                # Whether `--conversation` takes a prompt is not proven (#952).
                raise ProviderLaunchError("Antigravity resume does not take a prompt", "antigravity_option_unsupported")
            return command + ["--conversation", options.session_id]
        if options.session_id:
            raise ProviderLaunchError("A conversation id requires resume mode", "antigravity_session_invalid")
        if options.prompt:
            if options.prompt.startswith("-"):
                raise ProviderLaunchError("An Antigravity prompt cannot start with -", "antigravity_literal_prompt_required")
            command += ["-i", options.prompt]
        return command

    def get_allowed_cli_commands(self) -> list[str]:
        return []

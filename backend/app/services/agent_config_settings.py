"""Shared settings for the controller, sudoers printer, and installed trust helper."""
from __future__ import annotations

import json
import os
import pwd
import re
import stat
from dataclasses import dataclass, fields
from pathlib import Path

POLICY_PATH = Path("/etc/albiex-synth-city/agent-config.json")
_MAX_POLICY_BYTES = 8192


@dataclass(frozen=True)
class AgentConfigSettings:
    agent_config_helper_path: str = "/opt/albiex-synth-city-product/libexec/agent-config"
    agent_config_agent_user: str = "deckproduct"
    agent_config_controller_user: str = "deckproductctl"
    agent_config_work_root: str = "/home/deckproductctl/work"

    def __post_init__(self) -> None:
        for name in (self.agent_config_agent_user, self.agent_config_controller_user):
            if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", name) or name in {"ALL", "root"}:
                raise ValueError("Invalid agent-config account")
        try:
            agent = pwd.getpwnam(self.agent_config_agent_user)
        except KeyError:
            pass  # The printer can run before the operator creates the account.
        else:
            if agent.pw_uid == 0:
                raise ValueError("Invalid agent-config account")

        if not re.fullmatch(r"/[a-zA-Z0-9_./-]+", self.agent_config_helper_path):
            raise ValueError("Invalid agent-config helper path")
        for value in (self.agent_config_helper_path, self.agent_config_work_root):
            path = Path(value)
            if (not path.is_absolute() or path == Path("/") or str(path) != value
                    or ".." in path.parts or any(char in value for char in "*\0\r\n")):
                raise ValueError("Invalid agent-config path")

    def command(self) -> list[str]:
        return ["/usr/bin/sudo", "-n", "-u", self.agent_config_agent_user, "--", self.agent_config_helper_path]

    def sudoers_line(self) -> str:
        return (f'{self.agent_config_controller_user} ALL=({self.agent_config_agent_user}) '
                f'NOPASSWD: {self.agent_config_helper_path} ""')


def _require_root_control(info: os.stat_result) -> None:
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError("Agent-config policy must be controlled by root")


def _read_policy() -> bytes:
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open("/", directory_flags)
    try:
        for part in POLICY_PATH.parent.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
            _require_root_control(os.fstat(directory))
        fd = os.open(POLICY_PATH.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        with os.fdopen(fd, "rb") as policy:
            info = os.fstat(fd)
            _require_root_control(info)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Agent-config policy must be a regular file")
            payload = policy.read(_MAX_POLICY_BYTES + 1)
            if len(payload) > _MAX_POLICY_BYTES:
                raise ValueError("Agent-config policy is too large")
            return payload
    finally:
        os.close(directory)


def load_agent_config_settings() -> AgentConfigSettings:
    """Read the root-owned policy. An installation without it uses the built-in defaults."""
    try:
        payload = _read_policy()
    except FileNotFoundError:
        return AgentConfigSettings()
    values = json.loads(payload)
    if (not isinstance(values, dict) or not values.keys() <= {field.name for field in fields(AgentConfigSettings)}
            or not all(isinstance(value, str) for value in values.values())):
        raise ValueError("Invalid agent-config policy")
    return AgentConfigSettings(**values)

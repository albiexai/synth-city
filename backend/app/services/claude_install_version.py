"""The Claude Code version of another user's install, read from disk and never by running anything (#1069).

The controller must show the agent user's version, not its own. Nothing here runs a program: no `claude --version`,
no `sudo`, no execution as the agent user. The version is read only through the ACTIVE launcher, because an install
folder or a package that merely exists does not say which binary runs:

* the native installer: `~/.local/bin/claude` is a symlink to `.../claude/versions/<version>`; the target's name is the
  version;
* an npm install under the user's own prefix: `<prefix>/bin/claude` is a symlink into a
  `node_modules/@anthropic-ai/claude-code` package, and that package's `package.json` names the version.

Anything else (no launcher, a launcher that is not a symlink, a dangling or unexpected target, a file that cannot be
read, a value that is not a plain semantic version) is unknown. Unknown is the safe answer.

The paths come from the scoped user's home and from symlinks on that user's disk, never from a request.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "version unknown (cannot read the agent user's install)"
# A plain semantic version, optionally with a pre-release or build suffix. Never a path, never free text.
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_PACKAGE_FILE_LIMIT = 1_048_576
_NPM_PREFIXES = (".npm-global", ".local")


@dataclass(frozen=True)
class InstalledVersion:
    version: str | None
    source: str | None  # "native" or "npm" when the version is known


def _is_version(value: object) -> bool:
    return isinstance(value, str) and bool(_VERSION.match(value))


def _native(launcher: Path) -> str | None:
    """The version named by a native-installer launcher, or None when the launcher is not one."""
    target = Path(os.readlink(launcher))
    if target.parent.name != "versions" or target.parent.parent.name != "claude" or not _is_version(target.name):
        return None
    return target.name if os.path.exists(launcher) else None      # a dangling link is not the active binary


def _npm(launcher: Path) -> str | None:
    """The version of the npm package a launcher resolves into, or None."""
    resolved = launcher.resolve(strict=True)
    for root in (resolved, *resolved.parents):
        if root.name == "claude-code" and root.parent.name == "@anthropic-ai" and root.parent.parent.name == "node_modules":
            package = root / "package.json"
            if package.stat().st_size > _PACKAGE_FILE_LIMIT:
                return None
            version = json.loads(package.read_text(encoding="utf-8")).get("version")
            return version if _is_version(version) else None
    return None


def read_installed_version(home: Path) -> InstalledVersion:
    """The Claude Code version the user at `home` actually launches, or unknown. It never raises and never executes."""
    unknown = InstalledVersion(None, None)
    try:
        for prefix in _NPM_PREFIXES:
            launcher = home / prefix / "bin" / "claude"
            if not launcher.is_symlink():
                continue
            native = _native(launcher)
            if native is not None:
                return InstalledVersion(native, "native")
            npm = _npm(launcher)
            if npm is not None:
                return InstalledVersion(npm, "npm")
    except (OSError, ValueError, RuntimeError):
        return unknown
    return unknown

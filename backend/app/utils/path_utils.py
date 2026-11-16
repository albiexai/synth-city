"""Path utilities for Claude Code configuration file locations."""
import os
import platform
import pwd
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def get_managed_settings_file() -> Path:
    """
    Get the managed settings file path (admin-enforced, read-only).
    
    Returns OS-specific path:
    - macOS: /Library/Application Support/ClaudeCode/managed-settings.json
    - Linux: /etc/claude-code/managed-settings.json
    - Windows: C:\\ProgramData\\ClaudeCode\\managed-settings.json
    """
    system = platform.system()
    if system == "Darwin":  # macOS
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    elif system == "Linux":
        return Path("/etc/claude-code/managed-settings.json")
    elif system == "Windows":
        return Path("C:/ProgramData/ClaudeCode/managed-settings.json")
    else:
        # Fallback to Linux path for unknown systems
        return Path("/etc/claude-code/managed-settings.json")


def get_managed_mcp_config_file() -> Path:
    """
    Get the managed MCP config file path (admin-enforced, read-only).
    
    This file contains MCP servers configured by enterprise/system admins.
    These servers are always enabled and cannot be modified by users.
    
    Returns OS-specific path:
    - macOS: /Library/Application Support/ClaudeCode/managed-mcp.json
    - Linux: /etc/claude-code/managed-mcp.json
    - Windows: C:\\ProgramData\\ClaudeCode\\managed-mcp.json
    """
    system = platform.system()
    if system == "Darwin":  # macOS
        return Path("/Library/Application Support/ClaudeCode/managed-mcp.json")
    elif system == "Linux":
        return Path("/etc/claude-code/managed-mcp.json")
    elif system == "Windows":
        return Path("C:/ProgramData/ClaudeCode/managed-mcp.json")
    else:
        # Fallback to Linux path for unknown systems
        return Path("/etc/claude-code/managed-mcp.json")


class ClaudePathUtils:
    """Utility class for managing Claude Code configuration paths."""

    @staticmethod
    def get_user_claude_json() -> Optional[Path]:
        """Get user-level claude.json path."""
        return get_claude_user_config_file()

    @staticmethod
    def get_user_settings_json() -> Optional[Path]:
        """Get user-level settings.json path."""
        return get_claude_user_settings_file()

    @staticmethod
    def get_user_settings_local_json() -> Optional[Path]:
        """Get user-level settings.local.json path."""
        return get_claude_user_settings_local_file()

    @staticmethod
    def get_user_commands_dir() -> Optional[Path]:
        """Get user-level commands directory path."""
        return get_claude_user_commands_dir()

    @staticmethod
    def get_user_agents_dir() -> Optional[Path]:
        """Get user-level agents directory path."""
        return get_claude_user_agents_dir()

    @staticmethod
    def get_user_skills_dir() -> Optional[Path]:
        """Get user-level skills directory path."""
        return get_claude_user_skills_dir()

    @staticmethod
    def get_managed_settings_json() -> Path:
        """Get managed settings file path (admin-enforced, read-only)."""
        return get_managed_settings_file()


# The home that one request reads as its "global" (user) scope. Only the harness scope dependency sets it,
# for one request. Everything else (backups, the controller's own data) keeps the controller's home.
_scope_home: ContextVar[Optional[Path]] = ContextVar("harness_scope_home", default=None)


@dataclass(frozen=True)
class ScopeUser:
    """One OS user whose Claude Code configuration the global scope can show."""

    name: str
    home: Path
    is_controller: bool
    is_default: bool


def controller_user_name() -> str:
    """The OS user that runs this controller."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return os.environ.get("USER") or str(os.geteuid())


def scope_users() -> list[ScopeUser]:
    """The users the global scope may read: the controller itself and the one configured agent user.

    A request never names an arbitrary user. Anything outside this list is refused, so a caller cannot
    point the controller at another person's home.
    """
    # Read when a scope is resolved, not when the module loads: path_utils is imported very early (by the
    # provider modules) and the settings stack is not needed there.
    from app.config import settings

    controller = controller_user_name()
    agent = (settings.harness_agent_user or "").strip() or None
    users = [ScopeUser(controller, Path.home(), True, agent in (None, controller))]
    if agent and agent != controller:
        try:
            users.append(ScopeUser(agent, Path(pwd.getpwnam(agent).pw_dir), False, True))
        except KeyError:
            # An unknown configured user must not silently fall back to the controller's home.
            raise LookupError(f"The configured harness agent user {agent!r} does not exist on this host.")
    return users


def resolve_scope_user(name: Optional[str]) -> ScopeUser:
    """The user for a request: the named one, or the default (the agent user when one is configured)."""
    users = scope_users()
    if not name:
        return next(user for user in users if user.is_default)
    for user in users:
        if user.name == name:
            return user
    raise ValueError(f"{name!r} is not a user whose global scope can be shown.")


def set_scope_home(home: Optional[Path]):
    """Use this home as the global scope for the current request. Returns the token to reset it."""
    return _scope_home.set(home)


def get_user_home() -> Path:
    """The home of the global (user) scope for this request; the controller's own home by default."""
    return _scope_home.get() or Path.home()


def get_claude_user_config_dir() -> Path:
    """Get the user-level Claude configuration directory (~/.claude/)."""
    return get_user_home() / ".claude"


def get_claude_user_config_file() -> Path:
    """Get the user-level Claude config file (~/.claude.json)."""
    return get_user_home() / ".claude.json"


def get_claude_user_settings_file() -> Path:
    """Get the user-level Claude settings file (~/.claude/settings.json)."""
    return get_claude_user_config_dir() / "settings.json"


def get_claude_user_settings_local_file() -> Path:
    """Get the user-level Claude local settings file (~/.claude/settings.local.json)."""
    return get_claude_user_config_dir() / "settings.local.json"


def get_claude_user_commands_dir() -> Path:
    """Get the user-level Claude commands directory (~/.claude/commands/)."""
    return get_claude_user_config_dir() / "commands"


def get_claude_user_agents_dir() -> Path:
    """Get the user-level Claude agents directory (~/.claude/agents/)."""
    return get_claude_user_config_dir() / "agents"


def get_claude_user_skills_dir() -> Path:
    """Get the user-level Claude skills directory (~/.claude/skills/)."""
    return get_claude_user_config_dir() / "skills"


def get_claude_user_plugins_dir() -> Path:
    """Get the user-level Claude plugins directory (~/.claude/plugins/)."""
    return get_claude_user_config_dir() / "plugins"


def get_installed_plugins_file() -> Path:
    """Get the installed plugins manifest file (~/.claude/plugins/installed_plugins.json)."""
    return get_claude_user_plugins_dir() / "installed_plugins.json"


def get_known_marketplaces_file() -> Path:
    """Get the known marketplaces file (~/.claude/plugins/known_marketplaces.json)."""
    return get_claude_user_plugins_dir() / "known_marketplaces.json"


def get_marketplaces_dir() -> Path:
    """Get the marketplaces directory (~/.claude/plugins/marketplaces/)."""
    return get_claude_user_plugins_dir() / "marketplaces"


def get_claude_user_output_styles_dir() -> Path:
    """Get the user-level Claude output styles directory (~/.claude/output-styles/)."""
    return get_claude_user_config_dir() / "output-styles"


def get_project_claude_dir(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude configuration directory (.claude/)."""
    if project_path:
        return Path(project_path) / ".claude"
    return Path.cwd() / ".claude"


def get_project_settings_file(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude settings file (.claude/settings.json)."""
    return get_project_claude_dir(project_path) / "settings.json"


def get_project_settings_local_file(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude local settings file (.claude/settings.local.json)."""
    return get_project_claude_dir(project_path) / "settings.local.json"


def get_project_commands_dir(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude commands directory (.claude/commands/)."""
    return get_project_claude_dir(project_path) / "commands"


def get_project_agents_dir(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude agents directory (.claude/agents/)."""
    return get_project_claude_dir(project_path) / "agents"


def get_project_hooks_dir(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude hooks directory (.claude/hooks/)."""
    return get_project_claude_dir(project_path) / "hooks"


def get_project_skills_dir(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude skills directory (.claude/skills/)."""
    return get_project_claude_dir(project_path) / "skills"


def get_project_plugins_dir(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude plugins directory (.claude/plugins/)."""
    return get_project_claude_dir(project_path) / "plugins"


def get_project_output_styles_dir(project_path: Optional[str] = None) -> Path:
    """Get the project-level Claude output styles directory (.claude/output-styles/)."""
    return get_project_claude_dir(project_path) / "output-styles"


def get_project_mcp_config_file(project_path: Optional[str] = None) -> Path:
    """Get the project-level MCP config file (.mcp.json)."""
    if project_path:
        return Path(project_path) / ".mcp.json"
    return Path.cwd() / ".mcp.json"


def get_project_claude_md_file(project_path: Optional[str] = None) -> Path:
    """Get the project-level CLAUDE.md file."""
    if project_path:
        return Path(project_path) / "CLAUDE.md"
    return Path.cwd() / "CLAUDE.md"


def ensure_directory_exists(path: Path) -> None:
    """Ensure a directory exists, creating it if necessary."""
    path.mkdir(parents=True, exist_ok=True)


def get_claude_projects_dir() -> Path:
    """Get Claude projects directory (~/.claude/projects/)."""
    return get_claude_user_config_dir() / "projects"


def get_project_display_name(folder_name: str) -> str:
    """
    Convert Claude project folder name to display name.

    Example: '-home-user-projects-foo' -> 'foo'
    """
    parts = folder_name.split('-')
    if len(parts) > 3:
        # Return the last part as the display name
        return parts[-1]
    return folder_name


def get_claude_plans_dir() -> Path:
    """Get default Claude plans directory (~/.claude/plans/)."""
    return get_claude_user_config_dir() / "plans"


def convert_path_to_folder_name(absolute_path: str) -> str:
    """Convert absolute path to Claude's hyphenated folder format.

    Replaces '/' and '.' with '-' to match Claude Code's encoding scheme.

    Examples:
        '/home/juan/projects/foo' -> '-home-juan-projects-foo'
        '/home/juan/.ssh' -> '-home-juan--ssh'
    """
    return absolute_path.rstrip('/').replace('/', '-').replace('.', '-')

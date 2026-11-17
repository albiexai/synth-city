"""A best-effort, metadata-only check that a test run leaves Synth City's own real-home paths alone (#1122).

What is watched, and nothing else:

* the top-level names `.albiex-synth-city*` and `.claude-registry*` in each real home (the original `HOME` and the
  account's own home), and a bounded recursive listing under `.albiex-synth-city` and `.claude-registry`;
* outside HOME, by configuration: the database file named by `DATABASE_URL` (default `./synth_city.db`) and the legacy
  `claude_registry.db` in the working directory, each with its `-wal`, `-shm` and `-journal` sidecars, as named files
  (the folder is never listed); and the directories named by `BRIDGE_ATTACHMENT_DIR` and `BRIDGE_ATTACHMENT_AGENT_ROOT`,
  when they are set. The values come from the original environment and, below it in precedence, a `.env` in the
  working directory; only those three keys are read from that file.

Never traversed: `~/.claude` and `~/.codex` (the live agents write there), not even when a configured directory overlaps
them. A configured directory that is a real home, contains one, or overlaps those two folders is refused, and the
refusal is reported. Symlinks are never followed and no file content is read. Entries record mode, size and mtime_ns.
Messages name roles ("configured database"), never configured values.

Limits, on purpose: the check compares metadata between the start and the end of the run. It cannot name the test that
wrote, and a write that was undone before the end is not seen. It is not a sandbox.
"""
import os
import pwd
import stat
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

PREFIXES = (".albiex-synth-city", ".claude-registry")
PROTECTED_NAMES = (".claude", ".codex")
CONFIG_NAMES = ("DATABASE_URL", "BRIDGE_ATTACHMENT_DIR", "BRIDGE_ATTACHMENT_AGENT_ROOT")
HOME_NAMES = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
DATABASE_NAMES = ("synth_city.db", "claude_registry.db")
SIDECARS = ("", "-wal", "-shm", "-journal")
DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./synth_city.db"
MAX_DEPTH = 6
MAX_ENTRIES = 20000
EXCEEDED = ("bound-exceeded",)
UNREADABLE = ("unreadable",)
REFUSED = ("refused",)
FAILURES = (EXCEEDED, UNREADABLE, REFUSED)
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_directory(path):
    """Open an absolute directory path without following a symlink in ANY component, not only the last one."""
    descriptor = os.open(os.sep, _DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            inner = os.open(part, _DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = inner
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class _BoundExceeded(Exception):
    pass


def _real(path):
    return os.path.realpath(path)


def _overlap(first, second):
    """True when one resolved path is the other or inside it."""
    return _contains(first, second) or _contains(second, first)


def _contains(folder, path):
    """True when the resolved path is the folder or inside it."""
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def _record(st):
    return (st.st_mode, st.st_size, st.st_mtime_ns)


def configured_values(environ, cwd):
    """The three configuration values as the app would see them: the process environment over a `.env`.

    The `.env` is read by python-dotenv itself (what pydantic-settings uses), so quoting, escapes, trailing comments,
    `export` and `${VAR}` references mean exactly what they mean to the app. Only the three keys are kept."""
    found = {}
    try:
        values = dotenv_values(Path(cwd) / ".env", encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        values = {}
    for key, value in values.items():
        if key.upper() in CONFIG_NAMES and value is not None:
            found[key.upper()] = value
    for key, value in environ.items():
        if key.upper() in CONFIG_NAMES:
            found[key.upper()] = value
    return found


def _database_file(value, cwd):
    try:
        url = make_url(value or DEFAULT_DATABASE_URL)
    except ArgumentError:
        return None
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return None
    return _real(os.path.join(cwd, url.database))


def _directory(value, cwd, home):
    if value == "~" or value.startswith("~/"):
        value = home + value[1:]
    return _real(os.path.join(cwd, value))


class Guard:
    def __init__(self, original_home, passwd_home, cwd, environ):
        homes = {_real(home) for home in (original_home, passwd_home) if home}
        self.homes = tuple(sorted(home for home in homes if home != os.sep))
        self.cwd = _real(cwd)
        self.protected = tuple(_real(os.path.join(home, name)) for home in self.homes for name in PROTECTED_NAMES)
        self.files = {}      # label -> path: named files, stat only
        self.roots = {}      # label -> path: directories listed within the bounds
        self.refused = []    # labels of configured directories that cannot be watched
        configured = configured_values(environ, cwd)
        names = {os.path.join(self.cwd, name) for name in DATABASE_NAMES}
        database = _database_file(configured["DATABASE_URL"], self.cwd) if configured.get("DATABASE_URL") else None
        if database:
            names.add(database)
        for name in sorted(names):
            for suffix in SIDECARS:
                label = "configured database" if name == database else "working directory/" + os.path.basename(name)
                self.files[label + suffix] = name + suffix
        for key, label in (("BRIDGE_ATTACHMENT_DIR", "configured attachment dir"),
                           ("BRIDGE_ATTACHMENT_AGENT_ROOT", "configured attachment agent root")):
            if configured.get(key):
                self._add_root(label, _directory(configured[key], self.cwd, original_home or ""))

    def _add_root(self, label, path):
        # Inside a real home is fine (it is the configured Synth City folder); a home itself or a folder above one is not.
        if any(_overlap(path, folder) for folder in self.protected) or any(_contains(path, home) for home in self.homes):
            self.refused.append(label)
            return
        self.roots[label] = path

    def home_roots(self):
        return [os.path.join(home, name) for home in self.homes for name in PREFIXES]

    def in_real_home(self, path):
        resolved = _real(path)
        return any(_overlap(resolved, home) for home in self.homes)

    def environment_problems(self, environ=None, expanded=None):
        """Names only, never values: what points a test run at a real home or at a watched real path."""
        environ = os.environ if environ is None else environ
        problems = []
        for name in HOME_NAMES:
            if environ.get(name) and self.in_real_home(environ[name]):
                problems.append(f"{name} points into a real home")
        expanded = os.path.expanduser("~") if expanded is None else expanded
        if self.in_real_home(expanded):
            problems.append("~ expands into a real home")
        watched_files = set(self.files.values())
        watched_dirs = [*self.home_roots(), *self.roots.values()]
        for key, value in environ.items():
            name = key.upper()
            if name not in CONFIG_NAMES or not value:
                continue
            if name == "DATABASE_URL":
                database = _database_file(value, self.cwd)
                hit = database in watched_files
            else:
                directory = _directory(value, self.cwd, environ.get("HOME", ""))
                hit = any(_overlap(directory, path) for path in watched_dirs)
            if hit:
                problems.append(f"{name} points at a watched real path")
        return problems

    def _walk(self, descriptor, label, depth, found, counter):
        """List the directory behind an already opened descriptor. The bounds are tested while scanning, so a huge
        folder is never read whole, and a subfolder is opened relative to its parent without following a symlink,
        so a swap after the metadata read is not traversed."""
        with os.scandir(descriptor) as listing:
            for child in listing:
                if depth > MAX_DEPTH:
                    raise _BoundExceeded
                if child.name in PROTECTED_NAMES:
                    continue
                counter[0] += 1
                if counter[0] > MAX_ENTRIES:
                    raise _BoundExceeded
                st = os.stat(child.name, dir_fd=descriptor, follow_symlinks=False)
                found[label + "/" + child.name] = _record(st)
                if stat.S_ISDIR(st.st_mode):
                    inner = os.open(child.name, _DIRECTORY, dir_fd=descriptor)
                    try:
                        self._walk(inner, label + "/" + child.name, depth + 1, found, counter)
                    finally:
                        os.close(inner)

    def _walk_root(self, label, path, entries):
        found = {}
        try:
            descriptor = _open_directory(path)              # a symlink in any component fails: something was swapped
        except OSError:
            entries[label] = UNREADABLE
            return
        try:
            self._walk(descriptor, label, 1, found, [0])
        except _BoundExceeded:
            entries[label] = EXCEEDED
        except OSError:
            entries[label] = UNREADABLE
        else:
            entries.update(found)
        finally:
            os.close(descriptor)

    def snapshot(self):
        entries = {label: REFUSED for label in self.refused}

        def look(label, path):
            """The metadata of a path, None when it is simply not there; any other failure is recorded, not skipped."""
            try:
                return os.stat(path, follow_symlinks=False)
            except (FileNotFoundError, NotADirectoryError):
                return None
            except OSError:
                entries[label] = UNREADABLE
                return None

        for home in self.homes:
            try:
                with os.scandir(home) as listing:
                    names = [entry.name for entry in listing if entry.name.startswith(PREFIXES)]
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                entries[home] = UNREADABLE
                continue
            for name in names:
                path = os.path.join(home, name)
                st = look(path, path)
                if st is None:
                    continue
                entries[path] = _record(st)
                if name in PREFIXES and stat.S_ISDIR(st.st_mode):
                    self._walk_root(path, path, entries)
        for label, path in self.files.items():
            st = look(label, path)
            if st is not None:
                entries[label] = _record(st)
        for label, path in self.roots.items():
            st = look(label, path)
            if st is None:
                continue
            entries[label] = _record(st)
            if stat.S_ISDIR(st.st_mode):
                self._walk_root(label, path, entries)
        return entries


def changed(before, after):
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


_guard = None
_baseline = {}


def arm(original_home, environ, cwd):
    """Record the real paths and their state. Call it before HOME, XDG and the configuration are moved."""
    global _guard, _baseline
    try:
        passwd_home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        passwd_home = None
    _guard = Guard(original_home, passwd_home, cwd, environ)
    _baseline = _guard.snapshot()
    return _guard


def guard():
    return _guard


def session_changes():
    after = _guard.snapshot()
    failed = [key for key, value in after.items() if value in FAILURES]
    return sorted(set(changed(_baseline, after)) | set(failed))

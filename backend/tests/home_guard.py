"""Refuse test writes under the real home directory and record each refusal."""
import os
import pwd
import sys
from collections.abc import Mapping
from contextlib import contextmanager

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_WRITE_MODES = frozenset("wax+")
_PATH_ARGS = {
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.chmod": (0,),
    "os.chown": (0,),
    "os.truncate": (0,),
    "os.utime": (0,),
    "os.link": (1,),
    "os.symlink": (1,),
    "os.rename": (0, 1),
    "os.replace": (0, 1),
}
_HOME_ENV = ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME")
_RECORDED_PREFIXES = (".albiex-synth-city", ".claude-registry")

_roots = ()
_exempt = ()
_baseline = {}
_violations = []


def _under(path, root):
    return path == root or path.startswith(root + os.sep)


def _real(path):
    return os.path.realpath(os.fsdecode(path))


def _refused(candidate):
    if isinstance(candidate, int):
        return None
    try:
        real = _real(candidate)
    except TypeError:
        return None
    if any(_under(real, root) for root in _exempt):
        return None
    if any(_under(real, root) for root in _roots):
        return real
    return None


def _writes(flags, mode):
    if isinstance(flags, int):
        return bool(flags & _WRITE_FLAGS)
    return bool(set(mode) & _WRITE_MODES)


def _hook(event, args):
    if event == "open":
        if not _writes(args[2], args[1]):
            return
        candidates = (args[0],)
    elif event in _PATH_ARGS:
        candidates = tuple(args[i] for i in _PATH_ARGS[event])
    elif event == "sqlite3.connect":
        if args[0] in ("", ":memory:"):
            return
        candidates = (args[0],)
    elif event == "subprocess.Popen" and isinstance(args[3], Mapping):
        candidates = tuple(args[3][name] for name in _HOME_ENV if args[3].get(name))
    else:
        return
    for candidate in candidates:
        real = _refused(candidate)
        if real:
            _violations.append((event, real))
            raise PermissionError(f"write under the real home refused: {real}")


sys.addaudithook(_hook)


def _record_tree(entries, top):
    paths = [top]
    if os.path.isdir(top) and not os.path.islink(top):
        for dirpath, dirnames, filenames in os.walk(top):
            paths.extend(os.path.join(dirpath, name) for name in dirnames + filenames)
    for path in paths:
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        entries[path] = (st.st_size, st.st_mtime_ns, st.st_mode)


def snapshot(roots=None):
    entries = {}
    for home in _roots if roots is None else roots:
        if not os.path.isdir(home):
            continue
        for name in os.listdir(home):
            if name.startswith(_RECORDED_PREFIXES):
                _record_tree(entries, os.path.join(home, name))
    return entries


def changed(before, after):
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def arm(original_home, session_home, checkout):
    global _roots, _exempt, _baseline
    homes = {original_home, _passwd_home()} - {None, "", "/"}
    _roots = tuple(sorted({_real(home) for home in homes}))
    _exempt = (_real(session_home), _real(checkout))
    _baseline = snapshot()


def _passwd_home():
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return None


def session_changes():
    return changed(_baseline, snapshot())


def take_violations():
    found = list(_violations)
    _violations.clear()
    return found


def is_refused(path):
    return _refused(path) is not None


@contextmanager
def scoped(roots):
    global _roots
    saved = _roots
    _roots = tuple(_real(root) for root in roots)
    try:
        yield
    finally:
        _roots = saved

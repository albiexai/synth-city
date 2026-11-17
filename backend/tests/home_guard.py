"""Best-effort, metadata-only snapshot of Synth City entries in the real home."""
import os
import pwd
import stat

_PREFIXES = (".albiex-synth-city", ".claude-registry")
_WALK_ROOTS = (".albiex-synth-city", ".claude-registry")
_MAX_DEPTH = 6
_MAX_ENTRIES = 20000
EXCEEDED = ("bound-exceeded",)
UNREADABLE = ("unreadable",)
_FAILURES = (EXCEEDED, UNREADABLE)

_homes = ()
_baseline = {}


class _BoundExceeded(Exception):
    pass


def _real_homes(original_home):
    candidates = {original_home}
    try:
        candidates.add(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        pass
    return tuple(sorted({os.path.realpath(home) for home in candidates if home and home != "/"}))


def _watched(name):
    return name.startswith(_PREFIXES)


def _record(st):
    return (st.st_mode, st.st_size, st.st_mtime_ns)


def _walk(directory, depth, found, counter):
    with os.scandir(directory) as listing:
        children = list(listing)
    if children and depth > _MAX_DEPTH:
        raise _BoundExceeded
    for child in children:
        counter[0] += 1
        if counter[0] > _MAX_ENTRIES:
            raise _BoundExceeded
        st = os.stat(child.path, follow_symlinks=False)
        found[child.path] = _record(st)
        if stat.S_ISDIR(st.st_mode):
            _walk(child.path, depth + 1, found, counter)


def _walk_root(path, entries):
    found = {}
    try:
        _walk(path, 1, found, [0])
    except _BoundExceeded:
        entries[path] = EXCEEDED
    except OSError:
        entries[path] = UNREADABLE
    else:
        entries.update(found)


def snapshot(homes):
    entries = {}
    for home in homes:
        try:
            with os.scandir(home) as listing:
                names = [entry.name for entry in listing if _watched(entry.name)]
        except OSError:
            continue
        for name in names:
            path = os.path.join(home, name)
            try:
                st = os.stat(path, follow_symlinks=False)
            except OSError:
                continue
            entries[path] = _record(st)
            if name in _WALK_ROOTS and stat.S_ISDIR(st.st_mode):
                _walk_root(path, entries)
    return entries


def changed(before, after):
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def arm(original_home):
    global _homes, _baseline
    _homes = _real_homes(original_home)
    _baseline = snapshot(_homes)


def session_changes():
    after = snapshot(_homes)
    failed = [path for path, value in after.items() if value in _FAILURES]
    return sorted(set(changed(_baseline, after)) | set(failed))

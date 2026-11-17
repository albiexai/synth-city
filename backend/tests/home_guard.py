"""Best-effort, metadata-only snapshot of Synth City and CLI config entries in the real home."""
import os
import pwd

_PREFIXES = (".albiex-synth-city", ".claude-registry")
_NAMES = (".claude", ".codex")

_homes = ()
_baseline = {}


def _real_homes(original_home):
    candidates = {original_home}
    try:
        candidates.add(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        pass
    return tuple(sorted({os.path.realpath(home) for home in candidates if home and home != "/"}))


def _watched(name):
    return name.startswith(_PREFIXES) or name in _NAMES


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
            entries[path] = (st.st_size, st.st_mode, st.st_mtime_ns)
    return entries


def changed(before, after):
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def arm(original_home):
    global _homes, _baseline
    _homes = _real_homes(original_home)
    _baseline = snapshot(_homes)


def session_changes():
    return changed(_baseline, snapshot(_homes))

"""Self-test: the real-home snapshot detects deliberate writes made in a scratch stand-in."""
import os
import pwd
import sqlite3

import pytest

from app.config import Settings
from tests import home_guard

XDG_NAMES = ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')
PROBE = '.albiex-synth-city-probe'

WRITE_ATTEMPTS = {
    'open-w': lambda target, source: open(target, 'w').close(),
    'mkdir': lambda target, source: os.mkdir(target),
    'symlink-into': lambda target, source: os.symlink(source, target),
    'rename-into': lambda target, source: os.rename(source, target),
    'sqlite-connect': lambda target, source: sqlite3.connect(target).close(),
}


@pytest.fixture
def stand_in_home(tmp_path):
    home = tmp_path / 'stand-in-home'
    home.mkdir()
    (home / 'seed.txt').write_text('seed')
    return home


@pytest.mark.parametrize('attempt', WRITE_ATTEMPTS.values(), ids=WRITE_ATTEMPTS.keys())
def test_a_deliberate_write_into_the_stand_in_is_detected(stand_in_home, tmp_path, attempt):
    source = tmp_path / 'source.txt'
    source.write_text('kept')
    target = stand_in_home / PROBE
    before = home_guard.snapshot([str(stand_in_home)])
    attempt(target, source)
    changed = home_guard.changed(before, home_guard.snapshot([str(stand_in_home)]))
    assert str(target) in changed


def test_an_in_place_edit_of_a_recorded_entry_is_detected(stand_in_home):
    entry = stand_in_home / '.claude-registry'
    entry.write_text('a')
    before = home_guard.snapshot([str(stand_in_home)])
    entry.write_text('bbbb')
    assert str(entry) in home_guard.changed(before, home_guard.snapshot([str(stand_in_home)]))


def test_a_removed_recorded_entry_is_detected(stand_in_home):
    entry = stand_in_home / '.albiex-synth-city'
    entry.mkdir()
    before = home_guard.snapshot([str(stand_in_home)])
    entry.rmdir()
    assert str(entry) in home_guard.changed(before, home_guard.snapshot([str(stand_in_home)]))


@pytest.mark.parametrize('name', ['.claude', '.codex'])
def test_cli_config_entries_and_their_children_are_not_reported(stand_in_home, name):
    before = home_guard.snapshot([str(stand_in_home)])
    entry = stand_in_home / name
    entry.mkdir()
    (entry / 'created.json').write_text('{}')
    assert home_guard.changed(before, home_guard.snapshot([str(stand_in_home)])) == []


def test_a_write_under_a_recorded_root_subpath_is_detected(stand_in_home):
    root = stand_in_home / '.albiex-synth-city'
    (root / 'backups').mkdir(parents=True)
    before = home_guard.snapshot([str(stand_in_home)])
    (root / 'backups' / 'snapshot.db').write_bytes(b'data')
    changed = home_guard.changed(before, home_guard.snapshot([str(stand_in_home)]))
    assert str(root / 'backups' / 'snapshot.db') in changed


def test_an_in_place_edit_inside_a_recorded_root_is_detected(stand_in_home):
    root = stand_in_home / '.albiex-synth-city'
    root.mkdir()
    (root / 'settings.db').write_text('a')
    before = home_guard.snapshot([str(stand_in_home)])
    (root / 'settings.db').write_text('bbbb')
    assert str(root / 'settings.db') in home_guard.changed(before, home_guard.snapshot([str(stand_in_home)]))


def test_a_write_into_the_legacy_root_is_detected(stand_in_home):
    root = stand_in_home / '.claude-registry'
    root.mkdir()
    before = home_guard.snapshot([str(stand_in_home)])
    (root / 'legacy.json').write_text('{}')
    assert str(root / 'legacy.json') in home_guard.changed(before, home_guard.snapshot([str(stand_in_home)]))


def test_a_root_that_appears_from_absence_is_detected(stand_in_home):
    before = home_guard.snapshot([str(stand_in_home)])
    root = stand_in_home / '.albiex-synth-city'
    root.mkdir()
    (root / 'upgrade.lock').write_text('')
    changed = home_guard.changed(before, home_guard.snapshot([str(stand_in_home)]))
    assert str(root) in changed and str(root / 'upgrade.lock') in changed


def test_symlinks_under_a_recorded_root_are_not_followed(stand_in_home, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'kept.txt').write_text('x')
    root = stand_in_home / '.albiex-synth-city'
    root.mkdir()
    os.symlink(outside, root / 'link')
    after = home_guard.snapshot([str(stand_in_home)])
    assert str(root / 'link') in after
    assert not any(path.startswith(str(root / 'link') + os.sep) for path in after)


def test_exceeding_the_entry_bound_names_the_root(stand_in_home, monkeypatch):
    monkeypatch.setattr(home_guard, '_MAX_ENTRIES', 3)
    root = stand_in_home / '.albiex-synth-city'
    root.mkdir()
    for index in range(4):
        (root / f'f{index}').write_text('x')
    assert home_guard.snapshot([str(stand_in_home)])[str(root)] == home_guard.EXCEEDED


def test_exceeding_the_depth_bound_names_the_root(stand_in_home):
    root = stand_in_home / '.albiex-synth-city'
    deep = root.joinpath(*(f'd{index}' for index in range(6)))
    deep.mkdir(parents=True)
    assert home_guard.snapshot([str(stand_in_home)])[str(root)] != home_guard.EXCEEDED
    (deep / 'too-deep.txt').write_text('x')
    assert home_guard.snapshot([str(stand_in_home)])[str(root)] == home_guard.EXCEEDED


def test_a_session_end_bound_failure_is_reported(stand_in_home, monkeypatch):
    root = stand_in_home / '.albiex-synth-city'
    root.mkdir()
    monkeypatch.setattr(home_guard, '_homes', (str(stand_in_home),))
    monkeypatch.setattr(home_guard, '_baseline', home_guard.snapshot([str(stand_in_home)]))
    monkeypatch.setattr(home_guard, '_MAX_ENTRIES', 1)
    (root / 'a').write_text('')
    (root / 'b').write_text('')
    assert str(root) in home_guard.session_changes()


def test_writes_outside_the_recorded_prefixes_are_not_reported(stand_in_home):
    before = home_guard.snapshot([str(stand_in_home)])
    (stand_in_home / 'unrelated.txt').write_text('x')
    (stand_in_home / '.cache').mkdir()
    assert home_guard.changed(before, home_guard.snapshot([str(stand_in_home)])) == []


def test_the_passwd_home_is_snapshotted_as_well():
    passwd_home = os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    if passwd_home == '/':
        pytest.skip('passwd home is not a real home directory')
    assert passwd_home in home_guard._real_homes('/nonexistent-home')


def test_the_session_home_and_xdg_directories_are_temporary():
    session_home = os.environ['HOME']
    assert os.path.expanduser('~') == session_home
    for name in XDG_NAMES:
        assert os.environ[name].startswith(session_home + os.sep), name


def test_settings_home_paths_resolve_under_the_session_home():
    session_home = os.environ['HOME']
    home_paths = {
        name: field.default
        for name, field in Settings.model_fields.items()
        if isinstance(field.default, str) and field.default.startswith('~')
    }
    assert home_paths
    for name, value in home_paths.items():
        assert os.path.expanduser(value).startswith(session_home + os.sep), name

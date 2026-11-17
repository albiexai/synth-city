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
def test_a_new_direct_child_of_a_cli_config_entry_is_detected(stand_in_home, name):
    entry = stand_in_home / name
    entry.mkdir()
    os.utime(entry, ns=(0, 0))
    before = home_guard.snapshot([str(stand_in_home)])
    (entry / 'created.json').write_text('{}')
    assert str(entry) in home_guard.changed(before, home_guard.snapshot([str(stand_in_home)]))


def test_an_in_place_edit_inside_a_cli_config_entry_is_not_reported(stand_in_home):
    entry = stand_in_home / '.claude'
    entry.mkdir()
    (entry / 'settings.json').write_text('a')
    before = home_guard.snapshot([str(stand_in_home)])
    (entry / 'settings.json').write_text('bbbb')
    assert home_guard.changed(before, home_guard.snapshot([str(stand_in_home)])) == []


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

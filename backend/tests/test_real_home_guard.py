"""Self-test: a deliberate write under the real home is refused and leaves nothing behind."""
import os
import pwd
import sqlite3
import subprocess

import pytest

from app.config import Settings
from tests import home_guard

XDG_NAMES = ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME')

WRITE_ATTEMPTS = {
    'open-w': lambda target, source: open(target, 'w').close(),
    'open-append': lambda target, source: open(target, 'a').close(),
    'os-open-creat': lambda target, source: os.close(os.open(target, os.O_CREAT | os.O_WRONLY)),
    'mkdir': lambda target, source: os.mkdir(target),
    'symlink-into': lambda target, source: os.symlink(source, target),
    'rename-into': lambda target, source: os.rename(source, target),
    'sqlite-connect': lambda target, source: sqlite3.connect(target).close(),
}


@pytest.fixture
def fake_real_home(tmp_path):
    fake = tmp_path / 'fake-real-home'
    fake.mkdir()
    (fake / 'seed.txt').write_text('seed')
    with home_guard.scoped([fake]):
        yield fake


@pytest.mark.parametrize('attempt', WRITE_ATTEMPTS.values(), ids=WRITE_ATTEMPTS.keys())
def test_a_deliberate_write_under_the_real_home_is_refused(fake_real_home, tmp_path, attempt):
    source = tmp_path / 'source.txt'
    source.write_text('kept')
    target = fake_real_home / 'synth-city-probe'
    with pytest.raises(PermissionError):
        attempt(target, source)
    assert not target.exists()
    assert source.read_text() == 'kept'
    assert home_guard.take_violations()


def test_a_child_process_given_the_real_home_in_its_env_is_refused(fake_real_home):
    with pytest.raises(PermissionError):
        subprocess.run(['true'], env={'HOME': str(fake_real_home)}, check=False)
    assert home_guard.take_violations()


def test_reads_under_the_refused_root_and_writes_elsewhere_still_work(fake_real_home, tmp_path):
    assert (fake_real_home / 'seed.txt').read_text() == 'seed'
    (tmp_path / 'elsewhere.txt').write_text('ok')
    assert (tmp_path / 'elsewhere.txt').read_text() == 'ok'


def test_the_passwd_home_is_a_refused_root():
    passwd_home = pwd.getpwuid(os.getuid()).pw_dir
    if passwd_home in ('', '/'):
        pytest.skip('passwd home is not a real home directory')
    assert home_guard.is_refused(os.path.join(passwd_home, '.albiex-synth-city', 'probe'))


def test_the_session_home_and_xdg_directories_are_temporary():
    session_home = os.environ['HOME']
    assert os.path.expanduser('~') == session_home
    assert not home_guard.is_refused(session_home)
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


def test_the_snapshot_reports_a_new_file_under_a_recorded_prefix(tmp_path):
    home = tmp_path / 'home'
    (home / '.albiex-synth-city').mkdir(parents=True)
    (home / 'unrelated').mkdir()
    before = home_guard.snapshot([str(home)])
    (home / '.albiex-synth-city' / 'backup.db').write_text('x')
    (home / 'unrelated' / 'note.txt').write_text('x')
    changed = home_guard.changed(before, home_guard.snapshot([str(home)]))
    assert str(home / '.albiex-synth-city' / 'backup.db') in changed
    assert not any('unrelated' in path for path in changed)

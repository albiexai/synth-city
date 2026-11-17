"""Keep test imports and each test independent of installation settings."""
import copy
import atexit
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_settings import BaseSettings

from tests import home_guard, stray_children


# A memory database does not suppress independent HOME-resource conversion.
# Establish isolation before config or child conftests can import application code.
_XDG_SUBDIRS = {
    'XDG_CONFIG_HOME': '.config',
    'XDG_DATA_HOME': '.local/share',
    'XDG_STATE_HOME': '.local/state',
    'XDG_CACHE_HOME': '.cache',
}
_original_env = {name: os.environ.get(name) for name in ('HOME', *_XDG_SUBDIRS)}
_session_home = tempfile.TemporaryDirectory(prefix='synth-city-test-home-')
os.environ['HOME'] = _session_home.name
for _name, _subdir in _XDG_SUBDIRS.items():
    os.environ[_name] = os.path.join(_session_home.name, _subdir)
home_guard.arm(_original_env['HOME'], _session_home.name, Path(__file__).resolve().parents[2])


def _restore_env():
    for name, value in _original_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    _session_home.cleanup()


atexit.register(_restore_env)


_settings_init = BaseSettings.__init__


def _isolated_settings_init(self, **values):
    defaults = {
        name: copy.deepcopy(field.get_default(call_default_factory=True))
        for name, field in type(self).model_fields.items()
    }
    defaults["database_url"] = "sqlite+aiosqlite:///:memory:"
    # Init values take precedence over process values. Never open a local .env.
    _settings_init(self, **(defaults | values | {"_env_file": None}))


# app.config constructs its shared instance during import. Isolate that first
# construction before child conftests import the application and its database.
with patch.object(BaseSettings, "__init__", _isolated_settings_init):
    from app.config import Settings, settings


def pytest_sessionfinish(session, exitstatus):
    leaks = stray_children.close_run()
    if leaks:
        session.exitstatus = 1
        summary = ", ".join(f"{kind} pid {pid} ({name})" for kind, pid, name in leaks)
        print(f"\nstray child processes left by this test run: {summary}")
    refused = home_guard.take_violations()
    if refused:
        session.exitstatus = 1
        print(f"\nwrites under the real home refused outside a test: {refused}")
    changed = home_guard.session_changes()
    if changed:
        session.exitstatus = 1
        print(f"\nreal home changed during this test run: {', '.join(changed)}")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    names = {name.casefold() for name in Settings.model_fields}
    for name in tuple(os.environ):
        if name.casefold() in names:
            monkeypatch.delenv(name)
    monkeypatch.setattr(Settings, "__init__", _isolated_settings_init)
    defaults = Settings()
    for name in Settings.model_fields:
        monkeypatch.setattr(settings, name, copy.deepcopy(getattr(defaults, name)))


@pytest.fixture(autouse=True, scope="session")
def isolated_home_session():
    if os.environ['HOME'] != _session_home.name:
        raise RuntimeError('HOME does not point at the session test home')
    for name, subdir in _XDG_SUBDIRS.items():
        if os.environ[name] != os.path.join(_session_home.name, subdir):
            raise RuntimeError(f'{name} does not point at the session test home')
    yield


@pytest.fixture(autouse=True)
def real_home_guard():
    yield
    refused = home_guard.take_violations()
    if refused:
        pytest.fail(
            'test wrote under the real home: '
            + ', '.join(f'{event} {path}' for event, path in refused),
            pytrace=False,
        )

"""Keep test imports and each test independent of installation settings."""
import copy
import atexit
import os
import tempfile
from collections import OrderedDict
from unittest.mock import patch

import pytest
from pydantic_settings import BaseSettings

from tests import home_guard, stray_children


# A memory database does not suppress independent HOME-resource conversion.
# Establish isolation before config or child conftests can import application code.
# First record the real paths and their state; only then move HOME, XDG and the
# database and attachment settings into a temporary directory (#1122).
_XDG_SUBDIRS = {
    'XDG_CONFIG_HOME': '.config',
    'XDG_DATA_HOME': '.local/share',
    'XDG_STATE_HOME': '.local/state',
    'XDG_CACHE_HOME': '.cache',
}
_original_env = {
    name: value for name, value in os.environ.items()
    if name in home_guard.HOME_NAMES or name.upper() in home_guard.CONFIG_NAMES
}
home_guard.arm(os.environ.get('HOME'), dict(os.environ), os.getcwd())
_session_home = tempfile.TemporaryDirectory(prefix='synth-city-test-home-')
_session_env = {
    'HOME': _session_home.name,
    **{name: os.path.join(_session_home.name, subdir) for name, subdir in _XDG_SUBDIRS.items()},
    'DATABASE_URL': f'sqlite+aiosqlite:///{_session_home.name}/synth_city.db',
    'BRIDGE_ATTACHMENT_DIR': os.path.join(_session_home.name, 'bridge-attachments'),
    'BRIDGE_ATTACHMENT_AGENT_ROOT': os.path.join(_session_home.name, 'bridge-agent-root'),
}
for _name in _original_env:
    os.environ.pop(_name, None)         # every spelling of a configuration name, whatever its case
os.environ.update(_session_env)


def _restore_env():
    for name in _session_env:
        os.environ.pop(name, None)
    os.environ.update(_original_env)
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
    changed = home_guard.session_changes()
    if changed:
        session.exitstatus = 1
        print(f"\nSynth City real-home paths changed or could not be checked during this test run: {', '.join(changed)}")


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


@pytest.fixture(autouse=True)
def isolated_github_rate_limit_state(monkeypatch):
    # The rate-limit service is process-wide; a 403 recorded by one test must not pause /health in the next.
    from app.services.github_rate_limit_service import github_rate_limit_service
    monkeypatch.setattr(github_rate_limit_service, "_observations", OrderedDict())


@pytest.fixture(autouse=True)
def real_home_environment():
    """Fail the test that points HOME, XDG or the configuration back at a real path, then put them right.

    The check runs at the start and again at the end of every test, so a change after the session began is named
    where it happened. It repairs the environment afterwards, so one leak does not fail every later test.
    """
    _fail_on_real_paths('before')
    yield
    _fail_on_real_paths('after')


def _fail_on_real_paths(moment):
    problems = home_guard.guard().environment_problems()
    if problems:
        os.environ.update(_session_env)
        pytest.fail(f"{moment} this test: {'; '.join(problems)}", pytrace=False)

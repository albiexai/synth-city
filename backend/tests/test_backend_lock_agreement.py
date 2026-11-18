"""The backend lock and the exported requirements agree, and FastAPI and starlette are at their new lines (#1160).

No CI job reads uv.lock (the workflows install from requirements.txt), so a lock edited without `uv` has only these
checks to keep it honest: every pin equals the locked version, every locked package is pinned, every dependency a locked
package names is itself locked, and FastAPI's own new requirement (opentelemetry-api) is there.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
LOCK = tomllib.loads((BACKEND / "uv.lock").read_text(encoding="utf-8"))
PACKAGES = {package["name"]: package for package in LOCK["package"]}
PROJECT = [name for name, package in PACKAGES.items() if "editable" in package["source"] or "virtual" in package["source"]]
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins() -> dict[str, str]:
    pins = {}
    for line in (BACKEND / "requirements.txt").read_text(encoding="utf-8").splitlines():
        match = PIN.match(line)
        if match:
            pins[_norm(match.group(1))] = match.group(2)
    return pins


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def test_there_is_exactly_one_project_package_in_the_lock():
    assert len(PROJECT) == 1


def test_every_pin_equals_the_locked_version_and_every_locked_package_is_pinned():
    pins = _pins()
    locked = {_norm(name): package["version"] for name, package in PACKAGES.items() if name not in PROJECT}
    assert {name for name in pins if pins[name] != locked.get(name)} == set(), "pins that differ from the lock"
    assert set(locked) - set(pins) == set(), "locked packages with no pin in requirements.txt"
    assert set(pins) - set(locked) == set(), "pins for packages the lock does not have"


def test_every_dependency_a_locked_package_names_is_locked():
    missing = [(name, dep["name"]) for name, package in PACKAGES.items() for dep in package.get("dependencies", [])
               if dep["name"] not in PACKAGES]
    assert missing == []


def test_fastapi_and_starlette_are_at_their_new_lines():
    assert _version(PACKAGES["fastapi"]["version"]) >= (0, 162, 0)
    assert _version(PACKAGES["starlette"]["version"]) >= (1, 10, 0)


def test_fastapis_required_opentelemetry_api_is_locked_and_pinned():
    names = {dep["name"] for dep in PACKAGES["fastapi"]["dependencies"]}
    assert "opentelemetry-api" in names
    assert "opentelemetry-api" in PACKAGES and "opentelemetry-api" in _pins()


def test_the_locked_stack_supports_the_projects_python_floor():
    # starlette 1.8 and later need Python 3.11, and so does this project.
    assert LOCK["requires-python"] == ">=3.11"

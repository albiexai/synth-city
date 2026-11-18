"""jsdom 31 and jest-dom 7 for the frontend tests, and the Node requirement that comes with them (#1158).

The Supervisor's decision (option B): the installer keeps its Node 22.12+ check; the README and the installation guide
say what RUNNING THE FRONTEND TESTS needs, which is whatever jsdom's own engines field says. The lockfile must agree
with package.json, because `npm ci` fails on a lock that does not.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
LOCK = json.loads((ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))["packages"]
DOCS = {"README.md": ROOT / "README.md", "docs/guide/installation.md": ROOT / "docs" / "guide" / "installation.md"}


def _major(spec: str) -> int:
    return int(re.search(r"\d+", spec).group())


def _unresolved() -> list[tuple[str, str]]:
    """Dependencies that name no package in the lock, looked up the way Node walks up node_modules folders."""
    missing = []
    for path, info in LOCK.items():
        wanted = {**info.get("dependencies", {}), **info.get("optionalDependencies", {})}
        if path == "":
            wanted |= info.get("devDependencies", {})
        for name in wanted:
            base = path
            while True:
                if (base + "/" if base else "") + "node_modules/" + name in LOCK:
                    break
                if not base:
                    missing.append((path, name))
                    break
                cut = base.rfind("/node_modules/")
                base = base[:cut] if cut >= 0 else ""
    return missing


def test_the_lockfile_agrees_with_package_json_and_every_dependency_resolves():
    root = LOCK[""]
    assert root["dependencies"] == PACKAGE["dependencies"]
    assert root["devDependencies"] == PACKAGE["devDependencies"]
    assert _unresolved() == []


def test_jsdom_and_jest_dom_are_at_the_new_majors_in_package_json_and_in_the_lock():
    dev = PACKAGE["devDependencies"]
    assert _major(dev["jsdom"]) >= 31 and _major(LOCK["node_modules/jsdom"]["version"]) >= 31
    assert _major(dev["@testing-library/jest-dom"]) >= 7
    assert _major(LOCK["node_modules/@testing-library/jest-dom"]["version"]) >= 7


def test_jest_dom_7_has_the_peer_it_requires():
    peers = LOCK["node_modules/@testing-library/jest-dom"].get("peerDependencies", {})
    assert "@testing-library/dom" in peers, "jest-dom 7 requires @testing-library/dom as a peer"
    assert "@testing-library/dom" in PACKAGE["devDependencies"]


def _test_run_floor() -> list[str]:
    """The Node versions jsdom's own engines field names, e.g. ['22.22.2', '24.15.0', '26.0.0']."""
    engines = LOCK["node_modules/jsdom"]["engines"]["node"]
    return re.findall(r"(\d+\.\d+\.\d+)", engines)


def test_the_readme_and_the_installation_guide_state_the_node_requirement_for_running_the_frontend_tests():
    floor = _test_run_floor()
    assert floor, "jsdom names no Node range"
    for name, path in DOCS.items():
        text = path.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if "frontend tests" in line and "Node" in line]
        assert lines, f"{name} does not say what running the frontend tests needs"
        for wanted in floor:
            major, minor, patch = wanted.split(".")
            shown = wanted if (major, minor) == ("22", "22") else f"{major}.{minor}" if major == "24" else major
            assert any(shown in line for line in lines), f"{name} leaves out Node {shown}"
        # Node 23 and 25 are not in the supported range, and the page must not imply they are.
        assert any("23" in line and "25" in line and "not" in line for line in lines), f"{name} must say Node 23 and 25 are not supported for the tests"


def test_the_installer_keeps_its_node_floor_and_does_not_take_the_test_run_floor():
    text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "22.12" in text
    assert "22.22" not in text and "24.15" not in text


def test_the_release_note_names_the_test_run_requirement():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("\n## [", 1)[0]
    assert re.search(r"frontend tests[^\n]*22\.22\.2", unreleased), "Unreleased has no note on the frontend-test Node requirement"

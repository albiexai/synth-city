"""The shards must run every backend test exactly once, and the check must notice when they do not (#1013)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "backend_shard_identities.py"
spec = importlib.util.spec_from_file_location("backend_shard_identities", SCRIPT)
ident = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ident)

FULL = [f"tests/a/test_one.py::test_{name}" for name in "abcdef"]


def shard(collected, executed=None, junit=None, outcome="passed"):
    executed = collected if executed is None else executed
    return {"collected": list(collected),
            "executed": [{"id": item, "outcome": outcome, "runs": 1} for item in executed],
            "junit_tests": len(collected) if junit is None else junit}


def good():
    return {"shard 1": shard(FULL[:3]), "shard 2": shard(FULL[3:])}


def test_an_exact_partition_passes_and_reports_its_counts():
    problems, summary = ident.check(FULL, good())
    assert problems == [] and summary["full"] == 6 and summary["executed"] == 6 and summary["shards"] == 2


def test_a_dropped_test_fails():
    shards = {"shard 1": shard(FULL[:3]), "shard 2": shard(FULL[3:5])}
    problems, _ = ident.check(FULL, shards)
    assert any("test_f" in problem for problem in problems)


def test_a_duplicated_test_fails():
    shards = {"shard 1": shard(FULL[:3] + [FULL[0]]), "shard 2": shard(FULL[3:])}
    problems, _ = ident.check(FULL, shards)
    assert any("more frequent" in problem and "test_a" in problem for problem in problems)


def test_a_dropped_and_a_duplicated_test_with_equal_totals_still_fails():
    shards = {"shard 1": shard(FULL[:3]), "shard 2": shard(FULL[3:5] + [FULL[0]])}   # f dropped, a twice: 6 = 6
    problems, summary = ident.check(FULL, shards)
    assert summary["collected"] == len(FULL)                      # the totals agree
    assert any("test_f" in problem for problem in problems) and any("test_a" in problem for problem in problems)


def test_a_test_that_is_not_in_the_full_collection_fails():
    shards = {"shard 1": shard(FULL[:3] + ["tests/a/test_one.py::test_extra"]), "shard 2": shard(FULL[3:])}
    problems, _ = ident.check(FULL, shards)
    assert any("test_extra" in problem for problem in problems)


def test_a_test_collected_but_not_executed_fails_even_when_the_totals_match():
    shards = {"shard 1": shard(FULL[:3], executed=FULL[:2] + [FULL[3]]), "shard 2": shard(FULL[3:])}
    assert ident.check(FULL, shards)[0]


def test_a_failed_or_errored_test_fails_the_check():
    for outcome in ("failed", "error"):
        shards = {"shard 1": shard(FULL[:3], outcome=outcome), "shard 2": shard(FULL[3:])}
        assert any(outcome in problem for problem in ident.check(FULL, shards)[0])


def test_skipped_xfailed_and_xpassed_are_allowed_and_skips_are_counted():
    shards = {"shard 1": shard(FULL[:3], outcome="skipped"), "shard 2": shard(FULL[3:], outcome="xfailed")}
    problems, summary = ident.check(FULL, shards)
    assert problems == [] and summary["skipped"] == 3


def test_a_test_that_ran_twice_fails():
    shards = good()
    shards["shard 1"]["executed"][0]["runs"] = 2
    assert any("ran 2 times" in problem for problem in ident.check(FULL, shards)[0])


def test_the_junit_total_must_equal_the_collected_count():
    shards = {"shard 1": shard(FULL[:3], junit=2), "shard 2": shard(FULL[3:])}
    assert any("JUnit" in problem for problem in ident.check(FULL, shards)[0])
    shards["shard 1"]["junit_tests"] = None
    assert any("JUnit" in problem for problem in ident.check(FULL, shards)[0])


def test_uuid_parameter_ids_match_across_separate_collections():
    one = "tests/a/test_two.py::test_x[sessionId-11111111-2222-3333-4444-555555555555]"
    two = "tests/a/test_two.py::test_x[sessionId-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]"
    shards = {"shard 1": shard([two], executed=[one])}
    assert ident.check([one], shards)[0] == []


def test_two_distinct_tests_that_normalise_to_one_key_are_reported():
    ids = ["t.py::test_x[11111111-2222-3333-4444-555555555555]", "t.py::test_x[aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee]"]
    problems, _ = ident.check(ids, {"shard 1": shard(ids)})
    assert any("collision" in problem for problem in problems)


def test_an_empty_shard_or_an_empty_full_collection_fails():
    assert any("no collected" in problem for problem in ident.check(FULL, {"shard 1": shard([])})[0])
    assert any("full collection is empty" in problem for problem in ident.check([], good())[0])


def _write_artifact(root, index, *, attempt=None, outcome="passed", skip=()):
    name = f"backend-test-timings-{index}"
    if attempt is not None:
        name += f"-attempt-{attempt}"
    folder = root / name
    folder.mkdir()
    ids = FULL[(index - 1) * 3:index * 3]
    rows = [
        {"id": item, "outcome": outcome if item == ids[0] else "passed", "runs": 1}
        for item in ids
    ]
    files = {
        f"backend-shard-{index}-collected.txt": "\n".join(ids) + "\n",
        f"backend-shard-{index}-executed.jsonl": "".join(json.dumps(row) + "\n" for row in rows),
        f"backend-junit-{index}.xml": f'<testsuites><testsuite tests="{len(ids)}"/></testsuites>',
    }
    for name, text in files.items():
        if name not in skip:
            (folder / name).write_text(text, encoding="utf-8")


def _write_artifacts(root, count=2, skip=()):
    for index in range(1, count + 1):
        _write_artifact(root, index, skip=skip)


def _run(root, full_ids=FULL, shards=2):
    full = root / "full.txt"
    full.write_text("\n".join(full_ids) + "\n", encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), "--full", str(full), "--artifacts", str(root / "art"),
                           "--shards", str(shards)], capture_output=True, text=True, timeout=60)


def test_the_command_passes_a_complete_set_of_artifacts(tmp_path):
    (tmp_path / "art").mkdir()
    _write_artifacts(tmp_path / "art")
    done = _run(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "executed 6" in done.stdout


@pytest.mark.parametrize(("original", "rerun", "expected"), [
    ("failed", "passed", 0),
    ("failed", "failed", 1),
    ("passed", "failed", 1),
])
def test_the_gate_uses_the_newest_artifact_per_shard_for_a_partial_rerun(tmp_path, original, rerun, expected):
    art = tmp_path / "art"
    art.mkdir()
    _write_artifact(art, 1, attempt=1, outcome=original)
    _write_artifact(art, 2, attempt=1)
    _write_artifact(art, 1, attempt=2, outcome=rerun)
    done = _run(tmp_path)
    assert done.returncode == expected, done.stdout + done.stderr
    assert "shard 1: backend-test-timings-1-attempt-2" in done.stdout
    assert "shard 2: backend-test-timings-2-attempt-1" in done.stdout
    assert ("ended as failed" in done.stdout) == (expected == 1)


def test_the_gate_orders_attempts_numerically(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    _write_artifact(art, 1, attempt=10)
    _write_artifact(art, 1, attempt=9, outcome="failed")
    _write_artifact(art, 2, attempt=1)
    done = _run(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "shard 1: backend-test-timings-1-attempt-10" in done.stdout


def test_the_gate_refuses_a_newer_incomplete_artifact_even_with_an_older_success(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    _write_artifact(art, 1, attempt=1)
    _write_artifact(art, 2, attempt=1)
    _write_artifact(art, 1, attempt=2, skip=("backend-shard-1-executed.jsonl",))
    done = _run(tmp_path)
    assert done.returncode == 1
    assert "shard 1: backend-test-timings-1-attempt-2" in done.stdout
    assert "shard 1: backend-shard-1-executed.jsonl is missing or empty" in done.stdout


@pytest.mark.parametrize("folder", [
    "backend-test-timings-1-attempt-0",
    "backend-test-timings-1-attempt-last",
    "backend-test-timings-1-attempt-02",
    "backend-test-timings-3-attempt-1",
])
def test_the_gate_refuses_invalid_attempt_or_shard_names(tmp_path, folder):
    art = tmp_path / "art"
    art.mkdir()
    _write_artifact(art, 1, attempt=1)
    _write_artifact(art, 2, attempt=1)
    (art / folder).mkdir()
    done = _run(tmp_path)
    assert done.returncode == 1
    assert [line for line in done.stdout.splitlines() if line.startswith("PROBLEM:")] == [
        f"PROBLEM: unexpected artifact {folder}",
    ]


def test_the_gate_refuses_mixed_legacy_and_attempt_artifacts(tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    _write_artifacts(art)
    _write_artifact(art, 1, attempt=2)
    done = _run(tmp_path)
    assert done.returncode == 1
    assert "cannot mix legacy and attempt-named artifacts" in done.stdout


@pytest.mark.parametrize("skip", ["backend-shard-2-collected.txt", "backend-shard-2-executed.jsonl", "backend-junit-2.xml"])
def test_the_command_fails_when_an_artifact_file_is_missing(tmp_path, skip):
    (tmp_path / "art").mkdir()
    _write_artifacts(tmp_path / "art", skip=(skip,))
    done = _run(tmp_path)
    assert done.returncode != 0 and "missing or empty" in done.stdout


def test_the_command_fails_for_a_missing_extra_or_wrong_number_of_artifacts(tmp_path):
    (tmp_path / "art").mkdir()
    _write_artifacts(tmp_path / "art")
    assert "missing artifact" in _run(tmp_path, shards=3).stdout
    (tmp_path / "art" / "backend-test-timings-9").mkdir()
    assert "unexpected artifact" in _run(tmp_path).stdout


def test_the_command_fails_when_a_test_is_dropped_and_another_duplicated(tmp_path):
    (tmp_path / "art").mkdir()
    _write_artifacts(tmp_path / "art")
    done = _run(tmp_path, full_ids=FULL[:5] + [FULL[0]])    # the full collection differs from what the shards ran
    assert done.returncode != 0


def test_the_plugin_records_one_final_outcome_per_test(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken_setup():\n    raise RuntimeError('setup')\n"
        "@pytest.fixture\n"
        "def broken_teardown():\n    yield\n    raise RuntimeError('teardown')\n"
        "def test_passed():\n    pass\n"
        "def test_failed():\n    assert False\n"
        "@pytest.mark.skip(reason='x')\ndef test_skipped():\n    pass\n"
        "@pytest.mark.xfail\ndef test_xfailed():\n    assert False\n"
        "@pytest.mark.xfail\ndef test_xpassed():\n    pass\n"
        "def test_setup_error(broken_setup):\n    pass\n"
        "def test_teardown_error(broken_teardown):\n    pass\n"
        "@pytest.mark.parametrize('value', [1, 2])\ndef test_param(value):\n    pass\n",
        encoding="utf-8")
    out = tmp_path / "executed.jsonl"
    backend = str(ROOT / "backend")
    env = {"PYTHONPATH": backend, "PATH": __import__("os").environ["PATH"]}
    done = subprocess.run([sys.executable, "-m", "pytest", "test_sample.py", "-q", "-p", "no:cacheprovider",
                           "-p", "tests.ci_shard_identity", f"--identity-out={out}"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 1, done.stdout + done.stderr
    parsed, bad = ident.parse_executed(out.read_text(encoding="utf-8"))
    assert bad == []
    rows = {row["id"]: row for row in parsed}
    assert {name: row["outcome"] for name, row in rows.items()} == {
        "test_sample.py::test_passed": "passed", "test_sample.py::test_failed": "failed",
        "test_sample.py::test_skipped": "skipped", "test_sample.py::test_xfailed": "xfailed",
        "test_sample.py::test_xpassed": "xpassed", "test_sample.py::test_setup_error": "error",
        "test_sample.py::test_teardown_error": "error", "test_sample.py::test_param[1]": "passed",
        "test_sample.py::test_param[2]": "passed"}
    assert all(row["runs"] == 1 for row in rows.values()) and len(rows) == 9     # one line per test


def test_the_plugin_writes_nothing_without_the_option(tmp_path):
    (tmp_path / "test_sample.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    env = {"PYTHONPATH": str(ROOT / "backend"), "PATH": __import__("os").environ["PATH"]}
    done = subprocess.run([sys.executable, "-m", "pytest", "test_sample.py", "-q", "-p", "no:cacheprovider",
                           "-p", "tests.ci_shard_identity"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0 and list(tmp_path.glob("*.jsonl")) == []


def test_a_uuid_in_the_file_name_or_the_test_name_is_never_normalised():
    one = "tests/a/test_11111111-2222-3333-4444-555555555555.py::test_x[case]"
    two = "tests/a/test_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.py::test_x[case]"
    assert ident.normalise(one) != ident.normalise(two)
    assert ident.check([one], {"shard 1": shard([two], executed=[two])})[0]
    assert ident.normalise("t.py::test_11111111-2222-3333-4444-555555555555") == "t.py::test_11111111-2222-3333-4444-555555555555"
    assert ident.normalise("t.py::test_x[a-11111111-2222-3333-4444-555555555555]") == "t.py::test_x[a-<uuid>]"


def test_an_unknown_outcome_is_refused():
    for outcome in ("unknown", "", "PASSED"):
        problems = ident.check(FULL, {"shard 1": shard(FULL[:3], outcome=outcome), "shard 2": shard(FULL[3:])})[0]
        assert any("outcome" in problem for problem in problems), outcome


def test_a_missing_or_non_integer_run_count_is_refused_not_defaulted():
    rows = ['{"id": "t.py::a", "outcome": "passed"}', '{"id": "t.py::b", "outcome": "passed", "runs": "1"}',
            '{"id": "t.py::c", "outcome": "passed", "runs": true}', "not json", "[1]",
            '{"outcome": "passed", "runs": 1}', '{"id": 5, "outcome": "passed", "runs": 1}']
    parsed, bad = ident.parse_executed("\n".join(rows))
    assert parsed == [] and len(bad) == len(rows)
    ok, none = ident.parse_executed('{"id": "t.py::a", "outcome": "passed", "runs": 1}\n\n')
    assert none == [] and len(ok) == 1


def test_a_run_count_other_than_one_in_a_parsed_row_is_a_problem():
    shards = good()
    shards["shard 1"]["executed"][0].pop("runs")
    assert any("ran None" in problem for problem in ident.check(FULL, shards)[0])


def test_the_command_reports_malformed_executed_lines(tmp_path):
    (tmp_path / "art").mkdir()
    _write_artifacts(tmp_path / "art")
    path = tmp_path / "art" / "backend-test-timings-1" / "backend-shard-1-executed.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    done = _run(tmp_path)
    assert done.returncode != 0 and "is not JSON" in done.stdout

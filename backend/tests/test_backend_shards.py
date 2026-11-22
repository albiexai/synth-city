"""The backend test files split into shards that together run every file exactly once (#959).

A later change moves the hosted backend job onto parallel shards. These tests prove the assignment alone: the
shards partition the files, a new file is never left out, the plan is deterministic and balanced, and the one
ignored file stays out. The workflow is not touched here.
"""
from __future__ import annotations

import importlib.util
import statistics
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "backend_shards.py"
spec = importlib.util.spec_from_file_location("backend_shards", SCRIPT)
shards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shards)

FILES = shards.test_files()
WEIGHTS = shards.load_weights()


def _flat(plan):
    return [name for shard in plan for name in shard]


@pytest.mark.parametrize("count", range(1, 7))
def test_the_shards_partition_the_files_exactly(count):
    plan = shards.plan(FILES, WEIGHTS, count)
    assert len(plan) == count
    assert sorted(_flat(plan)) == FILES          # none missing, none twice
    assert all(plan) or count > len(FILES)       # no shard is empty while there are files to spread


def test_a_new_unweighted_file_lands_in_exactly_one_shard():
    new = "tests/agent_teams/test_a_file_added_later.py"
    plan = shards.plan([*FILES, new], WEIGHTS, 4)
    assert sum(new in shard for shard in plan) == 1
    assert sorted(_flat(plan)) == sorted([*FILES, new])


def test_a_weight_for_a_removed_file_does_not_break_the_plan():
    weights = {**WEIGHTS, "tests/test_deleted_long_ago.py": 500.0}
    assert shards.plan(FILES, weights, 4) == shards.plan(FILES, WEIGHTS, 4)


def test_an_unweighted_file_gets_the_median_weight():
    median = statistics.median(WEIGHTS[name] for name in FILES if name in WEIGHTS)
    assert shards.plan(["a_test.py", "b_test.py"], {}, 2) == [["a_test.py"], ["b_test.py"]]
    plan = shards.plan(["heavy.py", "new.py", "x.py", "y.py"], {"heavy.py": median * 3, "x.py": median, "y.py": median}, 2)
    # heavy alone balances new, x and y at the median weight each (3 x median)
    assert sorted(len(shard) for shard in plan) == [1, 3]


def test_the_plan_is_deterministic():
    assert shards.plan(FILES, WEIGHTS, 4) == shards.plan(list(reversed(FILES)), WEIGHTS, 4)


def test_four_shards_are_balanced_on_the_committed_weights():
    plan = shards.plan(FILES, WEIGHTS, 4)
    median = statistics.median(WEIGHTS.values())
    loads = [sum(WEIGHTS.get(name, median) for name in shard) for shard in plan]
    floor = max(WEIGHTS.values())                # the largest file cannot be split
    assert max(loads) <= max(1.35 * statistics.mean(loads), floor)


def test_the_ignored_real_sudo_file_is_in_no_shard():
    assert "tests/test_cc_bridge_real_sudo.py" not in FILES
    assert "tests/test_cc_bridge_real_sudo.py" not in _flat(shards.plan(FILES, WEIGHTS, 4))


def test_the_command_prints_one_shards_files_and_refuses_a_bad_shard():
    expected = shards.plan(FILES, WEIGHTS, 4)[1]
    done = subprocess.run([sys.executable, str(SCRIPT), "--shard", "2", "--of", "4"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and done.stdout.split() == expected
    bad = subprocess.run([sys.executable, str(SCRIPT), "--shard", "5", "--of", "4"], capture_output=True, text=True, timeout=60)
    assert bad.returncode != 0


def test_the_file_pattern_is_the_one_pytest_collects_by_default(tmp_path):
    tests = tmp_path / "backend" / "tests"
    (tests / "sub").mkdir(parents=True)
    (tests / "__pycache__").mkdir()
    for name in ("test_one.py", "two_test.py", "sub/test_three.py", "sub/four_test.py",
                 "conftest.py", "helper.py", "sub/test_notes.txt", "__pycache__/test_cached.py"):
        (tests / name).write_text("", encoding="utf-8")
    assert shards.test_files(tests) == ["tests/sub/four_test.py", "tests/sub/test_three.py",
                                        "tests/test_one.py", "tests/two_test.py"]

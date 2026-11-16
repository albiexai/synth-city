#!/usr/bin/env python3
"""Split the backend test files into balanced shards (#959).

Whole files go to shards, largest weight first, each to the currently lightest shard. The weights are seconds
per file from the hosted JUnit timings (``backend_shard_weights.json``). A file with no weight gets the median
weight, so a new test file is always selected by exactly one shard and none can be left out. The plan depends
only on the file list and the weights, so every shard computes the same plan.

    python scripts/ci/backend_shards.py --shard 2 --of 4      # prints the test files of shard 2, one per line
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESTS = ROOT / "backend" / "tests"
WEIGHTS = Path(__file__).with_name("backend_shard_weights.json")
# The files pytest collects by default, and the one file the backend job already ignores.
PATTERNS = ("test_*.py", "*_test.py")
IGNORED = ("tests/test_cc_bridge_real_sudo.py",)


def test_files(tests: Path = TESTS) -> list[str]:
    """Test file paths relative to ``backend``, sorted, without the ignored ones."""
    base = tests.parent
    found = {
        path.relative_to(base).as_posix()
        for path in tests.rglob("*.py")
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in PATTERNS)
        and "__pycache__" not in path.parts
    }
    return sorted(found - set(IGNORED))


def load_weights(path: Path = WEIGHTS) -> dict[str, float]:
    return {name: float(seconds) for name, seconds in json.loads(path.read_text(encoding="utf-8")).items()}


def plan(files: list[str], weights: dict[str, float], count: int) -> list[list[str]]:
    """``count`` lists of files; every file is in exactly one list."""
    if count < 1:
        raise ValueError("the shard count must be at least 1")
    known = [weights[name] for name in files if name in weights]
    default = statistics.median(known) if known else 1.0
    ordered = sorted(files, key=lambda name: (-weights.get(name, default), name))
    shards: list[list[str]] = [[] for _ in range(count)]
    loads = [0.0] * count
    for name in ordered:
        lightest = min(range(count), key=lambda index: (loads[index], index))
        shards[lightest].append(name)
        loads[lightest] += weights.get(name, default)
    return [sorted(shard) for shard in shards]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shard", type=int, required=True, help="1-based shard number")
    parser.add_argument("--of", type=int, required=True, dest="count", help="number of shards")
    args = parser.parse_args(argv)
    if not 1 <= args.shard <= args.count:
        parser.error("--shard must be between 1 and --of")
    for name in plan(test_files(), load_weights(), args.count)[args.shard - 1]:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())

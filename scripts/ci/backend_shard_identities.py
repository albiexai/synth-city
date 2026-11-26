#!/usr/bin/env python3
"""Prove that the backend shards together ran every test exactly once (#1013).

Inputs: the node ids of the full collection, and for every shard the node ids it collected, the node ids it
executed with their final outcome (``backend/tests/ci_shard_identity.py``) and its JUnit file. Identities are
compared as multisets, so a dropped test and a duplicated test cannot cancel each other. The only dynamic part
of an id is a uuid inside a few parameter ids; ids that contain one are compared with the uuid replaced by
``<uuid>``, and two distinct tests that normalise to the same key are reported and fail.

With a known-flakes registry, the gate reconciles pytest's exit status and JUnit failure counts.
It tolerates only a test-call failure with an exact node ID and a full signature match.

    python scripts/ci/backend_shard_identities.py --full full.txt --artifacts DIR --shards 4 --registry known-flakes.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ARTIFACT_NAME = re.compile(r"backend-test-timings-([1-9][0-9]*)(?:-attempt-([1-9][0-9]*))?")
FAILING = ("failed", "error")
ALLOWED = ("passed", "skipped", "xfailed", "xpassed")   # every other outcome, known or not, is a problem
SHOWN = 20


def normalise(node_id: str) -> str:
    """Replace a uuid only inside the parameter suffix (from the first '['); the path and the test name stay raw."""
    head, bracket, params = node_id.partition("[")
    return head + bracket + UUID.sub("<uuid>", params)


def parse_collected(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "::" in line]


def parse_executed(text: str) -> tuple[list[dict], list[str]]:
    """Strict records: an object with a string id, a string outcome and an integer run count. Anything else is a problem."""
    rows, problems = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            problems.append(f"executed line {number} is not JSON")
            continue
        if not (isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("outcome"), str)
                and isinstance(row.get("runs"), int) and not isinstance(row.get("runs"), bool)):
            problems.append(f"executed line {number} is not an object with a string id, a string outcome and an integer runs")
            continue
        if "failure_signature" in row:
            signature = row["failure_signature"]
            if row["outcome"] not in FAILING or not isinstance(signature, str) or not signature.strip():
                problems.append(f"executed line {number} has an invalid failure signature")
                continue
        rows.append(row)
    return rows, problems


def _diff(expected: Counter, actual: Counter, label: str) -> list[str]:
    problems = []
    dropped = sorted(key for key in expected if actual[key] < expected[key])
    extra = sorted(key for key in actual if actual[key] > expected[key])
    if dropped:
        problems.append(f"{label}: {len(dropped)} identities missing or fewer than expected, e.g. {dropped[:SHOWN]}")
    if extra:
        problems.append(f"{label}: {len(extra)} identities more frequent than expected, e.g. {extra[:SHOWN]}")
    return problems


def _matches_flake(row: dict, flakes: dict[str, re.Pattern]) -> bool:
    pattern = flakes.get(row["id"])
    signature = row.get("failure_signature")
    return (row["outcome"] == "failed" and pattern is not None and isinstance(signature, str)
            and bool(signature.strip()) and pattern.fullmatch(signature) is not None)


def check(full: list[str], shards: dict[str, dict], flakes: dict[str, re.Pattern] | None = None) -> tuple[list[str], dict]:
    """Check exactly-once execution and, when given a registry, reconcile failure evidence."""
    problems: list[str] = []
    full_keys = Counter(normalise(item) for item in full)
    if not full:
        problems.append("the full collection is empty")
    for key, count in sorted(full_keys.items()):
        if count > 1:
            problems.append(f"collision: {count} distinct tests normalise to {key!r}; identities cannot tell them apart")
    collected_total, executed_total, skipped = Counter(), Counter(), 0
    tolerated = 0
    for name, shard in sorted(shards.items()):
        collected = Counter(normalise(item) for item in shard.get("collected", []))
        executed_rows = shard.get("executed", [])
        executed = Counter(normalise(row["id"]) for row in executed_rows if isinstance(row.get("id"), str))
        if not collected:
            problems.append(f"{name}: no collected identities")
        if not executed_rows:
            problems.append(f"{name}: no executed identities")
        problems += _diff(collected, executed, f"{name} executed against collected")
        problems += _diff(executed, collected, f"{name} collected against executed")
        if flakes is not None:
            failures = sum(row["outcome"] == "failed" for row in executed_rows)
            errors = sum(row["outcome"] == "error" for row in executed_rows)
            if shard.get("pytest_exit") != ("1" if failures or errors else "0"):
                problems.append(f"{name}: pytest exit status is missing or disagrees with the recorded outcomes")
            if shard.get("junit_failures") != failures or shard.get("junit_errors") != 0:
                problems.append(f"{name}: JUnit does not account for the recorded test failures without errors")
        for row in executed_rows:
            if row.get("runs") != 1:
                problems.append(f"{name}: {row.get('id')} ran {row.get('runs')!r} times")
            if row.get("outcome") in FAILING:
                if flakes is not None and _matches_flake(row, flakes):
                    tolerated += 1
                else:
                    problems.append(f"{name}: {row['id']} ended as {row['outcome']}")
            elif row.get("outcome") not in ALLOWED:
                problems.append(f"{name}: {row.get('id')} has the outcome {row.get('outcome')!r}")
        skipped += sum(1 for row in executed_rows if row.get("outcome") == "skipped")
        junit = shard.get("junit_tests")
        if junit is None:
            problems.append(f"{name}: no readable JUnit file")
        elif junit != sum(collected.values()):
            problems.append(f"{name}: JUnit counts {junit} tests, the shard collected {sum(collected.values())}")
        collected_total.update(collected)
        executed_total.update(executed)
    problems += _diff(full_keys, collected_total, "shards collected against the full collection")
    problems += _diff(full_keys, executed_total, "shards executed against the full collection")
    problems += _diff(collected_total, full_keys, "full collection against the shards collected")
    summary = {"full": len(full), "collected": sum(collected_total.values()), "executed": sum(executed_total.values()),
               "skipped": skipped, "shards": len(shards), "tolerated": tolerated}
    return problems, summary


def _junit_counts(path: Path) -> dict[str, int]:
    try:
        root = ET.parse(path).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        counts = {}
        for field in ("tests", "failures", "errors"):
            if suites and all(field in suite.attrib for suite in suites):
                values = [int(suite.attrib[field]) for suite in suites]
                if all(value >= 0 for value in values):
                    counts[f"junit_{field}"] = sum(values)
        return counts
    except (OSError, ET.ParseError, ValueError):
        return {}


def _latest_artifacts(artifacts: Path, count: int) -> tuple[dict[int, Path], list[str]]:
    latest: dict[int, tuple[int, Path]] = {}
    problems = []
    layouts = set()
    folders = sorted(path for path in artifacts.iterdir() if path.is_dir()) if artifacts.is_dir() else []
    for folder in folders:
        match = ARTIFACT_NAME.fullmatch(folder.name)
        if match is None or int(match[1]) > count:
            problems.append(f"unexpected artifact {folder.name}")
            continue
        index = int(match[1])
        attempt = int(match[2] or 0)
        layouts.add(attempt > 0)
        if index not in latest or attempt > latest[index][0]:
            latest[index] = (attempt, folder)
    if len(layouts) > 1:
        problems.append("cannot mix legacy and attempt-named artifacts")
    return {index: folder for index, (_, folder) in latest.items()}, problems


def load_shards(artifacts: Path, count: int) -> tuple[dict[str, dict], list[str]]:
    """Read the newest artifact per shard, or a complete legacy set from one attempt."""
    selected, problems = _latest_artifacts(artifacts, count)
    shards = {}
    for index in range(1, count + 1):
        folder = selected.get(index)
        if folder is None:
            problems.append(f"missing artifact for shard {index}")
            continue
        files = {"collected": folder / f"backend-shard-{index}-collected.txt",
                 "executed": folder / f"backend-shard-{index}-executed.jsonl",
                 "junit": folder / f"backend-junit-{index}.xml"}
        text = {}
        for kind, path in files.items():
            if not path.is_file() or path.stat().st_size == 0:
                problems.append(f"shard {index}: {path.name} is missing or empty")
            elif kind != "junit":
                text[kind] = path.read_text(encoding="utf-8")
        executed, bad_rows = parse_executed(text.get("executed", ""))
        problems += [f"shard {index}: {message}" for message in bad_rows]
        exit_file = folder / f"backend-shard-{index}-pytest-exit.txt"
        shards[f"shard {index}"] = {
            "artifact": folder.name,
            "collected": parse_collected(text.get("collected", "")),
            "executed": executed,
            "pytest_exit": exit_file.read_text(encoding="utf-8").strip() if exit_file.is_file() else None,
            **_junit_counts(files["junit"]),
        }
    return shards, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", type=Path, required=True, help="node ids of the full collection, one per line")
    parser.add_argument("--artifacts", type=Path, required=True, help="directory holding one folder per shard artifact")
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--registry", type=Path, help="the workflow tree's known-flakes registry")
    args = parser.parse_args(argv)
    flakes = None
    if args.registry is not None:
        from known_flakes import read_registry, validate

        try:
            registry = read_registry(args.registry)
        except (OSError, ValueError) as exc:
            print(f"Could not read the known-flakes registry ({exc})", file=sys.stderr)
            return 1
        errors = validate(registry, datetime.now(timezone.utc).date())
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        flakes = {entry["test_id"]: re.compile(entry["failure_signature"]) for entry in registry}
    shards, problems = load_shards(args.artifacts, args.shards)
    for name, shard in shards.items():
        print(f"{name}: {shard['artifact']}")
    full = parse_collected(args.full.read_text(encoding="utf-8")) if args.full.is_file() else []
    more, summary = check(full, shards, flakes)
    problems += more
    print(f"shards {summary['shards']}; full collection {summary['full']}; collected {summary['collected']}; "
          f"executed {summary['executed']}; skipped {summary['skipped']}; tolerated known failures {summary['tolerated']}")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

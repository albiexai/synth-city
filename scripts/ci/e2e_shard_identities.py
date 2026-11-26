#!/usr/bin/env python3
"""Prove that Playwright's shards cover each active journey exactly once.

    python scripts/ci/e2e_shard_identities.py --shards 4 --output journeys.json
    python scripts/ci/e2e_shard_identities.py --shards 4 --output journeys.json --artifacts shard-artifacts

With a separate checker checkout, --frontend selects the candidate's Playwright directory.

Version 1 stores sorted identities as [project id, relative file, ...suite titles,
test title]. The full list and each shard's list use the same identity format.
The producer and aggregate can check the same registry, report and runner exit status.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
LIST_COMMAND = ["npx", "--no-install", "playwright", "test", "--list", "--grep", "@active", "--reporter=json"]
Identity = tuple[str, ...]
Field = TypeVar("Field")
ARTIFACT_NAME = re.compile(r"e2e-journeys-([1-9][0-9]*)-attempt-([1-9][0-9]*)")
TERMINAL_COLOR = re.compile(r"\x1b\[[0-9;]*m")


def field(value: object, key: str, kind: type[Field], *, default: Field | None = None) -> Field:
    if not isinstance(value, dict):
        raise ValueError(f"expected an object with {key!r}")
    item = value.get(key, default)
    if not isinstance(item, kind):
        raise ValueError(f"expected {key!r} to be a {kind.__name__}")
    return item


def suite_tests(suite: object, titles: tuple[str, ...] = ()) -> Iterator[tuple[Identity, dict]]:
    for spec in field(suite, "specs", list):
        file = field(spec, "file", str)
        title = field(spec, "title", str)
        tests = field(spec, "tests", list)
        if not file or not tests:
            raise ValueError("an entry has no file or project tests")
        for test in tests:
            # The reporter can group the same spec from multiple projects into one entry.
            yield (field(test, "projectId", str), file, *titles, title), test
    for child in field(suite, "suites", list, default=[]):
        yield from suite_tests(child, (*titles, field(child, "title", str)))


def read_report(path: Path, label: str, *, listing: bool = False) -> list[tuple[Identity, dict]]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        errors = field(report, "errors", list)
        tests = [entry for suite in field(report, "suites", list) for entry in suite_tests(suite)]
    except OSError as exc:
        raise ValueError(f"{label}: could not read the Playwright report") from exc
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{label}: invalid Playwright report ({exc})") from exc
    if errors:
        description = "collection errors" if listing else "errors"
        raise ValueError(f"{label}: Playwright reported {description}")
    return tests


def collect(label: str, directory: Path, frontend: Path, shard: str | None = None) -> list[Identity]:
    report_path = directory / "collection.json"
    report_path.unlink(missing_ok=True)
    command = [*LIST_COMMAND, f"--shard={shard}"] if shard else LIST_COMMAND
    result = subprocess.run(
        command, cwd=frontend, env={**os.environ, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(report_path)},
        stdout=subprocess.DEVNULL, check=False, timeout=120,
    )
    if result.returncode:
        raise ValueError(f"{label}: Playwright exited with status {result.returncode}")
    journeys = [identity for identity, _ in read_report(report_path, label, listing=True)]
    if not journeys:
        raise ValueError(f"{label} collection is empty")
    return sorted(journeys)


def prove_partition(journeys: list[Identity], shards: dict[str, list[Identity]]) -> None:
    expected = Counter(journeys)
    collected = Counter(identity for shard in shards.values() for identity in shard)
    collisions = sorted(identity for identity, count in expected.items() if count > 1)
    missing = sorted(expected.keys() - collected.keys())
    duplicates = sorted(identity for identity, count in collected.items() if count > 1)
    unexpected = sorted(collected.keys() - expected.keys())
    problems = [
        f"{label} ({len(items)}), e.g. {items[:5]!r}"
        for label, items in (
            ("full inventory identity collision", collisions),
            ("missing from shards", missing),
            ("duplicate in shards", duplicates),
            ("unexpected in shards", unexpected),
        )
        if items
    ]
    if problems:
        raise ValueError("\n".join(problems))


def latest_reports(directory: Path, count: int) -> dict[str, Path]:
    selected: dict[int, tuple[int, Path]] = {}
    for folder in sorted(directory.iterdir()):
        match = ARTIFACT_NAME.fullmatch(folder.name)
        if match is None or not folder.is_dir() or int(match[1]) > count:
            raise ValueError(f"unexpected e2e journey artifact {folder.name!r}")
        shard, attempt = map(int, match.groups())
        if shard not in selected or selected[shard][0] < attempt:
            selected[shard] = (attempt, folder / "results.json")
    for shard in range(1, count + 1):
        if shard not in selected:
            raise ValueError(f"missing artifact for shard {shard}")
    return {f"{shard}/{count}": selected[shard][1] for shard in range(1, count + 1)}


def registered_patterns(registry: list[dict], journeys: list[Identity]) -> dict[str, re.Pattern]:
    counts = Counter(identity[-1] for identity in journeys)
    return {entry["test_id"]: re.compile(entry["failure_signature"]) for entry in registry
            if counts[entry["test_id"]] == 1}


def matching_failure(result: dict, pattern: re.Pattern | None) -> bool:
    if pattern is None or result.get("status") not in ("failed", "timedOut"):
        return False
    errors, error = result.get("errors"), result.get("error")
    if not isinstance(errors, list) or len(errors) != 1 or not isinstance(errors[0], dict):
        return False
    reported = errors[0].get("message")
    if not isinstance(reported, str) or not reported.strip():
        return False
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str):
        return False
    signature = TERMINAL_COLOR.sub("", message).strip()
    return bool(signature) and pattern.fullmatch(signature) is not None


def verified_report(report: Path, label: str, patterns: dict[str, re.Pattern] | None) -> list[Identity]:
    identities = []
    tolerated = 0
    for identity, test in read_report(report, label):
        invalid = f"{label}: expected one passing execution or registered failure for {identity!r}"
        results = test.get("results")
        if (test.get("expectedStatus") != "passed" or not isinstance(results, list)
                or len(results) != 1 or not isinstance(results[0], dict)):
            raise ValueError(invalid)
        result = results[0]
        retry, worker = result.get("retry"), result.get("workerIndex")
        if type(retry) is not int or retry != 0 or (patterns is not None and (type(worker) is not int or worker < 0)):
            raise ValueError(invalid)
        if test.get("status") == "expected" and result.get("status") == "passed":
            if result.get("error") is not None or result.get("errors", []) != []:
                raise ValueError(invalid)
        elif (patterns is not None and test.get("status") == "unexpected"
              and matching_failure(result, patterns.get(identity[-1]))):
            tolerated += 1
        else:
            raise ValueError(invalid)
        identities.append(identity)
    if patterns is not None:
        try:
            exit_status = report.with_name("playwright-exit.txt").read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"{label}: could not read the Playwright exit status") from exc
        if exit_status != ("1" if tolerated else "0"):
            raise ValueError(f"{label}: Playwright exit status disagrees with the recorded outcomes")
    print(f"{label.capitalize()} reported {len(identities)} journeys via {report.parent.name}; registered failures: {tolerated}.")
    return sorted(identities)


def verify_executions(directory: Path, journeys: list[Identity], planned: dict[str, list[Identity]],
                      patterns: dict[str, re.Pattern] | None = None) -> None:
    executed = {shard: verified_report(report, f"shard {shard}", patterns)
                for shard, report in latest_reports(directory, len(planned)).items()}
    prove_partition(journeys, executed)
    for shard, identities in executed.items():
        if identities != planned[shard]:
            raise ValueError(f"shard {shard}: execution does not match the planned shard")


def write_manifest(path: Path, journeys: list[Identity], shards: dict[str, list[Identity]]) -> None:
    stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False)
    temporary = Path(stream.name)
    try:
        with stream:
            manifest = {"version": 1, "journeys": journeys, "shards": shards}
            json.dump(manifest, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shards", type=int, required=True, help="number of planned Playwright shards")
    parser.add_argument("--output", type=Path, help="manifest path for the full run (directory must exist)")
    parser.add_argument("--artifacts", type=Path, help="prove execution from the latest report per shard")
    parser.add_argument("--frontend", type=Path, default=FRONTEND, help="candidate Playwright frontend directory")
    parser.add_argument("--shard", type=int, help="verify one producer shard")
    parser.add_argument("--report", type=Path, help="Playwright JSON report for --shard")
    parser.add_argument("--registry", type=Path, help="the workflow tree's known-flakes registry")
    args = parser.parse_args()
    if args.shards < 1:
        parser.error("--shards must be positive")
    if args.shard is not None:
        if not 1 <= args.shard <= args.shards or args.report is None or args.registry is None:
            parser.error("--shard requires a valid shard number, --report and --registry")
        if args.output is not None or args.artifacts is not None:
            parser.error("--output and --artifacts describe the full run")
    elif args.output is None or args.report is not None:
        parser.error("the full run requires --output, without --report")

    try:
        registry = None
        if args.registry is not None:
            from known_flakes import read_registry, validate

            registry = read_registry(args.registry)
            errors = validate(registry, datetime.now(timezone.utc).date())
            if errors:
                raise ValueError("\n".join(errors))
        frontend = args.frontend.resolve()
        with tempfile.TemporaryDirectory(prefix="e2e-journeys-") as directory:
            if args.shard is not None:
                shard = f"{args.shard}/{args.shards}"
                planned = collect(f"shard {shard}", Path(directory), frontend, shard)
                inventory = collect("full", Path(directory), frontend) if registry else planned
                patterns = registered_patterns(registry, inventory)
                executed = verified_report(args.report, f"shard {shard}", patterns)
                prove_partition(planned, {shard: executed})
                return 0
            journeys = collect("full", Path(directory), frontend)
            shards = {}
            for index in range(1, args.shards + 1):
                shard = f"{index}/{args.shards}"
                shards[shard] = collect(f"shard {shard}", Path(directory), frontend, shard)
        prove_partition(journeys, shards)
        if args.artifacts is not None:
            patterns = registered_patterns(registry, journeys) if registry is not None else None
            verify_executions(args.artifacts, journeys, shards, patterns)
        write_manifest(args.output, journeys, shards)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Journey manifest failed: {exc}", file=sys.stderr)
        return 1

    counts = ", ".join(f"{shard}: {len(items)}" for shard, items in shards.items())
    proof = "execution" if args.artifacts is not None else "partition"
    print(f"Proved the {proof} of {len(journeys)} active journeys across {len(shards)} shards ({counts}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

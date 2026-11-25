#!/usr/bin/env python3
"""Prove that Playwright's planned shards collect each active journey exactly once.

    python scripts/ci/e2e_shard_identities.py --shards 4 --output journeys.json

Version 1 stores sorted identities as [project id, relative file, ...suite titles,
test title]. The full list and each shard's list use the same identity format.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import TypeVar

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
LIST_COMMAND = ["npx", "--no-install", "playwright", "test", "--list", "--grep", "@active", "--reporter=json"]
Identity = tuple[str, ...]
Field = TypeVar("Field")


def field(value: object, key: str, kind: type[Field], *, default: Field | None = None) -> Field:
    if not isinstance(value, dict):
        raise ValueError(f"expected an object with {key!r}")
    item = value.get(key, default)
    if not isinstance(item, kind):
        raise ValueError(f"expected {key!r} to be a {kind.__name__}")
    return item


def suite_identities(suite: object, titles: tuple[str, ...] = ()) -> Iterator[Identity]:
    for spec in field(suite, "specs", list):
        file = field(spec, "file", str)
        title = field(spec, "title", str)
        tests = field(spec, "tests", list)
        if not file or not tests:
            raise ValueError("an entry has no file or project tests")
        for test in tests:
            # The reporter can group the same spec from multiple projects into one entry.
            yield (field(test, "projectId", str), file, *titles, title)
    for child in field(suite, "suites", list, default=[]):
        yield from suite_identities(child, (*titles, field(child, "title", str)))


def collect(label: str, directory: Path, shard: str | None = None) -> list[Identity]:
    report_path = directory / "collection.json"
    report_path.unlink(missing_ok=True)
    command = [*LIST_COMMAND, f"--shard={shard}"] if shard else LIST_COMMAND
    result = subprocess.run(
        command, cwd=FRONTEND, env={**os.environ, "PLAYWRIGHT_JSON_OUTPUT_FILE": str(report_path)},
        stdout=subprocess.DEVNULL, check=False, timeout=120,
    )
    if result.returncode:
        raise ValueError(f"{label}: Playwright exited with status {result.returncode}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        errors = field(report, "errors", list)
        journeys = [
            identity
            for suite in field(report, "suites", list)
            for identity in suite_identities(suite)
        ]
    except OSError as exc:
        raise ValueError(f"{label}: could not read the Playwright report") from exc
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{label}: invalid Playwright report ({exc})") from exc
    if errors:
        raise ValueError(f"{label}: Playwright reported collection errors")
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
    parser.add_argument("--output", type=Path, required=True, help="manifest path (parent directory must exist)")
    args = parser.parse_args()
    if args.shards < 1:
        parser.error("--shards must be positive")

    try:
        with tempfile.TemporaryDirectory(prefix="e2e-journeys-") as directory:
            journeys = collect("full", Path(directory))
            shards = {}
            for index in range(1, args.shards + 1):
                shard = f"{index}/{args.shards}"
                shards[shard] = collect(f"shard {shard}", Path(directory), shard)
        prove_partition(journeys, shards)
        write_manifest(args.output, journeys, shards)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"Journey manifest failed: {exc}", file=sys.stderr)
        return 1

    counts = ", ".join(f"{shard}: {len(items)}" for shard, items in shards.items())
    print(f"Proved the partition of {len(journeys)} active journeys across {len(shards)} shards ({counts}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Render the queue waits for the required jobs of one Actions run attempt."""

import argparse
import json
import sys
from datetime import datetime


REQUIRED_JOBS = {
    "Backend regression tests": ("Full backend regression tests",),
    "Product CI": ("Product CI gate",),
    "Factory source checks": ("Changed Python undefined names", "Rename old-name guard"),
    "E2E": ("e2e",),
}


def queue_seconds(job: dict) -> int | None:
    try:
        created = datetime.fromisoformat(job["created_at"])
        started = datetime.fromisoformat(job["started_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if created.tzinfo is None or started.tzinfo is None or started < created:
        return None
    return int((started - created).total_seconds())


def render(workflow: str, pages: list[dict]) -> str:
    lines = ["## Required job queue time", "", "| Required job | Queue wait |", "| --- | ---: |"]
    jobs = [job for page in pages for job in page["jobs"]]
    for name in REQUIRED_JOBS[workflow]:
        matches = [job for job in jobs if job.get("name") == name]
        seconds = queue_seconds(matches[0]) if len(matches) == 1 else None
        wait = f"{seconds} s" if seconds is not None else "Unknown"
        lines.append(f"| {name} | {wait} |")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", choices=REQUIRED_JOBS)
    args = parser.parse_args()
    print(render(args.workflow, json.load(sys.stdin)))

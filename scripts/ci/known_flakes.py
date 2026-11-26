"""Validate the CI known-flakes registry and enforce UTC expiry dates."""

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path


_FIELDS = {"test_id", "failure_signature", "issue", "expires_on"}


def _unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate field in a registry record")
    return fields


def _expiry(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            if parsed.isoformat() == value:
                return parsed
    raise ValueError("expires_on must be a YYYY-MM-DD date or null")


def _record(entry) -> tuple[str, int, date | None]:
    if not isinstance(entry, dict) or entry.keys() != _FIELDS:
        raise ValueError("expected exactly test_id, failure_signature, issue and expires_on")
    test_id = entry["test_id"]
    if not isinstance(test_id, str) or not test_id.strip():
        raise ValueError("test_id must be a nonempty string")
    issue = entry["issue"]
    if type(issue) is not int or issue <= 0:
        raise ValueError("issue must be a positive integer")
    signature = entry["failure_signature"]
    if not isinstance(signature, str) or not signature.strip():
        raise ValueError("failure_signature must be a nonempty regex string")
    try:
        re.compile(signature)
    except (re.error, OverflowError, RecursionError) as exc:
        raise ValueError("failure_signature is not a valid regex") from exc
    return test_id, issue, _expiry(entry["expires_on"])


def validate(registry, today: date) -> list[str]:
    if not isinstance(registry, list):
        return ["expected an array of known-flake records"]
    errors = []
    seen = set()
    for number, entry in enumerate(registry, start=1):
        try:
            test_id, issue, expiry = _record(entry)
        except ValueError as exc:
            errors.append(f"Registry record {number} is invalid ({exc})")
            continue
        if test_id in seen:
            errors.append(f"Duplicate test ID {test_id!r} (issue #{issue})")
        seen.add(test_id)
        if expiry is not None and expiry < today:
            errors.append(f"Known flake {test_id!r} (issue #{issue}) expired at the end of {expiry} UTC")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry", type=Path)
    args = parser.parse_args()
    try:
        registry = json.loads(args.registry.read_text(encoding="utf-8"), object_pairs_hook=_unique_fields)
    except (OSError, ValueError) as exc:
        print(f"Could not read the known-flakes registry ({exc})", file=sys.stderr)
        return 1
    errors = validate(registry, datetime.now(timezone.utc).date())
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    sys.exit(main())

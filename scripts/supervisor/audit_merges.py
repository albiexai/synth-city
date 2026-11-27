#!/usr/bin/env python3
"""Audit exact-head acceptance for a Supervisor. GitHub label restoration is opt-in."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlencode

import _common as common

PAGE_SIZE = 100
MAX_PAGES = 30
PULL_FIELDS = (
    "{number, state, draft, merged_at, head: .head.sha, base: .base.ref, "
    "repo: .base.repo.full_name, labels: [.labels[].name]}"
)

# Match the latest active verdict and current slot rules in github_head_acceptance_service.
ACCEPTANCE = """
SELECT EXISTS (
    SELECT 1 FROM github_head_reviews AS review
    JOIN mail_team_members AS member ON member.id = review.reviewer_member_id
    JOIN agent_team_slots AS slot ON slot.id = member.team_slot_id
    WHERE review.id = (
        SELECT fact.id FROM github_head_reviews AS fact
        WHERE fact.work_item_id = item.id AND fact.dispatch_nonce = item.dispatch_nonce
          AND fact.pr_number = item.pr_number AND fact.head_sha = ? AND fact.status = 'active'
        ORDER BY fact.id DESC LIMIT 1
    )
      AND review.verdict = 'accepted' AND slot.enabled = 1
      AND member.team_preset_id = scope.preset_id AND slot.preset_id = scope.preset_id
      AND lower(trim(slot.role, char(32, 9, 13, 10))) = 'verifier'
      AND slot.id IS NOT item.owner_slot_id AND slot.id IS NOT team.leader_slot_id
      AND member.id = (
        SELECT occupant.id FROM mail_team_members AS occupant WHERE occupant.team_slot_id = slot.id
        ORDER BY occupant.updated_at DESC, occupant.id DESC LIMIT 1
      )
)
FROM github_work_items AS item
JOIN team_github_scopes AS scope ON scope.id = item.scope_id
JOIN agent_team_presets AS team ON team.id = scope.preset_id
WHERE item.scope_id = ? AND item.pr_number = ?
LIMIT 2
"""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise common.ConfigError("invalid arguments. See --help.")


def request(path, projection, *, gh_run, env, payload=None):
    method = "GET" if payload is None else "POST"
    command = ["gh", "api", "--hostname", "github.com", "--method", method, path, "--jq", projection]
    if payload is not None:
        command += ["--input", "-"]
    try:
        response = gh_run(command, input=json.dumps(payload) if payload is not None else None,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=True,
                          timeout=common.TIMEOUT_SECONDS, env=env)
        if len(response.stdout) > common.MAX_BYTES:
            raise common.StepError("GitHub response is too large")
        return json.loads(response.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        raise common.StepError("GitHub request failed or returned unreadable data") from None


def scope_config(db, scope_id):
    row = db.execute("""
        SELECT repo_owner, repo_name, base_ref, review_hold_label, enabled, preset_id
        FROM team_github_scopes WHERE id = ?
    """, (scope_id,)).fetchone()
    if row is None or not all(isinstance(part, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                              and part not in (".", "..") for part in row[:2]):
        raise common.ConfigError("scope has no valid GitHub repository")
    return row


def acceptance(db, scope_id, pull):
    rows = db.execute(ACCEPTANCE, (pull["head"], scope_id, pull["number"])).fetchall()
    return bool(rows[0][0]) if len(rows) == 1 else None


def checked_pull(pull):
    if not (
        isinstance(pull, dict) and type(pull.get("number")) is int and 0 < pull["number"] < 2**63
        and pull.get("state") in ("open", "closed") and type(pull.get("draft")) is bool
        and all(isinstance(pull.get(key), str) and pull[key] for key in ("head", "base", "repo"))
        and re.fullmatch(r"[0-9a-f]{40}", pull["head"])
        and isinstance(pull.get("labels"), list) and all(isinstance(label, str) for label in pull["labels"])
        and "merged_at" in pull and (
            pull["merged_at"] is None or (pull["state"] == "closed" and isinstance(pull["merged_at"], str)
            and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", pull["merged_at"]))
        )
    ):
        raise common.StepError("incomplete GitHub pull request evidence")
    return pull


def list_pulls(api, repo, base):
    pulls = {}
    for page in range(1, MAX_PAGES + 1):
        query = urlencode({"state": "all", "base": base, "sort": "created", "direction": "asc",
                           "per_page": PAGE_SIZE, "page": page})
        rows = api(f"repos/{repo}/pulls?{query}", f"map({PULL_FIELDS})")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise common.StepError("incomplete GitHub pull request page")
        for row in rows:
            pull = checked_pull(row)
            number = pull["number"]
            if number in pulls or pull["base"] != base or pull["repo"].lower() != repo.lower():
                raise common.StepError("GitHub pull request listing changed or does not match the scope")
            pulls[number] = pull
        if len(rows) < PAGE_SIZE:
            return pulls.values()
    raise common.StepError("GitHub pull request listing exceeded the page limit")


def held(pull, label):
    return isinstance(label, str) and label.lower() in {name.lower() for name in pull["labels"]}


def restore(db, api, scope_id, config, repo, pull, out):
    number = pull["number"]
    current = checked_pull(api(f"repos/{repo}/pulls/{number}", PULL_FIELDS))
    if (current["state"] != "open" or current["draft"] or current["merged_at"] is not None
            or any(current[key] != pull[key] for key in ("number", "repo", "base", "head"))
            or held(current, config[3]) or scope_config(db, scope_id) != config
            or acceptance(db, scope_id, current) is not False):
        out(f"PR #{number} changed during the audit. Restoration skipped.")
        return
    label = config[3]
    labels = api(f"repos/{repo}/issues/{number}/labels", "map(.name)", payload={"labels": [label]})
    if not isinstance(labels, list) or label not in labels:
        raise common.StepError("GitHub did not confirm the added hold")
    out(f"PR #{number} hold restored.")


def audit(db, api, scope_id, restore_hold, out):
    config = scope_config(db, scope_id)
    owner, repo_name, base_ref, label, enabled, _preset_id = config
    if restore_hold and (enabled != 1 or not isinstance(label, str) or not label.strip()):
        raise common.ConfigError("restoration needs an enabled scope with a configured review hold")
    repo = f"{owner}/{repo_name}"
    base = (api(f"repos/{repo}", ".default_branch") if base_ref == "origin/HEAD"
            else base_ref.removeprefix("origin/") if isinstance(base_ref, str) else None)
    if not isinstance(base, str) or not base or base == "HEAD" or base.startswith("refs/"):
        raise common.ConfigError("scope has no valid GitHub base branch")
    attention = False
    for pull in list_pulls(api, repo, base):
        merged = pull["merged_at"] is not None
        if not merged and (pull["state"] != "open" or pull["draft"]):
            continue
        number = pull["number"]
        accepted = acceptance(db, scope_id, pull)
        if merged and accepted:
            out(f"PR #{number} merged with exact-head acceptance.")
        elif not accepted:
            attention = True
            state = "merged" if merged else "is open"
            out(f"PR #{number} {state} without proven exact-head acceptance.")
            if accepted is None:
                out(f"PR #{number} lacks a unique work item in the selected scope.")
            if restore_hold and not merged and accepted is False and not held(pull, label):
                restore(db, api, scope_id, config, repo, pull, out)
    return int(attention)


def run(argv, *, gh_run=subprocess.run, env=None, stdout=None, stderr=None):
    env = os.environ if env is None else env
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    redact = common.Redactor(env.get("GH_TOKEN"), env.get("GITHUB_TOKEN"), env.get(common.OPERATOR_ENV))

    def out(line, stream=stdout):
        print(common.printable(redact(line)), file=stream)

    def api(path, projection, **options):
        return request(path, projection, gh_run=gh_run, env={
            **env, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_DEBUG": "",
        }, **options)

    parser = Parser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="SQLite evidence file")
    parser.add_argument("--scope-id", required=True, type=int, help="GitHub scope id in that file")
    parser.add_argument("--restore-hold", action="store_true", help="add the configured hold to eligible pulls")
    try:
        args = parser.parse_args(argv)
        if not 0 < args.scope_id < 2**63:
            raise common.ConfigError("scope id is outside the supported range")
        uri = args.db.resolve(strict=True).as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=common.TIMEOUT_SECONDS)) as db:
            return audit(db, api, args.scope_id, args.restore_hold, out)
    except (common.ConfigError, common.StepError) as error:
        out(f"error: {error}", stderr)
    except (OSError, RuntimeError, sqlite3.Error, ValueError):
        out("error: SQLite evidence could not be read", stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))

#!/usr/bin/env python3
"""Split an issue into parts with the gh CLI. The default is a dry run: it runs only read commands.

With --execute it files each part that is not yet filed, removes the ready label from the original and comments the
list of parts. It never closes the original and never ends its work item: it prints the end-stopped command to run
once a part's pull request has merged. The script holds no token. Exit code: 0 ok, 1 attention, 2 error.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

READY_LABEL = "synth-city-v44-ready"
PAGE_SIZE = 100
MAX_PAGES = 30
MAX_PLAN = 65536
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LANE = re.compile(r"^[A-Za-z0-9:_.-]{1,50}$")


class PlanError(Exception):
    """The plan file or a gh answer is unusable. The text holds no token."""


def is_int(value, low=0, high=10**9):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def is_text(value, high):
    return isinstance(value, str) and 1 <= len(value) <= high


def need(ok, what):
    if not ok:
        raise PlanError(f"no valid {what}")


def load_plan(path):
    try:
        with open(path, "rb") as stream:
            raw = stream.read(MAX_PLAN + 1)
        plan = json.loads(raw) if len(raw) <= MAX_PLAN else None
    except (OSError, ValueError):
        raise PlanError("the plan file cannot be read as JSON") from None
    need(isinstance(plan, dict), "plan object (a file over 65536 bytes is refused)")
    need(is_text(plan.get("repo"), 100) and REPO.match(plan["repo"]), "plan repo (owner/name)")
    need(is_int(plan.get("original"), 1), "plan original issue number")
    need(plan.get("milestone") is None or is_text(plan["milestone"], 120), "plan milestone")
    parts = plan.get("parts")
    need(isinstance(parts, list) and 1 <= len(parts) <= 10, "plan parts (1 to 10)")
    for part in parts:
        need(isinstance(part, dict) and is_text(part.get("title"), 256) and isinstance(part.get("body"), str)
             and len(part["body"]) <= 20000 and isinstance(part.get("lane"), str) and LANE.match(part["lane"])
             and is_int(part.get("priority"), 1, 999)
             and not any(m in part[f] for f in ("title", "body") for m in ("split-from:#", "split-comment:#")),
             "part (title, body, lane, priority; marker text is not allowed)")
    stop = plan.get("end_stopped") or {}
    # A present id must be a real number: null is refused. Only the generation may be null (a known null generation).
    need(isinstance(stop, dict) and all(k not in stop or is_int(stop[k], 1) for k in ("preset", "item"))
         and ("revision" not in stop or is_int(stop["revision"]))
         and ("generation" not in stop or stop["generation"] is None or (isinstance(stop["generation"], str) and re.fullmatch(r"[0-9a-f]{64}", stop["generation"]))),
         "end_stopped ids")
    return plan


def texts(value):
    """Every decoded string of a JSON value, keys included: the text that would reach GitHub or the terminal."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from texts(key)
            yield from texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from texts(item)


def gh(args):
    try:
        done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60, env=dict(os.environ))
    except (OSError, subprocess.SubprocessError):
        return 127, "gh did not run"
    return done.returncode, (done.stdout if done.returncode == 0 else done.stderr)[:1_000_000]


def ask(runner, args, redact):
    code, text = runner(args)
    if code != 0:
        first = common.clean(redact((text or "").splitlines()[0] if text else ""), 160)
        raise PlanError(f"gh {args[0]} {args[1]} failed (exit {code}): {first}")
    return text


def ask_json(runner, args, redact):
    try:
        return json.loads(ask(runner, args, redact))
    except ValueError:
        raise PlanError("the gh answer is not JSON") from None


def scan(runner, redact, repo, since):
    """Every issue and pull request updated since the original was created, page by page. Complete is False after MAX_PAGES."""
    rows = []
    for page in range(1, MAX_PAGES + 1):
        chunk = ask_json(runner, ["api", f"repos/{repo}/issues?state=all&since={since}&per_page={PAGE_SIZE}&page={page}"], redact)
        need(isinstance(chunk, list) and all(isinstance(x, dict) and is_int(x.get("number"), 1) and "body" in x and (x["body"] is None or isinstance(x["body"], str))
                                             for x in chunk), "answer for the issue list")
        rows += chunk
        if len(chunk) < PAGE_SIZE:
            return rows, True
    return rows, False


def parser():
    ap = argparse.ArgumentParser(description="Split an issue into parts. Dry run by default.")
    ap.add_argument("--plan", required=True, help="JSON plan file: repo, original, milestone, parts, end_stopped")
    ap.add_argument("--execute", action="store_true", help="write to GitHub; without it nothing changes")
    ap.add_argument("--ready-label", default=READY_LABEL, help="label to remove from the original")
    return ap


def run(argv, env=None, stdout=None, stderr=None, runner=gh):
    env, stdout, stderr = os.environ if env is None else env, stdout or sys.stdout, stderr or sys.stderr
    try:
        args = parser().parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    redact = common.Redactor(env.get("GH_TOKEN"), env.get("GITHUB_TOKEN"), env.get(common.OPERATOR_ENV))
    out = lambda text, to=stdout: print(common.printable(redact(text)), file=to)  # noqa: E731
    try:
        plan = load_plan(args.plan)
        if any(redact(text) != text for text in texts(plan)):
            raise PlanError("the plan contains a known token value; it is refused before any command and not shown")
        return split(plan, args, runner, redact, out)
    except PlanError as error:
        out(f"error: {error}", stderr)
        return 2


def split(plan, args, runner, redact, out):
    repo, n, parts, execute = plan["repo"], plan["original"], plan["parts"], args.execute
    view = ask_json(runner, ["issue", "view", str(n), "-R", repo, "--json", "number,state,labels,milestone,comments,createdAt"], redact)
    need(isinstance(view, dict) and view.get("number") == n and isinstance(view.get("state"), str)
         and isinstance(view.get("labels"), list) and all(isinstance(x, dict) and isinstance(x.get("name"), str) for x in view["labels"])
         and isinstance(view.get("comments"), list) and all(isinstance(x, dict) and isinstance(x.get("body"), str) for x in view["comments"])
         and (view.get("milestone") is None or (isinstance(view["milestone"], dict) and isinstance(view["milestone"].get("title"), str)))
         and "milestone" in view and isinstance(view.get("createdAt"), str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", view["createdAt"]),
         "answer for the original issue")
    need(view["state"] == "OPEN", "original issue: it is not open")
    milestone = plan.get("milestone") or (view["milestone"] or {}).get("title")
    need(milestone, "milestone: set one in the plan or on the original")
    if redact(milestone) != milestone:
        raise PlanError("the milestone contains a known token value; it is refused before any write and not shown")
    rows, complete = scan(runner, redact, repo, view["createdAt"])
    if not complete and execute:
        raise PlanError(f"cannot prove that no part is filed twice: the scan stopped after {MAX_PAGES} pages")
    marker = lambda k: f"<!-- split-from:#{n} part:{k} -->"  # noqa: E731
    numbers = {}
    for k in range(1, len(parts) + 1):  # a pull request or the original issue never counts as a filed part
        hits = sorted({r["number"] for r in rows if "pull_request" not in r and r["number"] != n and marker(k) in (r["body"] or "")})
        if len(hits) > 1:
            raise PlanError(f"conflict: part {k} is matched by more than one issue ({', '.join(f'#{h}' for h in hits)})")
        numbers.update({k: hits[0]} if hits else {})
    out(f"split #{n} into {len(parts)} parts in {repo}" + ("" if execute else " (dry run: nothing changes; use --execute to write)"))
    if not complete:
        out(f"warning: the scan stopped after {MAX_PAGES} pages, so an earlier part may be missing from this check")
    for k, part in enumerate(parts, 1):
        title = common.clean(redact(part["title"]), 80)
        if k in numbers:
            out(f"part {k}: already filed as #{numbers[k]}")
        elif not execute:
            out(f"part {k}: would create \"{title}\" ({part['lane']}, priority:{part['priority']}, milestone {milestone})")
        else:
            body = f"{part['body']}\n\nSplit from #{n}.\n{marker(k)}"
            code, text = runner(["issue", "create", "-R", repo, "--title", part["title"], "--body", body, "--label", part["lane"],
                                 "--label", f"priority:{part['priority']}", "--milestone", milestone])
            if code != 0:
                raise PlanError(f"gh issue create failed for part {k} (exit {code}): "
                                + common.clean(redact((text or "").splitlines()[0] if text else ""), 160))
            found = re.fullmatch(rf"https://github\.com/{re.escape(repo)}/issues/(\d+)\s*", text or "")
            need(found, "issue address from gh issue create")
            numbers[k] = int(found.group(1))
            out(f"part {k}: created #{numbers[k]}")
    if any(x["name"] == args.ready_label for x in view["labels"]):
        if execute:
            ask(runner, ["issue", "edit", str(n), "-R", repo, "--remove-label", args.ready_label], redact)
        out(f"{'removed' if execute else 'would remove'} label {args.ready_label} from #{n}")
    else:
        out(f"label {args.ready_label} is not on #{n}")
    note = f"<!-- split-comment:#{n} -->"
    if any(note in c["body"] for c in view["comments"]):
        out(f"the list comment is already on #{n}")
    else:
        lines = "\n".join(f"- #{numbers.get(k, '?')} {part['title']}" for k, part in enumerate(parts, 1))
        if execute:
            ask(runner, ["issue", "comment", str(n), "-R", repo, "--body", f"Split into:\n{lines}\n{note}"], redact)
        out(f"{'commented' if execute else 'would comment'} the list of parts on #{n}")
    stop = plan.get("end_stopped") or {}
    data = {"operation_id": f"split-{n}", "expected_dispatch_generation": stop["generation"] if "generation" in stop else "<EXPECTED_DISPATCH_GENERATION>",
            "expected_scope_revision": stop.get("revision", "<EXPECTED_SCOPE_REVISION>"), "outcome": "superseded",
            "superseding_pr_number": "<SUPERSEDING_PR_NUMBER>", "reason": f"Superseded by the split of #{n}"}
    out("The helper does not end the original item. After a part's pull request has merged, run this yourself.")
    out("Replace each <PLACEHOLDER>, and remove its quotes where it stands for a number. The operator token stays in the environment.")
    out(f"curl -sS -X POST \"$SYNTH_CITY_URL/api/v1/agent-teams/presets/{stop.get('preset', '<PRESET_ID>')}/work-items/"
        f"{stop.get('item', '<ITEM_ID>')}/end-stopped\" -H \"x-synth-city-operator-token: ${common.OPERATOR_ENV}\" "
        f"-H \"content-type: application/json\" -d '{json.dumps(data, separators=(',', ':'))}'")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

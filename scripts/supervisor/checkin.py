#!/usr/bin/env python3
"""One Supervisor check-in on existing product routes.

The script sends GET requests only and writes no file. The product's activity and summary reads can record native
observations and notices on the server, as they do for the web page.

Each step prints `ok`, `attention` or `error` and one line of findings. One failed step does not stop the others.
An answer without the fields a step reports on is an error, never a success.
Exit code: 0 all ok, 1 attention, 2 a step or the setup failed. A token is never printed or written.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

INBOX_LIMIT = 50
FINISHED_ACTIONS = ("historical", "resolved")


def parse_time(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def need(ok, what):
    if not ok:
        raise common.StepError(f"the answer has no valid {what}")


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def health(data, args):
    need(isinstance(data, dict) and isinstance(data.get("status"), str) and isinstance(data.get("health"), str)
         and "open_findings" in data and (data["open_findings"] is None or is_count(data["open_findings"])),
         "status, health and findings count")
    parts = [f"status {data.get('status')}", f"health {data.get('health')}",
             "unknown open findings" if data["open_findings"] is None else f"{data['open_findings']} open findings"]
    if data.get("reasons"):
        parts.append("reasons " + ", ".join(common.clean(r, 40) for r in data["reasons"]))
    bad = data["status"] != "ok" or data["health"] != "ok" or data["open_findings"] is None
    return [("attention" if bad else "ok", ", ".join(parts))]


def summary(data, args):
    need(isinstance(data, dict) and "hold_code" in data and isinstance(data["hold_code"], (str, type(None)))
         and isinstance(data.get("coverage_complete"), bool)
         and isinstance(data.get("attention"), list) and isinstance(data.get("human_actions"), list),
         "hold, coverage, attention and human actions")
    found = []
    if data.get("hold_code"):
        found.append(f"hold {common.clean(data['hold_code'], 40)}")
    for item in data.get("attention") or []:
        found.append(f"attention {common.clean(item.get('code'), 40)}" + (f" #{item['issue_number']}" if item.get("issue_number") else ""))
    ready, blocked = [], []
    for item in data["human_actions"]:
        state = item.get("state")
        if state in FINISHED_ACTIONS:
            continue
        label = f"{common.clean(item.get('kind'), 40)} #{common.clean(item.get('issue_number'), 20)}"
        # Only a request the operator can act on now is "act now". Every other state waits on something else.
        (ready if state == "requested" else blocked).append(label if state == "requested" else f"{label} {common.clean(state, 40)}")
    if ready:
        found.append("human action, act now: " + ", ".join(ready))
    if blocked:
        found.append("human action, blocked: " + ", ".join(blocked))
    for item in data.get("required_ci_unreadable") or []:
        found.append(f"required CI unreadable #{item.get('issue_number')}")
    if data.get("coverage_complete") is False:
        found.append("coverage incomplete")
    status = (data.get("supervisor_observation") or {}).get("status")
    if status in ("late", "absent"):
        found.append(f"supervisor check-in {status}")
    return [("attention", "; ".join(found))] if found else [("ok", "no hold, no attention, no human action, coverage complete")]


def activity(data, args):
    need(isinstance(data, dict) and isinstance(data.get("slots"), list), "member list")
    reference = parse_time(data.get("checked_at"))
    need(reference is not None, "check time")
    found = []
    for slot in data.get("slots") or []:
        name = f"slot {slot.get('slot_id')}"
        state = slot.get("state")
        if state not in ("working", "idle"):
            found.append(f"{name} {common.clean(state, 40)}")
        elif state == "idle":
            since = parse_time(slot.get("observed_at"))
            minutes = int((reference - since).total_seconds() // 60) if since else None
            if minutes is None:
                found.append(f"{name} idle, time unknown")
            elif minutes > args.idle_minutes:
                found.append(f"{name} idle {minutes} min")
        wait = slot.get("waiting_input")
        if wait:
            found.append(f"{name} waiting for input ({common.clean(wait.get('reason'), 40)})")
    return [("attention", "; ".join(found))] if found else [("ok", f"{len(data.get('slots') or [])} members, none stopped, waiting or idle over {args.idle_minutes} min")]


def overview(data, args):
    counts = data.get("counts") if isinstance(data, dict) else None
    need(isinstance(counts, dict) and is_count(counts.get("total")) and is_count(counts.get("attention")), "counts")
    needing = counts["attention"]
    text = f"{needing} of {counts['total']} work items need attention"
    return [("attention" if needing else "ok", text)]


def inbox(messages, args):
    # A row without a valid id cannot be counted, so the page has no confirmed cursor: refuse it whole.
    need(isinstance(messages, list) and all(isinstance(m, dict) and is_count(m.get("id")) for m in messages), "message ids")
    seen = messages
    fresh = [m for m in seen if m["id"] > args.since_id]
    highest = max([args.since_id, *(m["id"] for m in fresh)])
    # The route gives the newest INBOX_LIMIT messages. A full page that stays above the saved id may hide older ones.
    limited = len(messages) >= INBOX_LIMIT and bool(seen) and min(m["id"] for m in seen) > args.since_id
    text = f"{len(fresh)} new messages since id {args.since_id}"
    lines = [("attention" if fresh else "ok", text + (", the newest page only; earlier messages may be missing" if limited else ""))]
    lines += [("detail", f"  {m['id']} from {common.clean(m.get('sender_name'), 60)}: {common.clean(m.get('subject'))}")
              for m in sorted(fresh, key=lambda m: m["id"])]
    cursor = f"highest inbox id seen: {highest} (not a confirmed cursor)" if limited else f"highest inbox id: {highest}"
    return lines + [("detail", cursor)]


def parser():
    ap = argparse.ArgumentParser(description="One Supervisor check-in on existing product routes. Reads only.")
    ap.add_argument("--base-url", default="http://127.0.0.1:8000", help="product address; plain http only for loopback")
    ap.add_argument("--preset", type=int, required=True, help="team preset id")
    ap.add_argument("--supervisor-token-file", help="file (mode 600) with the external actor token; without it the inbox step is skipped")
    ap.add_argument("--prompt-operator-token", action="store_true", help=f"ask for the operator token; else read {common.OPERATOR_ENV}")
    ap.add_argument("--since-id", type=int, default=0, help="show inbox messages above this id")
    ap.add_argument("--idle-minutes", type=int, default=20, help="report a member idle longer than this")
    return ap


def run(argv, env=None, stdout=None, stderr=None, prompt=common.hidden_prompt):
    env, stdout, stderr = os.environ if env is None else env, stdout or sys.stdout, stderr or sys.stderr
    try:
        args = parser().parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)
    redact = common.Redactor()
    try:
        base = common.check_base_url(args.base_url)
        operator = common.operator_token(env, args.prompt_operator_token, prompt)
        supervisor = common.read_token_file(args.supervisor_token_file) if args.supervisor_token_file else None
    except common.ConfigError as error:
        print(f"error: {error}", file=stderr)
        return 2
    redact = common.Redactor(operator, supervisor)
    client = common.Client(base, redact)
    product = {common.OPERATOR_HEADER: operator} if operator else {}
    prefix = f"/api/v1/agent-teams/presets/{args.preset}"
    steps = [("health", "/api/v1/health", product, None, health),
             ("summary", f"{prefix}/summary", product, None, summary),
             ("activity", f"{prefix}/activity", product, None, activity),
             ("overview", "/api/v1/factory/overview", product, {"team_id": args.preset}, overview)]
    if supervisor:
        steps.append(("inbox", "/api/v1/external/agent-mail/supervisor-inbox", {"Authorization": f"Bearer {supervisor}"},
                      {"limit": INBOX_LIMIT}, inbox))
    worst = 0
    for name, path, headers, params, read in steps:
        try:
            lines = read(client.get(path, headers, params), args)
        except (common.StepError, AttributeError, TypeError, ValueError, KeyError) as error:
            lines = [("error", str(error) if isinstance(error, common.StepError) else "the answer has an unexpected shape")]
        for level, text in lines:
            print(common.printable(redact(text if level == "detail" else f"{level} {name}: {text}")), file=stdout)
            worst = max(worst, {"attention": 1, "error": 2}.get(level, 0))
    if not supervisor:
        print("skip inbox: skipped, give --supervisor-token-file to read the supervisor inbox", file=stdout)
    return worst


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

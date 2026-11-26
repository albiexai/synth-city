"""#1116 part 1: the Supervisor check-in script reads product routes only and never shows a token."""
from __future__ import annotations

import getpass
import io
import json
import os
import signal
import stat
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "supervisor"))
import _common  # noqa: E402
import checkin  # noqa: E402

OPERATOR = "op-secret-4f9a1c"
SUPERVISOR = "sup-secret-77b2de"
NOW = "2026-11-26T02:00:00Z"
PRESET = 7
ROUTES = {
    "health": "/api/v1/health",
    "summary": f"/api/v1/agent-teams/presets/{PRESET}/summary",
    "activity": f"/api/v1/agent-teams/presets/{PRESET}/activity",
    "overview": "/api/v1/factory/overview",
    "inbox": "/api/v1/external/agent-mail/supervisor-inbox",
}


def healthy():
    return {
        ROUTES["health"]: {"status": "ok", "health": "ok", "reasons": [], "open_findings": 0},
        ROUTES["summary"]: {"hold_code": None, "attention": [], "human_actions": [], "required_ci_unreadable": [],
                            "coverage_complete": True, "supervisor_observation": {"status": "healthy"}},
        ROUTES["activity"]: {"checked_at": NOW, "slots": [
            {"slot_id": 1, "state": "working", "reason": "native_event", "observed_at": NOW}]},
        ROUTES["overview"]: {"counts": {"total": 4, "attention": 0}},
        ROUTES["inbox"]: [],
    }


class Fake:
    def __init__(self, answers):
        self.answers, self.seen, self.queries = answers, [], {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path, _, query = self.path.partition("?")
                fake.queries[path] = query
                fake.seen.append((path, {k.lower(): v for k, v in self.headers.items()}))
                status, body = fake.answers.get(path, (404, {"detail": "missing"}))
                if callable(body):
                    body(self)
                    return
                if status == 302:
                    self.send_response(302)
                    self.send_header("Location", body)
                    self.end_headers()
                    return
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serve():
    started = []

    def start(answers=None, **changes):
        merged = {path: (200, body) for path, body in healthy().items()}
        merged.update({ROUTES[name]: value if isinstance(value, tuple) else (200, value)
                       for name, value in changes.items()})
        merged.update(answers or {})
        fake = Fake(merged)
        started.append(fake)
        return fake

    yield start
    for fake in started:
        fake.close()


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "supervisor.token"
    path.write_text(SUPERVISOR + "\n")
    path.chmod(0o600)
    return path


def run(fake, *extra, env=None, prompt=None):
    out, err = io.StringIO(), io.StringIO()
    argv = ["--base-url", fake.url, "--preset", str(PRESET), *extra]
    code = checkin.run(argv, env=env or {}, stdout=out, stderr=err, prompt=prompt or (lambda _: ""))
    return code, out.getvalue(), err.getvalue()


def test_a_healthy_team_gives_ok_lines_and_exit_0(serve):
    code, out, _ = run(serve())
    assert code == 0
    assert [line.split()[0] for line in out.splitlines() if line.startswith(("ok", "attention", "error"))] == ["ok"] * 4
    assert "inbox: skipped" in out


@pytest.mark.parametrize("change,expected", [
    ({"health": {"status": "ok", "health": "degraded", "reasons": ["findings_unreadable"], "open_findings": 2}},
     "attention health"),
    ({"summary": {"hold_code": "hold", "attention": [{"code": "owner_stalled", "issue_number": 5}],
                  "human_actions": [{"kind": "review_pr", "issue_number": 5, "state": "requested"}],
                  "required_ci_unreadable": [{"issue_number": 5}], "coverage_complete": False,
                  "supervisor_observation": None}},
     "attention summary"),
    ({"overview": {"counts": {"total": 4, "attention": 2}}}, "attention overview"),
    ({"summary": {"hold_code": None, "attention": [], "human_actions": [], "required_ci_unreadable": [],
                  "coverage_complete": True, "supervisor_observation": {"status": "late"}}}, "check-in late"),
])
def test_a_finding_is_reported_with_exit_1(serve, change, expected):
    code, out, _ = run(serve(**change))
    assert code == 1
    assert expected in out


def test_the_summary_line_names_the_hold_the_attention_and_the_gaps(serve):
    summary = {"hold_code": "hold", "attention": [{"code": "owner_stalled", "issue_number": 5}],
               "human_actions": [{"kind": "review_pr", "issue_number": 5, "state": "requested"}],
               "required_ci_unreadable": [{"issue_number": 5}], "coverage_complete": False,
                  "supervisor_observation": None}
    _, out, _ = run(serve(summary=summary))
    for part in ("hold", "owner_stalled", "review_pr", "unreadable", "coverage"):
        assert part in out


def test_members_that_are_stopped_unknown_waiting_or_idle_too_long_are_reported(serve):
    slots = [
        {"slot_id": 1, "state": "idle", "reason": "r", "observed_at": "2026-11-26T01:30:00Z"},
        {"slot_id": 2, "state": "idle", "reason": "r", "observed_at": "2026-11-26T01:50:00Z"},
        {"slot_id": 3, "state": "stopped", "reason": "r", "observed_at": NOW},
        {"slot_id": 4, "state": "unknown", "reason": "r", "observed_at": None},
        {"slot_id": 5, "state": "working", "reason": "r", "observed_at": NOW,
         "waiting_input": {"reason": "trust", "observed_at": NOW}},
    ]
    code, out, _ = run(serve(activity={"checked_at": NOW, "slots": slots}))
    assert code == 1
    assert "slot 1" in out and "idle 30" in out
    assert "slot 2" not in out
    assert "slot 3" in out and "slot 4" in out
    assert "slot 5" in out and "trust" in out


def test_the_idle_limit_is_an_argument(serve):
    slots = [{"slot_id": 2, "state": "idle", "reason": "r", "observed_at": "2026-11-26T01:50:00Z"}]
    fake = serve(activity={"checked_at": NOW, "slots": slots})
    assert run(fake)[0] == 0
    assert "slot 2" in run(fake, "--idle-minutes", "5")[1]


def test_the_inbox_lists_newer_messages_without_their_text(serve, token_file):
    messages = [
        {"id": 10, "sender_name": "Old", "subject": "Old one", "body_markdown": "OLD-BODY"},
        {"id": 12, "sender_name": "Leader", "subject": "Decision needed", "body_markdown": "PRIVATE-BODY"},
        {"id": 11, "sender_name": "Member", "subject": "Question", "body_markdown": "OTHER-BODY"},
    ]
    code, out, _ = run(serve(inbox=messages), "--supervisor-token-file", str(token_file), "--since-id", "10")
    assert code == 1
    assert "12" in out and "Decision needed" in out and "11" in out and "Question" in out
    assert "Old one" not in out
    assert "BODY" not in out
    assert "highest inbox id: 12" in out


def test_a_failed_step_does_not_stop_the_others_and_gives_exit_2(serve):
    code, out, _ = run(serve(summary=(500, {"detail": "boom"})))
    assert code == 2
    assert "error summary" in out
    for name in ("health", "activity", "overview"):
        assert f"ok {name}" in out


def test_the_headers_go_only_where_they_belong(serve, token_file):
    fake = serve()
    run(fake, "--supervisor-token-file", str(token_file), env={"SYNTH_CITY_OPERATOR_TOKEN": OPERATOR})
    seen = dict(fake.seen)
    assert set(seen) == set(ROUTES.values())
    for name, path in ROUTES.items():
        headers = seen[path]
        assert headers.get("authorization") == (f"Bearer {SUPERVISOR}" if name == "inbox" else None)
        assert headers.get("x-synth-city-operator-token") == (None if name == "inbox" else OPERATOR)


def test_the_factory_overview_is_read_for_this_team_only(serve):
    fake = serve()
    run(fake)
    assert fake.queries[ROUTES["overview"]] == f"team_id={PRESET}"


def test_no_token_value_reaches_the_output_even_when_the_server_echoes_it(serve, token_file, tmp_path):
    echo = (401, {"detail": f"bad token {OPERATOR} and {SUPERVISOR}"})
    fake = serve({path: echo for path in ROUTES.values()})
    before = sorted(os.listdir(tmp_path))
    code, out, err = run(fake, "--supervisor-token-file", str(token_file), env={"SYNTH_CITY_OPERATOR_TOKEN": OPERATOR})
    assert code == 2
    for text in (out, err):
        assert OPERATOR not in text and SUPERVISOR not in text
    assert sorted(os.listdir(tmp_path)) == before


def test_a_token_inside_a_message_subject_is_removed_from_the_output(serve, token_file):
    leaked = [{"id": 3, "sender_name": f"x {OPERATOR}", "subject": f"key {SUPERVISOR} and {OPERATOR}"}]
    _, out, err = run(serve(inbox=leaked), "--supervisor-token-file", str(token_file),
                      env={"SYNTH_CITY_OPERATOR_TOKEN": OPERATOR})
    assert "key [token removed] and [token removed]" in out
    assert OPERATOR not in out + err and SUPERVISOR not in out + err


def test_a_token_cut_by_the_length_limit_is_not_printed_in_part(serve, token_file):
    # V2: the text was cut to 120 (subject) and 60 (sender) characters before the redaction, so a part of the token stayed.
    messages = [{"id": 3, "sender_name": "y" * 55 + OPERATOR, "subject": "x" * 110 + SUPERVISOR}]
    health = {"status": "ok", "health": "ok", "reasons": ["z" * 35 + OPERATOR], "open_findings": 0}
    _, out, err = run(serve(inbox=messages, health=health), "--supervisor-token-file", str(token_file),
                      env={"SYNTH_CITY_OPERATOR_TOKEN": OPERATOR})
    for token in (OPERATOR, SUPERVISOR):
        for size in range(4, len(token) + 1):
            assert token[:size] not in out + err and token[-size:] not in out + err


def test_control_characters_from_another_party_never_reach_the_terminal(serve, token_file):
    messages = [{"id": 3, "sender_name": "n\x1b]0;title\x07ame", "subject": "a\x1b[31mred\x07\r\nb\x00c"}]
    _, out, _ = run(serve(inbox=messages), "--supervisor-token-file", str(token_file))
    assert all(char in "\n" or char.isprintable() for char in out)
    assert "red" in out and "ame" in out


def test_proxy_settings_are_ignored_so_a_token_never_goes_to_a_proxy(serve, monkeypatch):
    proxy, fake = serve(), serve()
    for name in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", proxy.url)
    run(fake, env={"SYNTH_CITY_OPERATOR_TOKEN": OPERATOR})
    assert proxy.seen == [] and len(fake.seen) == 4


def test_the_redaction_removes_both_tokens_and_ignores_empty_ones():
    redact = _common.Redactor(OPERATOR, SUPERVISOR, "", None)
    assert redact(f"a {OPERATOR} b {SUPERVISOR} c") == "a [token removed] b [token removed] c"
    assert redact("nothing here") == "nothing here"


def test_a_token_file_that_others_can_read_is_refused_before_any_request(serve, token_file):
    token_file.chmod(0o644)
    fake = serve()
    code, out, err = run(fake, "--supervisor-token-file", str(token_file))
    assert code == 2 and fake.seen == []
    assert SUPERVISOR not in out + err
    assert "mode" in err


def test_a_missing_empty_or_linked_token_file_is_refused(serve, tmp_path, token_file):
    empty = tmp_path / "empty.token"
    empty.write_text("\n")
    empty.chmod(0o600)
    link = tmp_path / "link.token"
    link.symlink_to(token_file)
    fake = serve()
    for path in (tmp_path / "absent.token", empty, link):
        assert run(fake, "--supervisor-token-file", str(path))[0] == 2
    assert fake.seen == []


def test_the_token_file_mode_check_accepts_owner_only_files(token_file):
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert _common.read_token_file(token_file) == SUPERVISOR


@pytest.mark.parametrize("url", [
    "http://example.test:8000", "http://10.0.0.5", "ftp://127.0.0.1", "http://user:pw@127.0.0.1:8000", "127.0.0.1:8000",
])
def test_a_base_url_that_could_expose_a_token_is_refused(url):
    with pytest.raises(_common.ConfigError):
        _common.check_base_url(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000",
                                 "https://synth.example.test"])
def test_loopback_http_and_any_https_are_allowed(url):
    assert _common.check_base_url(url) == url.rstrip("/")


def test_the_command_refuses_a_remote_http_address_without_a_request(capsys):
    out, err = io.StringIO(), io.StringIO()
    code = checkin.run(["--base-url", "http://example.test:8000", "--preset", "7"],
                       env={"SYNTH_CITY_OPERATOR_TOKEN": OPERATOR}, stdout=out, stderr=err, prompt=lambda _: "")
    assert code == 2 and OPERATOR not in out.getvalue() + err.getvalue()


def test_a_redirect_is_not_followed_so_a_token_never_goes_to_another_address(serve):
    target = serve()
    fake = serve({ROUTES["health"]: (302, target.url + ROUTES["health"])})
    code, out, _ = run(fake, env={"SYNTH_CITY_OPERATOR_TOKEN": OPERATOR})
    assert code == 2 and "error health" in out
    assert target.seen == []


def test_a_very_large_answer_is_refused(serve):
    # A valid answer of the right shape, so only the size check can refuse it.
    code, out, _ = run(serve(health=(200, b'{"status": "ok", "health": "ok", "pad": "' + b"a" * 1_100_000 + b'"}')))
    assert code == 2 and "error health: the answer is larger than" in out


def test_the_operator_token_is_prompted_only_when_asked(serve):
    asked = []
    fake = serve()
    run(fake, prompt=lambda text: asked.append(text) or OPERATOR)
    assert asked == [] and all("x-synth-city-operator-token" not in h for _, h in fake.seen)
    fake.seen.clear()
    code, out, err = run(fake, "--prompt-operator-token", prompt=lambda text: asked.append(text) or OPERATOR)
    assert len(asked) == 1 and OPERATOR not in asked[0]
    assert {h.get("x-synth-city-operator-token") for p, h in fake.seen if p != ROUTES["inbox"]} == {OPERATOR}
    assert OPERATOR not in out + err


# --- V1's second security review (#1116 part 1) ---

def test_a_hidden_prompt_that_cannot_hide_the_input_is_a_setup_error_before_any_read(monkeypatch):
    read = []

    def echoing(prompt, stream=None):
        warnings.warn("Can not control echo on the terminal.", getpass.GetPassWarning)
        read.append(1)  # the real fallback reads the visible input after the warning
        return OPERATOR

    monkeypatch.setattr(getpass, "getpass", echoing)
    with pytest.raises(_common.ConfigError):
        _common.hidden_prompt("Operator token: ")
    assert read == []


def test_end_of_input_at_the_prompt_is_a_setup_error(monkeypatch):
    def closed(prompt, stream=None):
        raise EOFError

    monkeypatch.setattr(getpass, "getpass", closed)
    with pytest.raises(_common.ConfigError):
        _common.hidden_prompt("Operator token: ")


def test_a_failed_prompt_ends_the_command_with_exit_2_and_no_request(serve):
    def failing(_):
        raise _common.ConfigError("the terminal cannot hide the input; use the environment variable")

    fake = serve()
    code, out, err = run(fake, "--prompt-operator-token", prompt=failing)
    assert code == 2 and fake.seen == [] and "environment variable" in err


def test_every_emitted_line_is_printable_whatever_the_server_sends(serve, token_file):
    dirty = "\x1b[31m\x07a\r\nb\x00"
    answers = dict(
        health={"status": "ok" + dirty, "health": "degraded" + dirty, "reasons": [dirty], "open_findings": 0},
        summary={"hold_code": dirty, "attention": [{"code": dirty, "issue_number": 5}],
                 "human_actions": [{"kind": dirty, "issue_number": 5, "state": "requested"}],
                 "required_ci_unreadable": [], "coverage_complete": True, "supervisor_observation": {"status": "late"}},
        activity={"checked_at": NOW, "slots": [
            {"slot_id": dirty, "state": "stopped"},
            {"slot_id": 2, "state": "working", "observed_at": NOW, "waiting_input": {"reason": dirty, "observed_at": NOW}}]},
        overview={"counts": {"total": 4, "attention": 1}},
        inbox=[{"id": 3, "sender_name": dirty, "subject": dirty}])
    _, out, _ = run(serve(**answers), "--supervisor-token-file", str(token_file))
    assert all(char == "\n" or char.isprintable() for char in out)
    assert all(line.startswith(("ok ", "attention ", "error ", "skip ", "  ", "highest ")) for line in out.splitlines())


def _one_field_cases():
    """Each case starts from one complete valid answer and changes exactly one field, so the case can only fail on it."""
    complete = {name: healthy()[ROUTES[name]] for name in ("health", "summary", "activity", "overview")}
    cases = []

    def missing(step, key):
        cases.append(pytest.param(step, {k: v for k, v in complete[step].items() if k != key}, id=f"{step}-without-{key}"))

    def wrong(step, key, value, label):
        cases.append(pytest.param(step, {**complete[step], key: value}, id=f"{step}-{key}-{label}"))

    for key in ("status", "health", "reasons", "open_findings"):
        missing("health", key)
    for key, value, label in (("status", 5, "number"), ("health", 5, "number"), ("reasons", "none", "text"),
                              ("open_findings", -1, "negative"), ("open_findings", "3", "text"),
                              ("open_findings", True, "bool")):
        wrong("health", key, value, label)
    for key in ("hold_code", "attention", "human_actions", "required_ci_unreadable", "coverage_complete",
                "supervisor_observation"):
        missing("summary", key)
    for key, value, label in (("hold_code", 5, "number"), ("coverage_complete", "yes", "text"),
                              ("attention", {}, "object"), ("human_actions", {}, "object"),
                              ("required_ci_unreadable", {}, "object"), ("supervisor_observation", 5, "number"),
                              ("attention", ["x"], "entry-not-object"), ("human_actions", ["x"], "entry-not-object"),
                              ("required_ci_unreadable", ["x"], "entry-not-object")):
        wrong("summary", key, value, label)
    for key in ("slots", "checked_at"):
        missing("activity", key)
    wrong("activity", "slots", {}, "object")
    wrong("activity", "checked_at", "not a time", "text")
    missing("overview", "counts")
    for value, label in (({}, "empty"), ({"total": 3}, "no-attention"), ({"attention": 0}, "no-total"),
                         ({"total": "3", "attention": "0"}, "text"), ({"total": True, "attention": False}, "bool"),
                         ({"total": -1, "attention": 0}, "negative"), ([], "list")):
        wrong("overview", "counts", value, label)
    for step in complete:
        cases.append(pytest.param(step, [], id=f"{step}-not-an-object"))
    return cases


def test_the_complete_answers_used_for_the_one_field_cases_are_a_success(serve):
    code, out, _ = run(serve())
    assert code == 0, out


@pytest.mark.parametrize("name,answer", _one_field_cases())
def test_an_incomplete_answer_is_an_error_and_never_a_success(serve, name, answer):
    code, out, _ = run(serve(**{name: answer}))
    assert code == 2 and f"error {name}: the answer has no valid" in out
    for other in {"health", "summary", "activity", "overview"} - {name}:
        assert f"ok {other}" in out


def test_an_inbox_answer_that_is_not_a_list_is_an_error(serve, token_file):
    code, out, _ = run(serve(inbox={"detail": "x"}), "--supervisor-token-file", str(token_file))
    assert code == 2 and "error inbox: the answer has no valid" in out


def test_a_full_inbox_page_above_the_saved_id_is_a_limited_window(serve, token_file):
    page = [{"id": 100 + n, "sender_name": "m", "subject": "s"} for n in range(50)]
    code, out, _ = run(serve(inbox=page), "--supervisor-token-file", str(token_file), "--since-id", "10")
    assert code == 1 and "earlier messages may be missing" in out
    assert "highest inbox id seen: 149 (not a confirmed cursor)" in out and "highest inbox id: " not in out


def test_a_full_inbox_page_that_reaches_the_saved_id_is_complete(serve, token_file):
    page = [{"id": 10 + n, "sender_name": "m", "subject": "s"} for n in range(50)]
    _, out, _ = run(serve(inbox=page), "--supervisor-token-file", str(token_file), "--since-id", "10")
    assert "earlier messages may be missing" not in out and "highest inbox id: 59" in out


def test_a_pipe_as_the_token_file_is_refused_and_never_waited_for(tmp_path):
    pipe = tmp_path / "pipe.token"
    os.mkfifo(pipe)
    pipe.chmod(0o600)

    def blocked(*_):
        raise TimeoutError("opening the token file blocked")

    old = signal.signal(signal.SIGALRM, blocked)
    signal.alarm(5)
    try:
        with pytest.raises(_common.ConfigError, match="regular file"):
            _common.read_token_file(pipe)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def test_a_slow_trickle_is_stopped_by_the_total_deadline(serve, monkeypatch):
    monkeypatch.setattr(_common, "TIMEOUT_SECONDS", 0.4)

    def trickle(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        try:
            for _ in range(60):  # one byte every 0.1 s: no single wait reaches the limit
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass

    started = time.monotonic()
    code, out, _ = run(serve(health=trickle))
    assert code == 2 and "error health: the answer took longer than" in out
    assert time.monotonic() - started < 4


def test_a_member_state_the_script_does_not_know_is_reported(serve):
    slots = [{"slot_id": 7, "state": "paused", "reason": "r", "observed_at": NOW}]
    code, out, _ = run(serve(activity={"checked_at": NOW, "slots": slots}))
    assert code == 1 and "slot 7 paused" in out


# --- V1's third review (#1116 part 1) ---

def summary_with(*actions, coverage=True):
    return {"hold_code": None, "attention": [], "human_actions": list(actions), "required_ci_unreadable": [],
            "coverage_complete": coverage, "supervisor_observation": None}


@pytest.mark.parametrize("state", ["waiting_for_prerequisites", "context_pending", "pr_unavailable",
                                   "pr_identity_unavailable", "head_changed", "surprise_state"])
def test_a_blocked_human_action_is_reported_with_its_issue_and_state(serve, state):
    action = {"kind": "merge_pr", "issue_number": 7, "state": state}
    code, out, _ = run(serve(summary=summary_with(action)))
    assert code == 1 and f"blocked: merge_pr #7 {state}" in out and "act now" not in out


def test_a_request_the_operator_can_act_on_is_told_apart_from_a_blocked_one(serve):
    ready = {"kind": "review_pr", "issue_number": 5, "state": "requested", "assessment_current": True}
    blocked = {"kind": "merge_pr", "issue_number": 7, "state": "waiting_for_prerequisites"}
    _, out, _ = run(serve(summary=summary_with(ready, blocked)))
    assert "act now: review_pr #5" in out and "blocked: merge_pr #7 waiting_for_prerequisites" in out


def test_an_incomplete_coverage_still_names_the_outstanding_action(serve):
    action = {"kind": "merge_pr", "issue_number": 7, "state": "pr_unavailable"}
    _, out, _ = run(serve(summary=summary_with(action, coverage=False)))
    assert "coverage incomplete" in out and "merge_pr #7 pr_unavailable" in out


def test_a_finished_human_action_is_not_reported(serve):
    done = [{"kind": "review_pr", "issue_number": 5, "state": s} for s in ("historical", "resolved")]
    code, out, _ = run(serve(summary=summary_with(*done)))
    assert code == 0 and "no human action" in out


def test_an_unreadable_findings_count_is_unknown_and_never_zero(serve):
    health = {"status": "ok", "health": "degraded", "reasons": ["findings_unreadable"], "open_findings": None}
    code, out, _ = run(serve(health=health))
    assert code == 1 and "attention health" in out and "unknown open findings" in out and "None" not in out
    quiet = {"status": "ok", "health": "ok", "reasons": [], "open_findings": None}  # unknown is never the same as zero
    code, out, _ = run(serve(health=quiet))
    assert code == 1 and "attention health" in out and "unknown open findings" in out


@pytest.mark.parametrize("rows", [
    [{"sender_name": "m", "subject": "no id"}],
    [{"id": "5", "sender_name": "m", "subject": "text id"}],
    [{"id": True, "sender_name": "m", "subject": "bool id"}],
    [{"id": 5, "sender_name": "m", "subject": "ok"}, "not an object"],
])
def test_an_inbox_row_that_cannot_be_counted_is_an_error_with_no_cursor(serve, token_file, rows):
    code, out, _ = run(serve(inbox=rows), "--supervisor-token-file", str(token_file))
    assert code == 2 and "error inbox: the answer has no valid message ids" in out and "highest inbox id" not in out


def test_a_directory_as_the_token_file_is_a_setup_error_and_exit_2(serve, tmp_path):
    fake = serve()
    code, out, err = run(fake, "--supervisor-token-file", str(tmp_path))
    assert code == 2 and fake.seen == [] and "regular file" in err and "Traceback" not in out + err
    with pytest.raises(_common.ConfigError, match="regular file"):
        _common.read_token_file(tmp_path)


def test_a_token_file_that_is_not_text_is_a_setup_error_and_exit_2(serve, tmp_path):
    binary = tmp_path / "binary.token"
    binary.write_bytes(b"\xff\xfe\x00\x80")
    binary.chmod(0o600)
    fake = serve()
    code, out, err = run(fake, "--supervisor-token-file", str(binary))
    assert code == 2 and fake.seen == [] and "Traceback" not in out + err


# --- V2's review of 889c5ab0 (#1116 part 1) ---

def test_a_requested_action_is_act_now_only_when_its_assessment_is_current(serve):
    current = {"kind": "review_pr", "issue_number": 5, "state": "requested", "assessment_current": True}
    old = {"kind": "review_pr", "issue_number": 6, "state": "requested", "assessment_current": False}
    unknown = {"kind": "merge_pr", "issue_number": 7, "state": "requested"}
    _, out, _ = run(serve(summary=summary_with(current, old, unknown)))
    assert "act now: review_pr #5" in out and "#6" not in out.split("act now:")[1].split(";")[0]
    assert "stale: review_pr #6, merge_pr #7" in out


def test_a_stale_request_alone_is_never_called_act_now(serve):
    old = {"kind": "review_pr", "issue_number": 6, "state": "requested", "assessment_current": False}
    code, out, _ = run(serve(summary=summary_with(old)))
    assert code == 1 and "stale: review_pr #6" in out and "act now" not in out

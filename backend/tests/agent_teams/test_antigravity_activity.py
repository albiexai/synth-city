"""Antigravity CLI native observation (#737), from what P1 observed on agy 1.5.9 (stub model).

Observed sources, under ``<passwd home>/.gemini/antigravity-cli``:
- ``presence/<uuid>.lock``: held open by the live ``agy`` process for its conversation.
- ``conversation_summaries.db`` (SQLite, WAL): ``status`` RUNNING with ``not_fully_idle`` 1 during
  a turn, IDLE with 0 after it; ``workspace_uris`` names the trusted workspace.
- ``conversations/<uuid>.db`` (SQLite, WAL): ``steps(idx, step_type, status)``. Live values seen:
  2 while ``run_command`` ran (skip-permissions), 3 when done, 9 while ``ask_question`` waited;
  6 only after the process exited.
- ``brain/<uuid>/.system_generated/logs/transcript.jsonl``: one record per step.
Anything else is unknown.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

from app.services import antigravity_activity_service as ag

CID = "e6a5a8d6-8a84-4090-b249-33a98c43f756"
OTHER = "2fb8a97c-fbf2-418a-9658-e7496c072554"
START = datetime(2026, 11, 16, 11, 49, 0, 510000, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 16, 11, 49, 20, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "antigravity_sessions"


def at(seconds):
    return (START.replace(microsecond=0) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def user(i, s=1):
    return {"step_index": i, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": at(s)}


def planner(i, s=1, tools=None):
    record = {"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": at(s)}
    if tools is not None:
        record["tool_calls"] = [{"name": name, "args": {"secret": "never read"}} for name in tools]
    else:
        record["content"] = "reply"
    return record


def system(i, s=1):
    return {"step_index": i, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "status": "DONE", "created_at": at(s)}


RUNNING = {"status": "CASCADE_RUN_STATUS_RUNNING", "not_fully_idle": 1}
IDLE = {"status": "CASCADE_RUN_STATUS_IDLE", "not_fully_idle": 0}


def state(summary, steps, records, *, count=None, now=NOW, started=START, skip=False):
    summary = None if summary is None else {**summary, "step_count": len(steps) if count is None else count}
    return ag.antigravity_state(summary, len(steps), steps, records, started, now, skip_permissions=skip)[:2]


# --- States from observed shapes ------------------------------------------------------------

def test_question_waiting_is_observed_status_nine_after_a_single_ask_question():
    records = [user(0), planner(1, tools=["ask_question"])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 9)], records, count=3) == (
        "waiting_input", "native_question_open")


def test_tool_running_is_only_the_observed_run_command_status_two():
    records = [user(0), planner(1, tools=["run_command"])]
    steps = [(0, 14, 3), (1, 15, 3), (2, 132, 2)]
    assert state(RUNNING, steps, records, count=0, skip=True) == ("working", "native_progress")
    # The same numeric shape without the observed skip-permissions context may be a permission review.
    assert state(RUNNING, steps, records, count=0, skip=False)[0] == "unknown"


@pytest.mark.parametrize("tool", ["write_file", "edit_file", "invoke_subagent", "search_web"])
def test_other_tools_at_status_two_are_unknown(tool):
    records = [user(0), planner(1, tools=[tool])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 2)], records)[0] == "unknown"


@pytest.mark.parametrize("tool", ["run_command", "write_file", "invoke_subagent"])
def test_status_nine_for_another_tool_is_an_unconfirmed_wait(tool):
    records = [user(0), planner(1, tools=[tool])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 9)], records) == ("unknown", "native_wait_unconfirmed")


def test_two_tool_calls_are_never_attributed():
    records = [user(0), planner(1, tools=["ask_question", "run_command"])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 9)], records)[0] == "unknown"
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 2)], records, skip=True)[0] == "unknown"
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 3)], records)[0] == "unknown"


def test_waiting_step_must_follow_its_planner_response():
    records = [user(0), planner(1, tools=["ask_question"])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 101, 3), (3, 132, 9)], records)[0] == "unknown"


@pytest.mark.parametrize("steps,records", [
    ([(0, 14, 3)], [user(0)]),                                                   # the input itself
    ([(0, 14, 3), (1, 101, 3)], [user(0), system(1)]),                           # a system message after it
    ([(0, 14, 3), (1, 15, 3), (2, 132, 3)], [user(0), planner(1, tools=["run_command"])]),  # tool done, reply pending
])
def test_model_turn_in_progress_is_working(steps, records):
    assert state(RUNNING, steps, records) == ("working", "native_progress")


def test_idle_needs_a_completed_reply_after_the_current_input():
    records = [user(0), planner(1)]
    assert state(IDLE, [(0, 14, 3), (1, 15, 3)], records) == ("idle", "native_turn_completed")


def test_lagging_summary_does_not_make_a_new_turn_idle():
    # V1's counterexample: a fresh input at idx 2, while summaries and steps still show the old turn.
    records = [user(0), planner(1), user(2)]
    assert state(IDLE, [(0, 14, 3), (1, 15, 3)], records, count=2)[0] == "unknown"


def test_idle_needs_matching_step_counts():
    records = [user(0), planner(1)]
    assert state(IDLE, [(0, 14, 3), (1, 15, 3)], records, count=1)[0] == "unknown"


def test_status_six_after_exit_is_never_waiting():
    records = [user(0), planner(1, tools=["ask_question"])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 6)], records)[0] == "unknown"
    assert state(IDLE, [(0, 14, 3), (1, 15, 3), (2, 132, 6)], records)[0] == "unknown"


def test_input_in_the_same_second_as_the_process_start_is_ambiguous():
    records = [user(0, s=0), planner(1, s=0, tools=["ask_question"])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 9)], records)[0] == "unknown"


def test_a_stale_wait_from_before_a_restart_is_not_waiting():
    # The question was asked before this process started; a resumed agy has no new input yet.
    records = [user(0, s=-120), planner(1, s=-120, tools=["ask_question"])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 9)], records)[0] == "unknown"
    assert state(IDLE, [(0, 14, 3), (1, 15, 3), (2, 132, 6)], records)[0] == "unknown"


def test_resumed_turn_binds_to_the_latest_input():
    records = [user(0, s=-300), planner(1, s=-300), user(2), system(3)]
    steps = [(0, 14, 3), (1, 15, 3), (2, 14, 3), (3, 101, 3)]
    assert state(RUNNING, steps, records) == ("working", "native_progress")
    assert state(IDLE, steps + [(4, 15, 3)], records + [planner(4)]) == ("idle", "native_turn_completed")


def test_input_missing_from_the_live_steps_is_unknown():
    records = [user(0), planner(1, tools=["ask_question"])]
    assert state(RUNNING, [(1, 15, 3), (2, 132, 9)], records)[0] == "unknown"
    assert state(RUNNING, [(0, 15, 3), (1, 15, 3), (2, 132, 9)], records)[0] == "unknown"


def test_working_evidence_older_than_the_freshness_window_is_stale():
    records = [user(0), planner(1, tools=["run_command"])]
    late = NOW + timedelta(seconds=400)
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 2)], records, now=late, skip=True) == (
        "unknown", "native_event_stale")


@pytest.mark.parametrize("summary", [None, {"status": "CASCADE_RUN_STATUS_BUSY", "not_fully_idle": 1},
                                     {"status": "CASCADE_RUN_STATUS_RUNNING", "not_fully_idle": 0},
                                     {"status": "CASCADE_RUN_STATUS_IDLE", "not_fully_idle": 1}])
def test_unobserved_summary_values_are_unknown(summary):
    records = [user(0), planner(1)]
    assert state(summary, [(0, 14, 3), (1, 15, 3)], records)[0] == "unknown"


def test_no_current_input_is_no_native_event():
    assert state(RUNNING, [], [])[:2] == ("unknown", "no_native_event")


# --- Transcript reading: tool names only, bounded, partial records -------------------------

def test_fixture_transcripts_parse_to_the_observed_states():
    def load(name):
        return [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]
    started = datetime(2026, 11, 16, 11, 39, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 16, 11, 45, 0, tzinfo=timezone.utc)
    assert ag.antigravity_state({**IDLE, "step_count": 2}, 2, [(0, 14, 3), (1, 15, 3)],
                                load("turn_completed.jsonl"), started, later)[:2] == ("idle", "native_turn_completed")
    assert ag.antigravity_state({**RUNNING, "step_count": 0}, 1, [(0, 14, 3)], load("turn_working.jsonl"),
                                started, datetime(2026, 11, 16, 11, 39, 40, tzinfo=timezone.utc))[0] == "working"
    question_start = datetime(2026, 11, 16, 11, 49, 0, 510000, tzinfo=timezone.utc)
    assert ag.antigravity_state({**RUNNING, "step_count": 3}, 3, [(0, 14, 3), (1, 15, 3), (2, 132, 9)],
                                load("question_waiting.jsonl"), question_start,
                                question_start + timedelta(seconds=10))[:2] == ("waiting_input", "native_question_open")
    tool_start = datetime(2026, 11, 16, 11, 43, 26, tzinfo=timezone.utc)
    assert ag.antigravity_state({**RUNNING, "step_count": 0}, 3, [(0, 14, 3), (1, 15, 3), (2, 132, 2)],
                                load("tool_turn_working.jsonl"), tool_start,
                                tool_start + timedelta(seconds=10), skip_permissions=True)[0] == "working"
    resume_start = datetime(2026, 11, 16, 11, 44, 12, tzinfo=timezone.utc)
    assert ag.antigravity_state({**IDLE, "step_count": 5}, 5,
                                [(0, 14, 3), (1, 15, 3), (2, 14, 3), (3, 101, 3), (4, 15, 3)],
                                load("resumed.jsonl"), resume_start, later)[0] == "idle"


def test_transcript_keeps_only_structural_fields(tmp_path):
    path = tmp_path / "transcript.jsonl"
    path.write_text(json.dumps({**planner(1, tools=["ask_question"]), "content": "private reply"}) + "\n")
    records, failure = ag.read_transcript_records(path, os.getuid())
    assert failure is None
    assert records == [{"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
                        "created_at": at(1), "tools": ["ask_question"]}]
    assert "private" not in json.dumps(records) and "never read" not in json.dumps(records)


@pytest.mark.parametrize("content,failure", [
    (json.dumps(user(0)) + "\n" + '{"step_index": 1, "sour', "observation_incomplete"),
    (json.dumps(user(0)) + "\n" + "not json\n", "observation_invalid"),
    (json.dumps(user(0)) + "\n" + "[1, 2]\n", "observation_invalid"),
])
def test_partial_or_invalid_transcript_records_are_unknown(tmp_path, content, failure):
    path = tmp_path / "transcript.jsonl"
    path.write_text(content)
    assert ag.read_transcript_records(path, os.getuid()) == (None, failure)


def test_a_long_transcript_keeps_the_current_input_beyond_the_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "_TAIL_BYTES", 4096)
    monkeypatch.setattr(ag, "_SCAN_STEP", 4096)
    path = tmp_path / "transcript.jsonl"
    filler = [{**system(i), "content": "x" * 200} for i in range(1, 80)]
    lines = [user(0)] + filler + [planner(80, tools=["ask_question"])]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines))
    records, failure = ag.read_transcript_records(path, os.getuid())
    assert failure is None
    assert records[0]["type"] == "USER_INPUT" and records[-1]["tools"] == ["ask_question"]


def test_transcript_scan_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(ag, "_TAIL_BYTES", 2048)
    monkeypatch.setattr(ag, "_SCAN_STEP", 2048)
    monkeypatch.setattr(ag, "_SCAN_BYTES", 4096)
    path = tmp_path / "transcript.jsonl"
    filler = [{**system(i), "content": "x" * 200} for i in range(1, 80)]
    path.write_text("".join(json.dumps(r) + "\n" for r in [user(0)] + filler))
    records, failure = ag.read_transcript_records(path, os.getuid())
    assert records is not None and not any(r["type"] == "USER_INPUT" for r in records)


# --- A fake agy home: binding, discovery, snapshots ----------------------------------------

def make_db(path, statements):
    connection = sqlite3.connect(path)
    connection.execute("pragma journal_mode=wal")
    connection.execute("pragma wal_autocheckpoint=0")
    for sql, args in statements:
        connection.execute(sql, args)
    connection.commit()
    return connection


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "agent-home"
    root = home / ".gemini" / "antigravity-cli"
    for sub in ("presence", "conversations", f"brain/{CID}/.system_generated/logs"):
        (root / sub).mkdir(parents=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (root / "presence" / f"{CID}.lock").write_text("")
    summaries = make_db(root / "conversation_summaries.db", [
        ("create table conversation_summaries (conversation_id text, step_count integer, workspace_uris text,"
         " status text, not_fully_idle integer)", ()),
        ("insert into conversation_summaries values (?, 3, ?, 'CASCADE_RUN_STATUS_RUNNING', 1)",
         (CID, json.dumps([workspace.resolve().as_uri()]))),
    ])
    conversation = make_db(root / "conversations" / f"{CID}.db", [
        ("create table steps (idx integer, step_type integer, status integer)", ()),
        ("insert into steps values (0, 14, 3), (1, 15, 3), (2, 132, 9)", ()),
    ])
    transcript = root / "brain" / CID / ".system_generated" / "logs" / "transcript.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in [user(0), planner(1, tools=["ask_question"])]))
    for path in [home, *home.rglob("*")]:
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    monkeypatch.setattr(ag, "_process_uid", lambda pid: os.getuid())
    monkeypatch.setattr(ag, "_passwd_home", lambda uid: str(home))
    monkeypatch.setattr(ag, "_exe_identity", lambda pid: ("agy", 1, 2))
    monkeypatch.setattr(ag, "_cmdline", lambda pid: ["agy", "--conversation", CID])  # never a host /proc read
    monkeypatch.setattr(ag, "_fd_targets", lambda pid: [str(root / "presence" / f"{CID}.lock"),
                                                        str(root / "conversations" / f"{CID}.db")])
    yield {"home": home, "root": root, "ws": str(workspace), "summaries": summaries,
           "conversation": conversation, "transcript": transcript}
    summaries.close()
    conversation.close()


def observe(h, now=NOW):
    return ag.observe_antigravity(4242, CID, h["ws"], now, START)[:2]


def test_live_wal_databases_give_the_waiting_state(home):
    assert observe(home) == ("waiting_input", "native_question_open")


def test_observation_never_changes_the_agent_files(home):
    files = sorted(p for p in home["root"].rglob("*") if p.is_file())
    before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size, p.stat().st_mtime_ns) for p in files}
    observe(home)
    assert sorted(p for p in home["root"].rglob("*") if p.is_file()) == files
    assert {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size, p.stat().st_mtime_ns) for p in files} == before


def test_discovery_needs_exactly_one_presence_lock_and_the_workspace(home, monkeypatch):
    assert ag.discover_antigravity_conversation(4242, home["ws"]) == CID
    assert ag.discover_antigravity_conversation(4242, str(Path(home["ws"]).parent)) is None
    lock = str(home["root"] / "presence" / f"{CID}.lock")
    other = home["root"] / "presence" / f"{OTHER}.lock"
    other.write_text("")
    monkeypatch.setattr(ag, "_fd_targets", lambda pid: [lock, str(other)])
    assert ag.discover_antigravity_conversation(4242, home["ws"]) is None
    monkeypatch.setattr(ag, "_fd_targets", lambda pid: [])
    assert ag.discover_antigravity_conversation(4242, home["ws"]) is None


def test_lock_outside_the_trusted_root_is_ignored(home, monkeypatch, tmp_path):
    foreign = tmp_path / "elsewhere" / ".gemini" / "antigravity-cli" / "presence"
    foreign.mkdir(parents=True)
    (foreign / f"{CID}.lock").write_text("")
    monkeypatch.setattr(ag, "_fd_targets", lambda pid: [str(foreign / f"{CID}.lock")])
    assert ag.discover_antigravity_conversation(4242, home["ws"]) is None
    assert observe(home)[0] == "unknown"


def test_the_passwd_home_is_used_not_the_process_environment(home, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-agent"))
    assert observe(home)[0] == "waiting_input"


@pytest.mark.parametrize("change", ["uid", "exe", "lock"])
def test_identity_changing_during_the_read_is_binding_changed(home, monkeypatch, change):
    calls = {"n": 0}
    lock = str(home["root"] / "presence" / f"{CID}.lock")
    other = home["root"] / "presence" / f"{OTHER}.lock"
    other.write_text("")

    def flip(first, second):
        def value(pid):
            calls["n"] += 1
            return first if calls["n"] == 1 else second
        return value
    if change == "uid":
        monkeypatch.setattr(ag, "_process_uid", flip(os.getuid(), os.getuid() + 1))
    elif change == "exe":
        monkeypatch.setattr(ag, "_exe_identity", flip(("agy", 1, 2), ("agy", 1, 3)))
    else:
        monkeypatch.setattr(ag, "_fd_targets", flip([lock], [str(other)]))
    assert observe(home) == ("unknown", "binding_changed")


def test_a_process_that_is_not_agy_is_not_observed(home, monkeypatch):
    monkeypatch.setattr(ag, "_exe_identity", lambda pid: ("python3", 1, 2))
    assert observe(home) == ("unknown", "session_mismatch")


def test_wrong_workspace_or_conversation_is_a_mismatch(home):
    assert ag.observe_antigravity(4242, CID, str(Path(home["ws"]).parent), NOW, START)[:2] == ("unknown", "session_mismatch")
    assert ag.observe_antigravity(4242, OTHER, home["ws"], NOW, START)[:2] == ("unknown", "session_mismatch")


@pytest.mark.parametrize("hazard", ["symlink_root", "world_writable", "symlink_db"])
def test_unsafe_files_are_never_read(home, hazard, tmp_path):
    if hazard == "symlink_root":
        real = tmp_path / "real-gemini"
        (home["home"] / ".gemini").rename(real)
        (home["home"] / ".gemini").symlink_to(real)
    elif hazard == "world_writable":
        os.chmod(home["root"] / "conversation_summaries.db", 0o666)
    else:
        target = home["root"] / "conversations" / f"{CID}.db"
        moved = tmp_path / "moved.db"
        target.rename(moved)
        target.symlink_to(moved)
    assert observe(home)[0] == "unknown"


def test_files_over_the_cap_are_incomplete(home, monkeypatch):
    monkeypatch.setattr(ag, "_FILE_CAP", 64)
    assert observe(home) == ("unknown", "observation_incomplete")


def test_writer_between_copy_rounds_retries_then_is_unknown(home, monkeypatch):
    generation = {"n": 0}

    def write(_attempt):
        generation["n"] += 1
        home["conversation"].execute("insert into steps values (?, 15, 3)", (100 + generation["n"],))
        home["conversation"].commit()
    monkeypatch.setattr(ag, "_between_rounds", write)
    assert observe(home) == ("unknown", "binding_changed")
    assert generation["n"] == 2  # one retry


def test_writer_only_during_the_first_attempt_gives_a_coherent_read(home, monkeypatch):
    def write(attempt):
        if attempt == 0:
            home["conversation"].execute("insert into steps values (3, 14, 3)")
            home["conversation"].commit()
    monkeypatch.setattr(ag, "_between_rounds", write)
    # The coherent second snapshot sees the new input step and therefore no current wait.
    assert observe(home)[0] == "unknown"


def test_wal_checkpoint_between_rounds_is_retried(home, monkeypatch):
    def checkpoint(attempt):
        if attempt == 0:
            home["conversation"].execute("pragma wal_checkpoint(truncate)")
    monkeypatch.setattr(ag, "_between_rounds", checkpoint)
    assert observe(home) == ("waiting_input", "native_question_open")


def test_file_replaced_between_rounds_is_unknown(home, monkeypatch):
    def replace(_attempt):
        path = home["root"] / "conversation_summaries.db"
        copy = path.with_name("copy.db")
        copy.write_bytes(path.read_bytes())
        os.chmod(copy, 0o600)
        os.replace(copy, path)
    monkeypatch.setattr(ag, "_between_rounds", replace)
    assert observe(home) == ("unknown", "binding_changed")


def test_concurrent_writer_never_yields_a_mixed_state(home):
    # Each generation writes the summary and the step in one transaction, so a coherent snapshot
    # is either the waiting question or the completed turn, never a mix.
    stop = threading.Event()

    def writer():
        connection = sqlite3.connect(home["root"] / "conversations" / f"{CID}.db")
        summary = sqlite3.connect(home["root"] / "conversation_summaries.db")
        flip = False
        while not stop.is_set():
            flip = not flip
            connection.execute("update steps set status=? where idx=2", (3 if flip else 9,))
            connection.commit()
            summary.execute("update conversation_summaries set status=?", (
                "CASCADE_RUN_STATUS_IDLE" if flip else "CASCADE_RUN_STATUS_RUNNING",))
            summary.commit()
        connection.close()
        summary.close()
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        results = {observe(home) for _ in range(20)}
    finally:
        stop.set()
        thread.join()
    # The two databases are separate transactions, as in agy itself: a snapshot may see the step done
    # while the summary is still RUNNING, which is the real "answered, reply pending" state (working).
    # It must never read the question as answered-and-idle, or claim idle on a tool step.
    assert results <= {("waiting_input", "native_question_open"), ("working", "native_progress"),
                       ("unknown", "binding_changed"), ("unknown", "completion_unconfirmed"),
                       ("unknown", "native_wait_unconfirmed"), ("unknown", "observation_invalid")}
    assert not any(state == "idle" for state, _reason in results)


def test_deadline_and_query_budget_are_enforced(home, monkeypatch):
    monkeypatch.setattr(ag, "_DEADLINE_SECONDS", 0.0)
    assert observe(home) == ("unknown", "observation_incomplete")


def test_damaged_database_copy_is_unknown(home):
    path = home["root"] / "conversations" / f"{CID}.db"
    home["conversation"].execute("pragma wal_checkpoint(truncate)")
    data = bytearray(path.read_bytes())
    data[100:4096] = b"\x00" * (4096 - 100)
    path.write_bytes(bytes(data))
    assert observe(home)[0] == "unknown"


def test_missing_transcript_is_unknown(home):
    home["transcript"].unlink()
    assert observe(home) == ("unknown", "native_log_unavailable")


# --- Wiring into the shared observer -------------------------------------------------------

def test_capability_reports_read_only_sessions():
    from app.services.providers.capabilities import normalize_capability_matrix
    assert normalize_capability_matrix("antigravity-cli")["sessions"]["state"] == "read_only"


def test_antigravity_is_a_discovered_identity_provider():
    from app.services import agent_activity_service as activity
    assert "antigravity-cli" in activity._DISCOVERED_IDENTITY_PROVIDERS


def test_new_gap_reason_can_pause_owner_observation():
    from app.services import owner_observation_pause as pause
    assert "native_wait_unconfirmed" not in pause._GAPS  # a wait is not an observation gap
    assert "native_log_unavailable" in pause._GAPS


def _dispatch(monkeypatch, session_id, observed=None, discovered=None):
    from app.services import agent_activity_service as activity
    monkeypatch.setattr(activity, "_process", lambda _: ("S", "27"))
    monkeypatch.setattr(activity, "discover_antigravity_conversation", lambda pid, cwd: discovered)
    monkeypatch.setattr(activity, "observe_antigravity", lambda *args, **kwargs: observed)
    binding = activity.ActivityBinding(41000, "27", "/tmp/agy-fixture-ws", tmux_target="%8", binding_id=1)
    metadata = {}
    result = activity._observe(1, "antigravity-cli", session_id, [binding], False, datetime.now(timezone.utc),
                               provenance=metadata)
    return result, metadata


def test_first_launch_discovers_the_conversation_and_asks_for_resume(monkeypatch):
    result, metadata = _dispatch(monkeypatch, None, discovered=CID)
    assert (result.state, result.reason) == ("unknown", "restart_needed_for_observation")
    assert metadata["discovered_session_id"] == CID


def test_no_discoverable_conversation_is_unknown(monkeypatch):
    result, metadata = _dispatch(monkeypatch, None, discovered=None)
    assert result.reason == "session_identity_unavailable" and "discovered_session_id" not in metadata


def test_open_question_becomes_a_bound_wait(monkeypatch):
    result, metadata = _dispatch(monkeypatch, CID, observed=("waiting_input", "native_question_open", None))
    assert result.waiting_input is not None and result.waiting_input.reason == "question"
    assert metadata["input_wait_reason"] == "question" and metadata["input_wait_binding_id"] == 1


def test_unknown_never_reports_a_wait_or_a_new_identity(monkeypatch):
    result, metadata = _dispatch(monkeypatch, CID, observed=("unknown", "native_wait_unconfirmed", None))
    assert result.waiting_input is None and (result.state, result.reason) == ("unknown", "native_wait_unconfirmed")
    assert "discovered_session_id" not in metadata and "input_wait_known" not in metadata


@pytest.mark.asyncio
async def test_discovered_identity_switches_the_slot_to_resume(db):
    from app.models.database import AgentTeamPreset, AgentTeamSlot
    from app.services import agent_activity_service as activity
    preset = AgentTeamPreset(name="agy team")
    db.add(preset)
    await db.flush()
    slots = [AgentTeamSlot(preset_id=preset.id, position=i, display_name=f"A{i}", provider="antigravity-cli",
                           repo_id=f"r{i}", repo_path=f"/w{i}", repo_name=f"w{i}", launch_mode="plain",
                           launch_options={"model": "m-1"}) for i in (1, 2)]
    db.add_all(slots)
    await db.commit()
    first, second = ((slot.id, "antigravity-cli", None) for slot in slots)
    current = {first[0]: first, second[0]: second}
    assert await activity._save_codex_identities(db, [first], [{"discovered_session_id": CID}], current)
    await db.refresh(slots[0])
    assert slots[0].launch_mode == "resume"
    assert slots[0].launch_options == {"model": "m-1", "session_id": CID, "use_last": False}
    assert not await activity._save_codex_identities(db, [second], [{"discovered_session_id": CID}], current)
    # A saved identity is never cleared by a later observation without a discovery.
    assert not await activity._save_codex_identities(db, [first], [{}], current)
    await db.refresh(slots[0])
    assert slots[0].launch_options["session_id"] == CID


# --- Review 5920805239 corrections ----------------------------------------------------------

# F1: the current turn's evidence must be well formed, ordered and not from the future.
def _bad(**changes):
    return lambda record: {**record, **changes}


@pytest.mark.parametrize("label,records,steps,summary,skip", [
    ("question P missing time", [user(0), _bad(created_at=None)(planner(1, tools=["ask_question"]))],
     [(0, 14, 3), (1, 15, 3), (2, 132, 9)], RUNNING, False),
    ("question P malformed time", [user(0), _bad(created_at="yesterday")(planner(1, tools=["ask_question"]))],
     [(0, 14, 3), (1, 15, 3), (2, 132, 9)], RUNNING, False),
    ("question P before process", [user(0), planner(1, s=-5, tools=["ask_question"])],
     [(0, 14, 3), (1, 15, 3), (2, 132, 9)], RUNNING, False),
    ("question P in the future", [user(0), planner(1, s=600, tools=["ask_question"])],
     [(0, 14, 3), (1, 15, 3), (2, 132, 9)], RUNNING, False),
    ("question P before U", [user(0, s=5), planner(1, s=3, tools=["ask_question"])],
     [(0, 14, 3), (1, 15, 3), (2, 132, 9)], RUNNING, False),
    ("tool P before U", [user(0, s=5), planner(1, s=3, tools=["run_command"])],
     [(0, 14, 3), (1, 15, 3), (2, 132, 2)], RUNNING, True),
    ("idle reply missing time", [user(0), _bad(created_at=None)(planner(1))],
     [(0, 14, 3), (1, 15, 3)], IDLE, False),
    ("idle reply in the future", [user(0), planner(1, s=600)], [(0, 14, 3), (1, 15, 3)], IDLE, False),
    ("idle reply before U", [user(0, s=5), planner(1, s=3)], [(0, 14, 3), (1, 15, 3)], IDLE, False),
    ("idle reply with a tool call", [user(0), planner(1, tools=["run_command"])], [(0, 14, 3), (1, 15, 3)], IDLE, False),
    ("idle reply with two tool calls", [user(0), planner(1, tools=["a", "b"])], [(0, 14, 3), (1, 15, 3)], IDLE, False),
    ("idle reply with an empty tool list", [user(0), planner(1, tools=[])], [(0, 14, 3), (1, 15, 3)], IDLE, False),
    ("U in the future", [user(0, s=600), planner(1, s=600)], [(0, 14, 3), (1, 15, 3)], IDLE, False),
    ("U index not an integer", [_bad(step_index=True)(user(0)), planner(1)], [(0, 14, 3), (1, 15, 3)], IDLE, False),
    ("system step with a bad time", [user(0), _bad(created_at=7)(system(1))], [(0, 14, 3), (1, 101, 3)], RUNNING, False),
])
def test_unsupported_turn_records_are_unknown(label, records, steps, summary, skip):
    assert state(summary, steps, records, skip=skip)[0] == "unknown", label


def test_an_input_in_the_process_start_second_is_refused_even_with_a_later_reply():
    # Only U's guard decides here: P is from a later second.
    records = [user(0, s=0), planner(1, s=1, tools=["ask_question"])]
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 9)], records)[0] == "unknown"
    assert state(RUNNING, [(0, 14, 3), (1, 15, 3), (2, 132, 9)], [user(0, s=1), planner(1, s=1, tools=["ask_question"])])[0] == "waiting_input"


# F2: the permission context is read from the bound process's exact argv.
@pytest.mark.parametrize("argv,expected", [
    (["agy", "--dangerously-skip-permissions", "--conversation", CID], True),
    (["/opt/agy/bin/agy", "--model", "m-1", "--effort", "high", "--dangerously-skip-permissions", "-i", "go"], True),
    (["agy", "--conversation", CID], False),
    (["agy", "-i", "--dangerously-skip-permissions"], False),  # the prompt text is not the flag
    (["agy"], False),
    (["agy", "--yolo"], None),
    (["agy", "--conversation", CID, "--dangerously-skip-permissions"], None),
    (["agy", "--model"], None),
    (["python3", "--dangerously-skip-permissions"], None),
    ([], None),
])
def test_permission_context_accepts_only_the_launched_shape(argv, expected):
    assert ag.permission_context(argv) is expected


@pytest.fixture
def running_tool(home):
    home["conversation"].execute("update steps set status=2 where idx=2")
    home["conversation"].commit()
    home["transcript"].write_text("".join(json.dumps(r) + "\n" for r in [user(0), planner(1, tools=["run_command"])]))
    os.chmod(home["transcript"], 0o600)
    return home


def test_running_command_is_working_only_with_skip_permissions(running_tool, monkeypatch):
    monkeypatch.setattr(ag, "_cmdline", lambda pid: ["agy", "--dangerously-skip-permissions", "--conversation", CID])
    assert observe(running_tool) == ("working", "native_progress")
    monkeypatch.setattr(ag, "_cmdline", lambda pid: ["agy", "--conversation", CID])
    assert observe(running_tool)[0] == "unknown"


def test_unreadable_argv_is_not_the_skip_context(running_tool, monkeypatch):
    def unreadable(pid):
        raise PermissionError(pid)
    monkeypatch.setattr(ag, "_cmdline", unreadable)
    assert observe(running_tool)[0] == "unknown"


def test_argv_changing_during_the_read_is_binding_changed(running_tool, monkeypatch):
    calls = {"n": 0}

    def argv(pid):
        calls["n"] += 1
        return ["agy", "--dangerously-skip-permissions", "--conversation", CID] if calls["n"] == 1 else ["agy", "--conversation", CID]
    monkeypatch.setattr(ag, "_cmdline", argv)
    assert observe(running_tool) == ("unknown", "binding_changed")


# F3: a conversation configured on more than one slot is never observed or discovered.
def test_duplicate_conversation_is_refused_before_observation(monkeypatch):
    from app.services import agent_activity_service as activity
    called = []
    monkeypatch.setattr(activity, "_process", lambda _: ("S", "27"))
    monkeypatch.setattr(activity, "discover_antigravity_conversation", lambda *a: called.append("discover") or CID)
    monkeypatch.setattr(activity, "observe_antigravity",
                        lambda *a, **k: called.append("observe") or ("working", "native_progress", None))
    binding = activity.ActivityBinding(41000, "27", "/tmp/agy-fixture-ws", tmux_target="%8", binding_id=1)
    for session_id in (CID, None):
        metadata = {}
        result = activity._observe(1, "antigravity-cli", session_id, [binding], True, datetime.now(timezone.utc),
                                   provenance=metadata)
        assert (result.state, result.reason) == ("unknown", "duplicate_native_identity")
        assert result.waiting_input is None
        assert not {"input_wait_known", "discovered_session_id"} & set(metadata)
    assert called == []


# F4: every directory below the trusted root is checked and held while it is read.
@pytest.mark.parametrize("target", ["conversations", "presence", f"brain/{CID}", f"brain/{CID}/.system_generated/logs"])
def test_a_symlinked_data_directory_is_never_followed(home, tmp_path, target):
    real = tmp_path / "elsewhere"
    (home["root"] / target).rename(real)
    (home["root"] / target).symlink_to(real)
    # Refused when the directory is opened (no-follow), not only by the later replacement check.
    assert observe(home) == ("unknown", "session_mismatch")
    if target == "presence":
        assert ag.discover_antigravity_conversation(4242, home["ws"]) is None


@pytest.mark.parametrize("target", ["conversations", "presence", "brain"])
def test_a_world_writable_data_directory_is_refused(home, target):
    os.chmod(home["root"] / target, 0o777)
    assert observe(home)[0] == "unknown"


def test_a_directory_replaced_during_the_read_is_binding_changed(home, monkeypatch):
    def replace(attempt):
        if attempt == 0:
            conversations = home["root"] / "conversations"
            conversations.rename(home["root"] / "conversations-old")
            conversations.mkdir(mode=0o700)
            (conversations / f"{CID}.db").write_bytes(b"")
    monkeypatch.setattr(ag, "_between_rounds", replace)
    assert observe(home) == ("unknown", "binding_changed")


def test_no_descriptor_is_left_open(home):
    before = len(os.listdir("/proc/self/fd"))
    observe(home)
    ag.discover_antigravity_conversation(4242, home["ws"])
    assert len(os.listdir("/proc/self/fd")) == before


# F5 (bounded part): one deadline from entry; an overdue known result or identity is refused.
class _Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


def test_a_result_finished_after_the_deadline_is_refused(home, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ag, "time", clock)
    identity = ag._identity

    def slow_identity(pid):
        clock.now += 0.6  # the binding at entry and the final recheck together cross the 1 s deadline
        return identity(pid)
    monkeypatch.setattr(ag, "_identity", slow_identity)
    assert observe(home) == ("unknown", "observation_incomplete")


def test_discovery_finished_after_the_deadline_returns_no_identity(home, monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(ag, "time", clock)
    identity = ag._identity

    def slow_identity(pid):
        clock.now += 0.6
        return identity(pid)
    monkeypatch.setattr(ag, "_identity", slow_identity)
    assert ag.discover_antigravity_conversation(4242, home["ws"]) is None


def test_results_inside_the_deadline_are_kept(home, monkeypatch):
    monkeypatch.setattr(ag, "time", _Clock())
    assert observe(home) == ("waiting_input", "native_question_open")
    assert ag.discover_antigravity_conversation(4242, home["ws"]) == CID


# --- Review 5921913278 corrections ----------------------------------------------------------

def _scene(home, records, steps, summary):
    """Rewrite the fake agy home: the transcript (raw records), the live steps and the summary row."""
    home["transcript"].write_text("".join(json.dumps(r) + "\n" for r in records))
    os.chmod(home["transcript"], 0o600)
    home["conversation"].execute("delete from steps")
    home["conversation"].executemany("insert into steps values (?, ?, ?)", steps)
    home["conversation"].commit()
    home["summaries"].execute("update conversation_summaries set status=?, not_fully_idle=?, step_count=?",
                              (summary["status"], summary["not_fully_idle"], len(steps)))
    home["summaries"].commit()


def _raw_planner(i, calls, s=1):
    record = {"step_index": i, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": at(s)}
    record["tool_calls"] = calls
    return record


IDLE_STEPS = [(0, 14, 3), (1, 15, 3)]
TOOL_DONE_STEPS = [(0, 14, 3), (1, 15, 3), (2, 132, 3)]
SYSTEM_STEPS = [(0, 14, 3), (1, 15, 3), (2, 101, 3)]


@pytest.mark.parametrize("calls", [{"name": "run_command"}, "run_command", 3, None, [["run_command"]], ["run_command"]])
def test_a_present_but_malformed_tool_call_value_is_never_idle(home, calls):
    _scene(home, [user(0), _raw_planner(1, calls)], IDLE_STEPS, IDLE)
    assert observe(home) == ("unknown", "observation_invalid")


@pytest.mark.parametrize("name", [True, 7, ["run_command"], {"n": "run_command"}, None])
def test_a_tool_call_without_a_string_name_is_never_working(home, name):
    _scene(home, [user(0), _raw_planner(1, [{"name": name}])], TOOL_DONE_STEPS, RUNNING)
    assert observe(home) == ("unknown", "observation_invalid")


@pytest.mark.parametrize("change", [{"step_index": "1"}, {"step_index": True}, {"step_index": None},
                                    {"type": 15}, {"source": ["MODEL"]}, {"created_at": 5}])
@pytest.mark.parametrize("branch", ["idle", "system_progress"])
def test_a_malformed_current_turn_record_is_never_skipped(home, change, branch):
    if branch == "idle":
        records = [user(0), {**planner(1), **change}]
        _scene(home, records, IDLE_STEPS, IDLE)
    else:
        records = [user(0), {**planner(1, tools=["run_command"]), **change}, system(2)]
        _scene(home, records, SYSTEM_STEPS, RUNNING)
    assert observe(home) == ("unknown", "observation_invalid")


def test_the_well_formed_scenes_still_classify(home):
    _scene(home, [user(0), planner(1)], IDLE_STEPS, IDLE)
    assert observe(home) == ("idle", "native_turn_completed")
    _scene(home, [user(0), planner(1, tools=["run_command"]), system(2)], SYSTEM_STEPS, RUNNING)
    assert observe(home) == ("working", "native_progress")


def test_malformed_live_step_rows_are_unknown():
    records = [user(0), planner(1)]
    assert state(IDLE, [(0, 14, 3), (1, "15", 3)], records)[0] == "unknown"
    assert state(IDLE, [(0, 14, 3), (True, 15, 3)], records)[0] == "unknown"


HELD = ["", ".gemini", ".gemini/antigravity-cli", ".gemini/antigravity-cli/conversations",
        ".gemini/antigravity-cli/presence", f".gemini/antigravity-cli/brain/{CID}/.system_generated/logs"]


@pytest.mark.parametrize("rel", HELD)
def test_a_directory_made_world_writable_in_place_during_the_read_is_refused(home, monkeypatch, rel):
    target = home["home"] / rel if rel else home["home"]

    def loosen(attempt):
        os.chmod(target, 0o777)
    monkeypatch.setattr(ag, "_between_rounds", loosen)
    # Held before the change: refused by the final recheck. The transcript directory is opened after
    # the copy rounds, so there it is refused when held (its post-parse change is tested below).
    expected = "session_mismatch" if rel.endswith("/logs") else "binding_changed"
    assert observe(home) == ("unknown", expected)
    os.chmod(target, 0o700)
    if rel in ("", ".gemini", ".gemini/antigravity-cli", ".gemini/antigravity-cli/presence"):  # held by discovery
        assert ag.discover_antigravity_conversation(4242, home["ws"]) is None


def test_the_transcript_directory_loosened_after_parsing_is_refused(home, monkeypatch):
    logs = home["root"] / "brain" / CID / ".system_generated" / "logs"
    parse = ag.read_transcript_records

    def parse_then_loosen(*args, **kwargs):
        result = parse(*args, **kwargs)
        os.chmod(logs, 0o777)
        return result
    monkeypatch.setattr(ag, "read_transcript_records", parse_then_loosen)
    assert observe(home) == ("unknown", "binding_changed")


@pytest.mark.parametrize("view", ["held", "named"])
def test_a_directory_owner_change_during_the_read_is_refused(home, monkeypatch, view):
    # Tests cannot chown; report a different owner for the conversations directory after the hold.
    import types
    conversations = home["root"] / "conversations"
    ident = (conversations.stat().st_dev, conversations.stat().st_ino)
    fake = types.SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if not name.startswith("__")})
    held = {"done": False}

    def chowned(info):
        values = list(info)
        values[4] = info.st_uid + 1
        return os.stat_result(values)

    def fstat(fd):
        info = os.fstat(fd)
        if view == "held" and held["done"] and (info.st_dev, info.st_ino) == ident:
            return chowned(info)
        return info

    def lstat(path, *args, **kwargs):
        info = os.lstat(path, *args, **kwargs)
        if view == "named" and held["done"] and (info.st_dev, info.st_ino) == ident:
            return chowned(info)
        return info
    fake.fstat, fake.lstat = fstat, lstat
    monkeypatch.setattr(ag, "os", fake)
    monkeypatch.setattr(ag, "_between_rounds", lambda attempt: held.update(done=True))
    assert observe(home) == ("unknown", "binding_changed")


def test_a_record_after_the_input_must_also_follow_it_by_index():
    # In transcript order after U, but not after U by index: the turn is not a plain sequence.
    records = [user(1), planner(0), planner(2)]
    assert state(IDLE, [(1, 14, 3), (2, 15, 3)], records)[0] == "unknown"
    assert state(IDLE, [(1, 14, 3), (2, 15, 3)], [user(1), planner(2)])[0] == "idle"


# --- Review 5922278508: tool evidence is observed only on replies ---------------------------

@pytest.mark.parametrize("calls", [{"name": "run_command"}, [{"name": True}], [{"name": "run_command"}]])
def test_tool_evidence_on_the_current_input_is_refused(home, calls):
    _scene(home, [{**user(0), "tool_calls": calls}], [(0, 14, 3)], RUNNING)
    assert observe(home) == ("unknown", "observation_invalid")


@pytest.mark.parametrize("calls", [{"name": "run_command"}, [{"name": True}], [{"name": "run_command"}]])
def test_tool_evidence_on_a_system_step_is_refused(home, calls):
    records = [user(0), planner(1, tools=["run_command"]), {**system(2), "tool_calls": calls}]
    _scene(home, records, SYSTEM_STEPS, RUNNING)
    assert observe(home) == ("unknown", "observation_invalid")


def test_the_same_scenes_without_tool_evidence_still_classify(home):
    _scene(home, [user(0)], [(0, 14, 3)], RUNNING)
    assert observe(home) == ("working", "native_progress")
    _scene(home, [user(0), planner(1, tools=["run_command"]), system(2)], SYSTEM_STEPS, RUNNING)
    assert observe(home) == ("working", "native_progress")


def test_raw_tool_evidence_on_a_non_reply_record_is_refused():
    assert state(RUNNING, [(0, 14, 3)], [{**user(0), "tool_calls": [{"name": "run_command"}]}])[0] == "unknown"
    assert state(RUNNING, SYSTEM_STEPS, [user(0), planner(1, tools=["run_command"]),
                                         {**system(2), "tool_calls": []}])[0] == "unknown"

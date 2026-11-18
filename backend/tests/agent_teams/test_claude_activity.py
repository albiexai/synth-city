"""Claude native activity fixtures. No harness, credentials, or live database."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.services import agent_activity_service as activity
from app.services import claude_activity_service as claude
from app.utils.path_utils import convert_path_to_folder_name


@pytest.fixture
def native(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    cwd = tmp_path / "project"; cwd.mkdir()
    home = tmp_path / "config"
    directory = home / "projects" / convert_path_to_folder_name(str(cwd))
    directory.mkdir(parents=True)
    session = str(uuid4())
    log = directory / (session + ".jsonl")
    proc = tmp_path / "proc"; (proc / "50").mkdir(parents=True)
    (proc / "50" / "cmdline").write_bytes(b"claude\0--resume\0" + session.encode() + b"\0")
    monkeypatch.setattr(claude, "_PROC", proc)
    monkeypatch.setattr(claude, "_home", lambda pid: home)
    monkeypatch.setattr(activity, "_process", lambda pid: ("S", "1000"))
    monkeypatch.setattr(activity, "_process_started_at", lambda start: now - timedelta(seconds=10))

    def row(kind, seconds, **values):
        return {"type": kind, "uuid": str(uuid4()), "sessionId": session,
                "cwd": str(cwd), "isSidechain": False,
                "timestamp": (now + timedelta(seconds=seconds)).isoformat(), **values}
    user = row("user", -4, message={"role": "user", "content": "Complete the assigned fixture."})
    tool = row("assistant", -3, message={"role": "assistant", "stop_reason": "tool_use"})
    result = row("user", -2, message={"role": "user", "content": [{"type": "tool_result"}]})
    end = row("assistant", -1, message={"role": "assistant", "stop_reason": "end_turn"})
    duration = row("system", -.5, subtype="turn_duration", parentUuid=end["uuid"])
    records = [user, tool, result, end, duration]
    def write(rows=None):
        log.write_text("".join(json.dumps(r) + "\n" for r in (records if rows is None else rows)))
    def observe(identity=session, duplicate=False, provenance=None):
        return activity._observe(2, "claude-code", identity,
            [activity.ActivityBinding(50, "1000", str(cwd))], duplicate, now, provenance=provenance)
    write()
    return locals()


@pytest.mark.parametrize("count,expected", [(1,"working"),(2,"working"),(3,"working"),(4,"idle"),(5,"idle")])
def test_claude_main_turn_boundaries(native, count, expected):
    native["write"](native["records"][:count])
    result = native["observe"]()
    assert result.state == expected
    assert set(result.model_dump()) == {"slot_id", "state", "reason", "observed_at", "waiting_input"}
    assert str(native["cwd"]) not in result.model_dump_json()


def test_claude_duration_deduplicates_completion(native):
    first, second = {}, {}
    native["write"](native["records"][:4]); a = native["observe"](provenance=first)
    native["write"](); b = native["observe"](provenance=second)
    assert a.observed_at == b.observed_at
    assert first["event_id"] == second["event_id"]
    assert second["event_source"] == "claude_end_turn"


@pytest.mark.parametrize("field,value", [("sessionId",str(uuid4())),("cwd","/different"),("timestamp","2099-01-01T00:00:00Z")])
def test_claude_invalid_record_is_unknown(native, field, value):
    native["end"][field] = value; native["write"]()
    assert native["observe"]().state == "unknown"


@pytest.mark.parametrize("argv", [b"claude\0prompt UUID\0",b"node\0--resume\0UUID\0",b"claude\0--resume\0other\0",b"claude\0--\0--resume\0UUID\0"])
def test_claude_uuid_must_be_bound_as_an_option(native, argv):
    (native["proc"] / "50" / "cmdline").write_bytes(argv.replace(b"UUID",native["session"].encode()))
    assert native["observe"]().reason == "session_mismatch"


def test_claude_duplicate_and_missing_identity_are_unknown(native):
    assert native["observe"](duplicate=True).reason == "duplicate_native_identity"
    assert native["observe"](identity=None).reason == "session_identity_unavailable"


def test_claude_inherited_completion_is_not_current(native):
    for row in native["records"]:
        row["timestamp"] = (native["now"] - timedelta(seconds=100)).isoformat()
    native["write"]()
    assert native["observe"]().state == "unknown"


def test_claude_stale_working_is_unknown(native):
    native["user"]["timestamp"] = (native["now"] - timedelta(seconds=200)).isoformat()
    native["write"]([native["user"]])
    # The current process predates the stale turn.
    from unittest.mock import patch
    with patch.object(activity,"_process_started_at",return_value=native["now"] - timedelta(seconds=300)):
        assert native["observe"]().reason == "native_event_stale"


@pytest.mark.parametrize("flag",["isSidechain","isReplay"])
def test_claude_other_completion_cannot_settle_main_turn(native, flag):
    native["end"][flag] = True
    native["write"](native["records"][:4])
    assert native["observe"]().state == "working"


def test_claude_new_turn_invalidates_prior_completion(native):
    native["write"](native["records"]+[native["row"]("user",0,message={"role":"user","content":"Next fixture."})])
    assert native["observe"]().state == "working"


def test_claude_unsupported_system_event_invalidates_prior_completion(native):
    native["write"](native["records"]+[native["row"]("system",0,subtype="input_requested")])
    metadata = {}; result = native["observe"](provenance=metadata)
    assert result.state == "unknown" and metadata.get("event_source") is None


def test_claude_tool_result_without_start_does_not_settle(native):
    native["write"]([native["result"],native["end"],native["duration"]])
    assert native["observe"]().state == "unknown"


def test_claude_interruption_is_not_successful_completion(native):
    native["write"]([native["user"],native["row"]("user",0,message={"role":"user","content":"[Request interrupted by user]"})])
    metadata = {}; result = native["observe"](provenance=metadata)
    assert result.reason == "native_turn_interrupted" and metadata.get("event_source") is None


def test_claude_partial_record_is_unknown(native):
    native["log"].write_text(native["log"].read_text().rstrip("\n"))
    assert native["observe"]().reason == "observation_incomplete"


def test_claude_world_writable_log_is_unknown(native):
    native["log"].chmod(0o666)
    assert native["observe"]().state == "unknown"


@pytest.mark.parametrize("count,state", [(3, "working"), (5, "idle")])
def test_claude_workspace_subdirectory_keeps_main_turn(native, count, state):
    backend = native["cwd"] / "backend"
    backend.mkdir()
    for row in native["records"][1:4]:
        row["cwd"] = str(backend)
    native["write"](native["records"][:count])
    assert native["observe"]().state == state


def test_claude_relative_cwd_is_not_bound_to_workspace(native):
    native["end"]["cwd"] = "backend"
    native["write"]()
    assert native["observe"]().state == "unknown"


def test_claude_subdirectory_symlink_outside_workspace_is_unknown(native):
    outside = native["cwd"].parent / "outside"
    outside.mkdir()
    link = native["cwd"] / "linked"
    link.symlink_to(outside, target_is_directory=True)
    native["end"]["cwd"] = str(link)
    native["write"]()
    assert native["observe"]().state == "unknown"


def _long_turn(native, *, completed=False):
    result = dict(native["result"])
    result["message"] = {"role": "user", "content": [
        {"type": "tool_result", "content": "x" * (2 * claude._TAIL_BYTES)},
    ]}
    rows = [native["user"], native["tool"], result]
    if completed:
        rows += [native["end"], native["duration"]]
    else:
        rows.append(native["row"]("assistant", -1,
                    message={"role": "assistant", "stop_reason": "tool_use"}))
    native["write"](rows)
    return rows


@pytest.mark.parametrize("completed,state", [(False, "working"), (True, "idle")])
def test_claude_long_turn_keeps_proven_start(native, completed, state):
    _long_turn(native, completed=completed)
    assert native["log"].stat().st_size > claude._TAIL_BYTES
    metadata = {}
    result = native["observe"](provenance=metadata)
    assert result.state == state
    if completed:
        assert result.reason == "native_turn_completed"
        assert metadata["event_source"] == "claude_end_turn"
    else:
        assert result.reason == "native_progress"
        assert metadata.get("event_source") is None


@pytest.mark.parametrize("field,value", [
    ("sessionId", str(uuid4())), ("cwd", "/different"),
    ("timestamp", "2099-01-01T00:00:00Z"),
])
def test_claude_lookback_start_must_keep_its_binding(native, field, value):
    native["user"][field] = value
    _long_turn(native)
    assert native["observe"]().state == "unknown"


@pytest.mark.parametrize("flag", ["isSidechain", "isReplay"])
def test_claude_lookback_cannot_use_other_turn_start(native, flag):
    native["user"][flag] = True
    _long_turn(native, completed=True)
    metadata = {}
    assert native["observe"](provenance=metadata).state == "unknown"
    assert metadata.get("event_source") is None


def test_claude_lookback_does_not_cross_unsupported_event(native):
    rows = _long_turn(native)
    rows.insert(1, native["row"]("system", -3.5, subtype="input_requested"))
    native["write"](rows)
    assert native["observe"]().state == "unknown"


def test_claude_lookback_does_not_accept_stale_progress(native, monkeypatch):
    rows = _long_turn(native)
    for row in rows:
        row["timestamp"] = (native["now"] - timedelta(seconds=200)).isoformat()
    native["write"](rows)
    monkeypatch.setattr(activity, "_process_started_at",
                        lambda start: native["now"] - timedelta(seconds=300))
    assert native["observe"]().reason == "native_event_stale"


def _track_reads(monkeypatch, callback=None):
    original = claude.os.fdopen
    reads = []

    class TrackedStream:
        def __init__(self, *args, **kwargs):
            self.stream = original(*args, **kwargs)

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def fileno(self):
            return self.stream.fileno()

        def seek(self, offset):
            return self.stream.seek(offset)

        def read(self, size):
            value = self.stream.read(size)
            reads.append(len(value))
            if callback:
                callback(len(reads))
            return value

    monkeypatch.setattr(claude.os, "fdopen", TrackedStream)
    return reads


def test_claude_lookback_byte_budget_stays_unknown(native, monkeypatch):
    monkeypatch.setattr(claude, "_TAIL_BYTES", 512)
    monkeypatch.setattr(claude, "_MAX_HISTORY_BYTES", 4096)
    result = dict(native["result"])
    result["message"] = {"role": "user", "content": [
        {"type": "tool_result", "content": "x" * 8192},
    ]}
    native["write"]([native["user"], native["tool"], result,
                     native["end"], native["duration"]])
    reads = _track_reads(monkeypatch)
    metadata = {}
    observed = native["observe"](provenance=metadata)
    assert observed.state == "unknown" and observed.reason == "native_history_limit"
    assert sum(reads) == claude._MAX_HISTORY_BYTES
    assert len(reads) <= 8
    assert metadata.get("event_source") is None


def test_claude_file_change_during_lookback_is_unknown(native, monkeypatch):
    _long_turn(native)

    def change_on_second_read(count):
        if count == 2:
            with native["log"].open("a") as stream:
                stream.write(json.dumps(native["row"]("user", 0,
                    message={"role": "user", "content": "Next fixture."})) + "\n")

    reads = _track_reads(monkeypatch, change_on_second_read)
    assert native["observe"]().reason == "binding_changed"
    assert len(reads) > 1


@pytest.mark.parametrize("completed,state", [(False, "working"), (True, "idle")])
def test_claude_prior_turn_order_does_not_poison_new_turn(native, completed, state):
    earlier_prompt = native["row"]("user", -8,
        message={"role": "user", "content": "Earlier fixture."})
    earlier_tool = native["row"]("assistant", -6,
        message={"role": "assistant", "stop_reason": "tool_use"})
    earlier_result = native["row"]("user", -7,
        message={"role": "user", "content": [{"type": "tool_result"}]})
    earlier_end = native["row"]("assistant", -5,
        message={"role": "assistant", "stop_reason": "end_turn"})
    current = native["records"] if completed else native["records"][:3]
    native["write"]([earlier_prompt, earlier_tool, earlier_result, earlier_end] + current)
    assert native["observe"]().state == state


def test_claude_current_turn_order_still_refuses(native):
    native["result"]["timestamp"] = native["user"]["timestamp"]
    native["write"]()
    assert native["observe"]().reason == "observation_invalid"


def test_claude_latest_mismatched_prompt_does_not_reuse_prior_turn(native):
    prompt = native["row"]("user", 0, sessionId=str(uuid4()),
        message={"role": "user", "content": "Other fixture."})
    native["write"](native["records"] + [prompt])
    metadata = {}
    assert native["observe"](provenance=metadata).reason == "session_mismatch"
    assert metadata.get("event_source") is None


def test_claude_first_turn_with_launch_uuid_is_observed(native):
    (native["proc"] / "50" / "cmdline").write_bytes(
        b"claude\0--session-id\0" + native["session"].encode() + b"\0")
    native["write"]([native["user"]])
    result = native["observe"]()
    assert (result.state, result.reason) == ("working", "native_turn_started")


# ---- compact_boundary and informational keep the state (#1072) -----------------------------------------
#
# The two real shapes below are the key sets of the events in Claude Code 2.1.292 transcripts (read as key
# names and types only). The values are sanitized fixture values, not transcript content.
_REAL_SHAPES = {
    "compact_boundary": lambda: {
        "content": "Conversation compacted", "level": "info", "logicalParentUuid": str(uuid4()), "parentUuid": None,
        "compactMetadata": {"trigger": "auto", "preTokens": 1, "postTokens": 1, "durationMs": 1,
                            "cumulativeDroppedTokens": 0, "preCompactDiscoveredTools": [], "precomputed": False,
                            "preservedMessages": 0, "preservedSegment": {}},
    },
    "informational": lambda: {"content": "A notice", "level": "notice", "isMeta": False, "parentUuid": str(uuid4())},
}
_KEEPING = ["compact_boundary", "informational"]


def _shaped(native, subtype, seconds, **overrides):
    values = {"userType": "external", "entrypoint": "cli", "gitBranch": "main", "slug": "fixture",
              "version": "2.1.292", **_REAL_SHAPES[subtype](), **overrides}
    return native["row"]("system", seconds, subtype=subtype, **values)


def _observed(native, rows):
    native["write"](rows)
    provenance = {}
    return native["observe"](provenance=provenance), provenance


def _view(result):
    return result.state, result.reason, result.observed_at


# (rows of the turn before the event, the event's time, the rows after it)
_POSITIONS = {
    "after the prompt": (1, -3.5),
    "after a tool call": (2, -2.5),
    "after a tool result": (3, -1.5),
    "after the final answer": (4, -.8),
    "after the settled turn": (5, 0),
}


@pytest.mark.parametrize("subtype", _KEEPING)
@pytest.mark.parametrize("position", list(_POSITIONS))
def test_claude_compact_and_informational_events_keep_the_state(native, subtype, position):
    count, seconds = _POSITIONS[position]
    records = native["records"]
    plain, _ = _observed(native, records[:count])
    with_event, _ = _observed(native, records[:count] + [_shaped(native, subtype, seconds)])

    assert with_event.state != "unknown"
    assert _view(with_event) == _view(plain)


@pytest.mark.parametrize("subtype", _KEEPING)
def test_claude_event_between_the_final_answer_and_its_duration_keeps_the_completion(native, subtype):
    records = native["records"]
    plain, plain_provenance = _observed(native, records)
    rows = records[:4] + [_shaped(native, subtype, -.8)] + records[4:]
    with_event, provenance = _observed(native, rows)

    assert (with_event.state, with_event.reason) == ("idle", "native_turn_completed")
    assert provenance["event_source"] == "claude_end_turn"
    assert provenance["event_id"] == plain_provenance["event_id"]


def test_claude_several_kept_events_in_a_row_keep_the_state(native):
    records = native["records"]
    plain, _ = _observed(native, records[:3])
    rows = records[:3] + [_shaped(native, "compact_boundary", -1.9), _shaped(native, "informational", -1.8),
                          _shaped(native, "compact_boundary", -1.7), _shaped(native, "informational", -1.6)]
    with_events, _ = _observed(native, rows)

    assert _view(with_events) == _view(plain)


@pytest.mark.parametrize("subtype", _KEEPING)
def test_claude_a_kept_event_does_not_move_the_idle_time(native, subtype):
    """A notice or a compaction record is not member activity: idle_since must not move."""
    plain, _ = _observed(native, native["records"])
    later, _ = _observed(native, native["records"] + [_shaped(native, subtype, 0)])

    assert later.state == "idle" and later.observed_at == plain.observed_at


@pytest.mark.parametrize("subtype", ["input_requested", "api_error", "a_future_subtype"])
def test_claude_other_system_subtypes_still_give_unknown(native, subtype):
    rows = native["records"] + [native["row"]("system", 0, subtype=subtype)]
    result, provenance = _observed(native, rows)

    assert (result.state, result.reason) == ("unknown", "native_event_unsupported")
    assert provenance.get("event_source") is None


@pytest.mark.parametrize("subtype", _KEEPING)
@pytest.mark.parametrize("field,value,reason", [
    ("sessionId", str(uuid4()), "session_mismatch"),
    ("cwd", "/different", "session_mismatch"),
    ("timestamp", "2099-01-01T00:00:00Z", "observation_invalid"),
])
def test_claude_a_kept_event_is_validated_before_it_is_ignored(native, subtype, field, value, reason):
    """Validation comes first: a kept subtype from another session, another cwd or the future is not trusted."""
    event = _shaped(native, subtype, -.8, **{field: value})
    result, _ = _observed(native, native["records"][:4] + [event])

    assert (result.state, result.reason) == ("unknown", reason)


@pytest.mark.parametrize("subtype", _KEEPING)
def test_claude_a_kept_event_older_than_the_last_real_event_is_invalid(native, subtype):
    event = _shaped(native, subtype, -1.5)  # earlier than the final answer, but written after it
    result, _ = _observed(native, native["records"][:4] + [event])

    assert (result.state, result.reason) == ("unknown", "observation_invalid")


@pytest.mark.parametrize("subtype", _KEEPING)
def test_claude_a_kept_event_in_a_side_chain_is_ignored_as_before(native, subtype):
    plain, _ = _observed(native, native["records"][:2])
    side, _ = _observed(native, native["records"][:2] + [_shaped(native, subtype, -2.5, isSidechain=True)])

    assert _view(side) == _view(plain)


@pytest.mark.parametrize("subtype", _KEEPING)
def test_claude_a_kept_event_does_not_refresh_a_stale_working_turn(native, subtype):
    """The known limit. A kept event does not move observed_at, so a working turn whose last real event is
    older than the staleness window still reads as stale, even if a compaction was just recorded."""
    from unittest.mock import patch

    native["user"]["timestamp"] = (native["now"] - timedelta(seconds=200)).isoformat()
    native["write"]([native["user"], _shaped(native, subtype, -1)])
    with patch.object(activity, "_process_started_at", return_value=native["now"] - timedelta(seconds=300)):
        assert native["observe"]().reason == "native_event_stale"


# ---- the ordering fence includes kept events (review 5940065622) ----------------------------------------


def test_claude_a_kept_event_then_an_earlier_kept_event_is_out_of_order(native):
    """V1's first sequence: prompt, a kept event at a later time, then another kept event at an earlier time."""
    rows = [native["user"], _shaped(native, "compact_boundary", -1), _shaped(native, "informational", -2)]
    result, _ = _observed(native, rows)

    assert (result.state, result.reason) == ("unknown", "observation_invalid")


def test_claude_a_kept_event_then_an_earlier_real_event_is_out_of_order(native):
    """V1's second sequence: prompt, a kept event at a later time, then an assistant event at an earlier time."""
    rows = [native["user"], _shaped(native, "compact_boundary", -1), native["tool"]]  # user -4, kept -1, tool -3
    result, _ = _observed(native, rows)

    assert (result.state, result.reason) == ("unknown", "observation_invalid")


@pytest.mark.parametrize("first,second", [("compact_boundary", "informational"), ("informational", "compact_boundary")])
def test_claude_two_kept_events_out_of_order_are_invalid_in_either_order(native, first, second):
    rows = native["records"][:3] + [_shaped(native, first, -1.5), _shaped(native, second, -1.7)]
    result, _ = _observed(native, rows)

    assert (result.state, result.reason) == ("unknown", "observation_invalid")


@pytest.mark.parametrize("subtype", _KEEPING)
def test_claude_a_real_event_after_a_kept_event_in_order_still_passes(native, subtype):
    """The fence only refuses going backward. A later row after a kept row is fine and keeps the state."""
    records = native["records"]
    plain, _ = _observed(native, records[:3])
    rows = records[:2] + [_shaped(native, subtype, -2.5), records[2]]  # user -4, tool -3, kept -2.5, result -2
    result, _ = _observed(native, rows)

    assert _view(result) == _view(plain)


@pytest.mark.parametrize("subtype", _KEEPING)
def test_claude_a_kept_event_with_the_same_time_as_the_last_real_event_is_in_order(native, subtype):
    records = native["records"]
    plain, _ = _observed(native, records[:4])
    same_time = _shaped(native, subtype, -1)  # the final answer is at -1
    result, _ = _observed(native, records[:4] + [same_time])

    assert _view(result) == _view(plain)


def _split_final_message(native, *, second_message_id="msg-final"):
    """The final message written as two rows, one per content block (#1205, Claude Code 2.1.292)."""
    thinking = native["row"]("assistant", -1, message={
        "id": "msg-final", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "thinking"}]})
    text = native["row"]("assistant", -.8, parentUuid=thinking["uuid"], message={
        "id": second_message_id, "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text"}]})
    duration = native["row"]("system", -.5, subtype="turn_duration", parentUuid=text["uuid"])
    native["write"]([native["user"], native["tool"], native["result"], thinking, text, duration])


def test_claude_final_message_split_into_rows_settles_idle(native):
    _split_final_message(native)
    metadata = {}
    result = native["observe"](provenance=metadata)
    assert (result.state, result.reason) == ("idle", "native_turn_completed")
    assert metadata["event_source"] == "claude_end_turn"


def test_claude_end_turn_from_another_message_cannot_continue_a_settled_turn(native):
    _split_final_message(native, second_message_id="msg-other")
    assert native["observe"]().state != "idle"


def test_claude_split_final_message_keeps_one_settlement_identity(native):
    # Reading between the two rows and after them must name the same completion (no double settlement).
    thinking = native["row"]("assistant", -1, message={
        "id": "msg-final", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "thinking"}]})
    text = native["row"]("assistant", -.8, parentUuid=thinking["uuid"], message={
        "id": "msg-final", "role": "assistant", "stop_reason": "end_turn", "content": [{"type": "text"}]})
    head = [native["user"], native["tool"], native["result"], thinking]
    first, second = {}, {}
    native["write"](head); native["observe"](provenance=first)
    native["write"](head + [text]); native["observe"](provenance=second)
    assert first["event_id"] == second["event_id"]

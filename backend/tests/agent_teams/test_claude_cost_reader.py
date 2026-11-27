"""#1249: the Claude Code cost reader and the dated price table (design §4, the #1248 spike's reader rules).

Each assistant message counts once, at its first row's time, inside the campaign window. A session counts for a member
only when it is the session the product recorded for that member's launch; every other session in the team's folders
is unattributed usage, shown and never dropped. A file that can't be read safely, an unknown format or a model without
a dated price is unknown with a reason, never zero.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import claude_cost_reader as reader
from app.services.pricing_service import PricingService
from app.utils.path_utils import convert_path_to_folder_name

START = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
END = START + timedelta(days=1)
OPUS = "claude-opus-4-8"  # priced in the dated table: 5 / 25 / 6.25 / 0.50 USD per million tokens
UNPRICED = "claude-opus-5-5"
SONNET_4 = "claude-sonnet-4-20250514"  # tiered: 3 / 15 / 3.75 / 0.30, above 200k 6 / 22.50 / 7.50 / 0.60
SA, SB, SZ, S1, S2 = (f"{n}{n}{n}{n}{n}{n}{n}{n}-{n}{n}{n}{n}-4{n}{n}{n}-8{n}{n}{n}-{n * 12}" for n in "12345")


def _row(message_id, at, *, model=OPUS, tokens=(1000, 200, 50, 10), session=None, block=0):
    inp, cache_creation, cache_read, out = tokens
    row = {"type": "assistant", "timestamp": at.isoformat().replace("+00:00", "Z"),
            "message": {"id": message_id, "model": model, "content": [{"type": "text", "text": f"block {block}"}],
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cache_creation,
                                  "cache_read_input_tokens": cache_read, "output_tokens": out}}}
    if session is not None:
        row["sessionId"] = session
    return row


def _session(home, workspace, session_id, rows, *, mode=0o600):
    folder = home / ".claude" / "projects" / convert_path_to_folder_name(str(workspace))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    lines = [json.dumps({"type": "user", "sessionId": session_id, "timestamp": START.isoformat()})]
    for row in rows:  # a row names its session and working directory, as Claude Code writes them
        if isinstance(row, dict) and row.get("type") == "assistant":
            row = {"sessionId": session_id, "cwd": str(workspace), **row}
        lines.append(json.dumps(row) if isinstance(row, dict) else row)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(mode)
    return path


@pytest.fixture
def team(tmp_path):
    """Two Claude Code members in their own workspaces, each with its recorded launch session."""
    home = tmp_path / "home"
    a, b = tmp_path / "work" / "build-1", tmp_path / "work" / "build-2"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    _session(home, a, SA, [
        _row("m1", START + timedelta(hours=1), session=SA),
        _row("m1", START + timedelta(hours=1, seconds=1), session=SA, block=1),  # the same message's next block
        _row("m2", START + timedelta(hours=2), session=SA, tokens=(2000, 0, 0, 100)),
        _row("m0", START - timedelta(minutes=5), session=SA, tokens=(9999, 9999, 9999, 9999)),  # before
    ])
    _session(home, b, SB, [
        _row("n1", START + timedelta(hours=3), session=SB, tokens=(500, 0, 1000, 50)),
        _row("n2", END + timedelta(minutes=1), session=SB, tokens=(7777, 0, 0, 7777)),  # after the window
    ])
    _session(home, a, SZ, [_row("u1", START + timedelta(hours=4), session=SZ, tokens=(300, 0, 0, 30))])
    members = [reader.ClaudeMember("B1", str(a), SA), reader.ClaudeMember("B2", str(b), SB)]
    return home, members


def _read(home, members, **kwargs):
    return reader.read_claude_usage(START, END, members, home=home, uid=os.getuid(), **kwargs)


def _price(tokens, model=OPUS):
    price = PricingService.MODEL_PRICING[model]
    inp, cache_creation, cache_read, out = tokens
    return (inp * price["input"] + cache_creation * price["cache_creation"] + cache_read * price["cache_read"]
            + out * price["output"])


# The acceptance: two members and a usage line outside the window
def test_two_members_are_read_over_the_window_only(team):
    home, members = team
    report = _read(home, members)
    by_member = {m.member: m for m in report.members}
    b1, b2 = by_member["B1"], by_member["B2"]
    assert (b1.messages, b2.messages) == (2, 1)  # m0 and n2 are outside the window
    (b1_model,) = b1.models
    assert (b1_model.input, b1_model.cache_creation, b1_model.cache_read, b1_model.output) == (3000, 200, 50, 110)
    assert b1.cost_usd == pytest.approx(_price((1000, 200, 50, 10)) + _price((2000, 0, 0, 100)))
    assert b2.cost_usd == pytest.approx(_price((500, 0, 1000, 50)))
    assert (b1.unknown_reasons, b2.unknown_reasons) == ([], [])
    assert (report.members_priced, report.members_total) == (2, 2)
    assert report.prices_as_of == PricingService.PRICES_AS_OF == date(2026, 7, 2)


def test_a_session_the_product_did_not_record_is_unattributed_never_dropped(team):
    home, members = team
    report = _read(home, members)
    (other,) = report.unattributed
    assert (other.session_id, other.messages, other.models[0].input) == (SZ, 1, 300)
    assert all(m.messages <= 2 for m in report.members)  # and it isn't counted as B1's


def test_a_member_without_a_recorded_session_is_unknown(team):
    home, members = team
    members[0] = reader.ClaudeMember("B1", members[0].workspace, None)
    by_member = {m.member: m for m in _read(home, members).members}
    assert by_member["B1"].cost_usd is None and by_member["B1"].unknown_reasons == ["binding_unverified"]
    assert {u.session_id for u in _read(home, members).unattributed} == {SA, SZ}


def test_repeated_rows_of_one_message_count_once_at_the_first_rows_time(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S1, [
        _row("x", START - timedelta(seconds=1), session=S1),  # its first row is before the window
        _row("x", START + timedelta(seconds=1), session=S1, block=1),
    ])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.messages == 0 and member.cost_usd == 0.0


def test_an_unpriced_model_shows_its_tokens_with_cost_unknown(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S1, [_row("x", START + timedelta(hours=1), model=UNPRICED, session=S1)])
    report = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)])
    (member,) = report.members
    assert member.cost_usd is None and member.unknown_reasons == [f"price_unknown:{UNPRICED}"]
    assert (member.models[0].model, member.models[0].input, member.models[0].cost_usd) == (UNPRICED, 1000, None)
    assert (report.members_priced, report.members_total) == (0, 1)


@pytest.mark.parametrize("damage", ["symlink", "other_writable", "group_writable", "oversized", "unreadable"])
def test_a_file_that_cannot_be_read_safely_is_unknown(tmp_path, damage):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    path = _session(home, workspace, S1, [_row("x", START + timedelta(hours=1), session=S1)])
    kwargs = {}
    if damage == "symlink":
        target = tmp_path / "elsewhere.jsonl"
        path.rename(target)
        path.symlink_to(target)
    elif damage == "other_writable":
        path.chmod(0o602)
    elif damage == "group_writable":
        path.chmod(0o620)
    elif damage == "oversized":
        kwargs["max_bytes"] = 10
    else:
        path.chmod(0o000)
        if os.access(path, os.R_OK):
            pytest.skip("running with privileges that read any file")
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)], **kwargs).members
    assert member.cost_usd is None and member.unknown_reasons == ["native_log_unreadable"]


def test_a_file_owned_by_another_user_is_unknown(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S1, [_row("x", START + timedelta(hours=1), session=S1)])
    report = reader.read_claude_usage(START, END, [reader.ClaudeMember("B1", str(workspace), S1)],
                                      home=home, uid=os.getuid() + 1)
    assert report.members[0].unknown_reasons == ["native_log_unreadable"]


@pytest.mark.parametrize("bad", ['{"type": "assistant", "message": {"id": "x"}}', "not json",
                                 ('{"type": "assistant", "timestamp": "2026-11-20T10:00:00Z", "message": '
                                  '{"id": "x", "model": "claude-opus-4-8", "usage": {"input_tokens": "many"}}}'),
                                 ('{"type": "assistant", "timestamp": "2026-11-20T10:00:00Z", "message": '
                                  '{"id": "x", "model": "claude-opus-4-8"}}'), "[1, 2]",
                                 '{"timestamp": "2026-11-20T10:00:00Z", "message": {"id": "y"}}', '{"type": 5}'],
                         ids=["no_usage", "not_json", "bad_count", "usage_missing", "array", "no_type", "type_not_text"])
def test_a_row_in_an_unknown_format_is_unknown(tmp_path, bad):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S1, [_row("ok", START + timedelta(hours=1), session=S1), bad])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.cost_usd is None and member.unknown_reasons == ["format_unknown"]


def test_a_member_whose_session_file_is_missing_is_unknown(tmp_path):
    workspace = tmp_path / "w"
    workspace.mkdir()
    (member,) = _read(tmp_path / "home", [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.cost_usd is None and member.unknown_reasons == ["no_native_session"]


def test_a_codex_member_is_not_read_here(tmp_path):
    (member,) = _read(tmp_path / "home", [reader.ClaudeMember("C1", str(tmp_path), "s", provider="codex-cli")]).members
    assert member.cost_usd is None and member.unknown_reasons == ["harness_not_read"]


def test_a_window_with_no_assistant_usage_is_a_known_zero(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S1, [{"type": "system", "subtype": "notice", "timestamp": START.isoformat()}])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert (member.messages, member.cost_usd, member.unknown_reasons) == (0, 0.0, [])


def test_a_directory_at_the_session_path_is_unknown_for_that_member_only(team):
    home, members = team
    path = home / ".claude" / "projects" / convert_path_to_folder_name(members[0].workspace) / f"{SA}.jsonl"
    path.unlink()
    path.mkdir()
    by_member = {m.member: m for m in _read(home, members).members}
    assert by_member["B1"].cost_usd is None and by_member["B1"].unknown_reasons == ["native_log_unreadable"]
    assert by_member["B2"].cost_usd == pytest.approx(_price((500, 0, 1000, 50)))


def test_a_fifo_at_the_session_path_is_unknown(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    path = _session(home, workspace, S1, [])
    path.unlink()
    os.mkfifo(path, 0o600)
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.cost_usd is None and member.unknown_reasons == ["native_log_unreadable"]


def test_a_failing_file_operation_is_unknown_and_the_file_is_closed(tmp_path, monkeypatch):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S1, [_row("x", START + timedelta(hours=1))])
    opened, closed = [], []

    def fail(descriptor):
        raise OSError("EIO")

    def track(path, flags):
        opened.append(os.open(path, flags))
        return opened[-1]

    def close(descriptor):
        closed.append(descriptor)
        os.close(descriptor)

    shim = SimpleNamespace(**{name: getattr(os, name) for name in ("O_RDONLY", "O_NOFOLLOW", "O_NONBLOCK", "fdopen",
                                                                 "listdir")},
                           open=track, fstat=fail, close=close)
    monkeypatch.setattr(reader, "os", shim)
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.cost_usd is None and member.unknown_reasons == ["native_log_unreadable"]
    assert closed == opened and len(opened) == 1


# Who owns a session
@pytest.mark.parametrize("identity", [{"sessionId": SZ}, {"cwd": "/elsewhere"}, {"cwd": "w"}, {"cwd": None}, {"cwd": 5}],
                         ids=["other_session", "cwd_outside", "cwd_relative", "cwd_missing", "cwd_not_text"])
def test_rows_that_name_another_session_or_place_are_not_charged(tmp_path, monkeypatch, identity):
    home, workspace = tmp_path / "home", tmp_path / "w"
    monkeypatch.chdir(tmp_path)  # a relative cwd would resolve to the workspace itself
    workspace.mkdir()
    _session(home, workspace, S1, [_row("ok", START + timedelta(hours=1)),
                                   {**_row("x", START + timedelta(hours=2)), "sessionId": S1, "cwd": str(workspace),
                                    **identity}])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.cost_usd is None and member.unknown_reasons == ["session_mismatch"]


def test_a_row_from_a_subdirectory_of_the_workspace_is_the_members(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    (workspace / "backend").mkdir(parents=True)
    _session(home, workspace, S1, [{**_row("x", START + timedelta(hours=1)), "cwd": str(workspace / "backend")}])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.cost_usd == pytest.approx(_price((1000, 200, 50, 10)))


@pytest.mark.parametrize("session_id", ["aaaa", "../w/x", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", f"{{{SA}}}"])
def test_a_recorded_session_that_is_not_a_canonical_uuid_is_unverified(team, session_id):
    home, members = team
    members[0] = reader.ClaudeMember("B1", members[0].workspace, session_id)
    by_member = {m.member: m for m in _read(home, members).members}
    assert by_member["B1"].cost_usd is None and by_member["B1"].unknown_reasons == ["binding_unverified"]


def test_two_members_claiming_one_session_are_unknown_and_it_is_unattributed_once(team):
    home, members = team
    members.append(reader.ClaudeMember("B3", members[0].workspace, SA))
    report = _read(home, members)
    by_member = {m.member: m for m in report.members}
    for name in ("B1", "B3"):
        assert by_member[name].cost_usd is None and by_member[name].unknown_reasons == ["competing_binding"]
    assert by_member["B2"].unknown_reasons == []
    claimed = [u for u in report.unattributed if u.session_id == SA]
    assert len(claimed) == 1 and claimed[0].messages == 2 and claimed[0].unknown_reasons == ["competing_binding"]


def _failing_resolve(monkeypatch, bad, error):
    resolve = Path.resolve

    def fail(self, *args, **kwargs):
        if str(self) == bad:
            raise error("cannot resolve")
        return resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail)


@pytest.mark.parametrize("error", [OSError, RuntimeError, ValueError])
def test_a_row_cwd_that_cannot_be_resolved_is_unknown_for_that_member_only(team, monkeypatch, error):
    home, members = team
    workspace = Path(members[0].workspace)
    (workspace / "sub").mkdir()
    _session(home, workspace, SA, [{**_row("m3", START + timedelta(hours=1)), "cwd": str(workspace / "sub")}])
    _failing_resolve(monkeypatch, str(workspace / "sub"), error)
    by_member = {m.member: m for m in _read(home, members).members}
    assert by_member["B1"].cost_usd is None and by_member["B1"].unknown_reasons == ["session_mismatch"]
    assert by_member["B2"].cost_usd == pytest.approx(_price((500, 0, 1000, 50)))


@pytest.mark.parametrize("error", [OSError, RuntimeError, ValueError])
def test_a_workspace_that_cannot_be_resolved_is_unknown_for_that_member_only(team, monkeypatch, error):
    home, members = team
    _failing_resolve(monkeypatch, members[1].workspace, error)
    by_member = {m.member: m for m in _read(home, members).members}
    assert by_member["B2"].cost_usd is None and by_member["B2"].unknown_reasons == ["binding_unverified"]
    assert by_member["B1"].cost_usd == pytest.approx(_price((1000, 200, 50, 10)) + _price((2000, 0, 0, 100)))


# Each transcript counts once
def test_each_transcript_counts_once_however_the_workspace_is_spelled(team):
    home, members = team
    workspace = members[0].workspace
    members[0] = reader.ClaudeMember("B1", workspace + "/", SA)
    members.append(reader.ClaudeMember("B3", workspace, S2))
    _session(home, workspace, S2,
             [_row("w1", START + timedelta(hours=5), tokens=(100, 0, 0, 1))])
    report = _read(home, members)
    by_member = {m.member: m for m in report.members}
    assert (by_member["B1"].messages, by_member["B3"].messages) == (2, 1)
    assert [u.session_id for u in report.unattributed] == [SZ]


def test_an_earlier_claude_session_in_a_codex_members_workspace_is_unattributed(team):
    home, members = team
    members[1] = reader.ClaudeMember("C1", members[1].workspace, "codex-thread", provider="codex-cli")
    report = _read(home, members)
    assert {m.member: m.unknown_reasons for m in report.members}["C1"] == ["harness_not_read"]
    earlier = {u.session_id: u for u in report.unattributed}[SB]
    assert earlier.messages == 1 and earlier.models[0].cost_usd == pytest.approx(_price((500, 0, 1000, 50)))


def test_unattributed_usage_with_an_unpriced_model_says_why(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S2, [_row("x", START + timedelta(hours=1), model=UNPRICED)])
    report = _read(home, [reader.ClaudeMember("B1", str(workspace), None)])
    (other,) = report.unattributed
    assert other.models[0].cost_usd is None and other.unknown_reasons == [f"price_unknown:{UNPRICED}"]


# The long-context tier is chosen once per request, from its whole prompt
@pytest.mark.parametrize(("tokens", "expected"), [
    ((120_000, 0, 100_000, 100), 120_000 * 6e-6 + 100_000 * 0.6e-6 + 100 * 22.5e-6),  # V1's case: 0.78225
    ((100_000, 50_000, 50_001, 100), 100_000 * 6e-6 + 50_000 * 7.5e-6 + 50_001 * 0.6e-6 + 100 * 22.5e-6),
    ((100_000, 50_000, 50_000, 100), 100_000 * 3e-6 + 50_000 * 3.75e-6 + 50_000 * 0.3e-6 + 100 * 15e-6),
], ids=["cache_read_crosses", "mixed_cache_one_over", "mixed_cache_at_threshold"])
def test_the_long_context_tier_applies_to_every_class_when_the_prompt_is_over_200k(tmp_path, tokens, expected):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S1, [_row("x", START + timedelta(hours=1), model=SONNET_4, tokens=tokens)])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.cost_usd == pytest.approx(expected)


def test_the_tier_is_per_request_not_per_session_total(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, S1, [_row(f"x{n}", START + timedelta(hours=1, minutes=n), model=SONNET_4,
                                        tokens=(150_000, 0, 0, 10)) for n in range(2)])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), S1)]).members
    assert member.cost_usd == pytest.approx(2 * (150_000 * 3e-6 + 10 * 15e-6))


# The dated price table
def test_the_dated_price_is_an_exact_lookup():
    pricing = PricingService()
    assert pricing.dated_price(OPUS) == PricingService.MODEL_PRICING[OPUS]
    assert pricing.dated_price(f"anthropic/{OPUS}") == PricingService.MODEL_PRICING[OPUS]
    assert pricing.dated_price("claude-sonnet-5-5") is None  # get_model_pricing would give claude-sonnet-5's
    assert pricing.get_model_pricing("claude-sonnet-5-5") == PricingService.MODEL_PRICING["claude-sonnet-5"]
    assert pricing.dated_price("claude-fable-5-1") is None
    assert pricing.dated_price("opus-4-8") is None  # the family prefix is part of the id


# #1410: workspaces that share an encoded project folder, and folders that can't be listed
@pytest.fixture
def shared(tmp_path):
    """``project.alpha`` and ``project-alpha`` encode to one project folder; each has its member and session, and
    an earlier one-message session of the dash workspace is in the same folder."""
    home = tmp_path / "home"
    dot, dash = tmp_path / "work" / "project.alpha", tmp_path / "work" / "project-alpha"
    dot.mkdir(parents=True)
    dash.mkdir(parents=True)
    _session(home, dot, SA, [_row("a1", START + timedelta(hours=1))])
    _session(home, dash, SB, [_row("b1", START + timedelta(hours=2), tokens=(500, 0, 0, 5))])
    _session(home, dash, SZ, [_row("z1", START + timedelta(hours=3), tokens=(300, 0, 0, 30))])
    members = [reader.ClaudeMember("B1", str(dot), SA), reader.ClaudeMember("B2", str(dash), SB)]
    return home, dot, dash, members


@pytest.mark.parametrize("order", [1, -1], ids=["dot_first", "dash_first"])
def test_a_shared_folder_attributes_each_transcript_by_its_own_working_directory(shared, order):
    home, _dot, dash, members = shared
    report = _read(home, members[::order])
    by_member = {m.member: m for m in report.members}
    assert by_member["B1"].cost_usd == pytest.approx(_price((1000, 200, 50, 10)))
    assert by_member["B2"].cost_usd == pytest.approx(_price((500, 0, 0, 5)))
    (other,) = report.unattributed
    assert (other.workspace, other.session_id, other.messages, other.unknown_reasons) == (str(dash), SZ, 1, [])
    assert report.unlisted == []


@pytest.mark.parametrize("cwds", [("elsewhere",), ("project.alpha", "project-alpha")],
                         ids=["fits_neither", "fits_both_across_rows"])
def test_a_shared_folder_transcript_that_cannot_be_placed_is_unattributed_with_a_reason(shared, cwds):
    home, dot, _dash, members = shared
    base = dot.parent
    (base / "elsewhere").mkdir()
    rows = [{**_row(f"q{n}", START + timedelta(hours=4, minutes=n)), "sessionId": S2, "cwd": str(base / cwd)}
            for n, cwd in enumerate(cwds)]
    _session(home, dot, S2, rows)
    report = _read(home, members)
    placed = {u.session_id: u for u in report.unattributed}[S2]
    assert (placed.messages, placed.unknown_reasons) == (0, ["session_mismatch"])
    assert {m.member: m.unknown_reasons for m in report.members} == {"B1": [], "B2": []}


def test_a_folder_that_cannot_be_listed_is_a_coverage_gap_and_keeps_known_results(team):
    home, members = team
    folder = home / ".claude" / "projects" / convert_path_to_folder_name(members[0].workspace)
    folder.chmod(0o300)  # files still open by name; the folder can't be listed
    try:
        if os.access(folder, os.R_OK):
            pytest.skip("running with privileges that list any directory")
        report = _read(home, members)
    finally:
        folder.chmod(0o700)
    by_member = {m.member: m for m in report.members}
    assert by_member["B1"].cost_usd == pytest.approx(_price((1000, 200, 50, 10)) + _price((2000, 0, 0, 100)))
    assert [(u.workspaces, u.reason) for u in report.unlisted] == [([members[0].workspace], "native_log_unlisted")]
    assert all(u.session_id != SZ for u in report.unattributed)  # unknown, not shown as no usage


def test_an_empty_or_missing_folder_is_no_gap(tmp_path):
    home = tmp_path / "home"
    empty, missing = tmp_path / "w-empty", tmp_path / "w-missing"
    empty.mkdir()
    missing.mkdir()
    (home / ".claude" / "projects" / convert_path_to_folder_name(str(empty))).mkdir(parents=True)
    report = _read(home, [reader.ClaudeMember("C1", str(empty), "t", provider="codex-cli"),
                          reader.ClaudeMember("C2", str(missing), "t", provider="codex-cli")])
    assert (report.unattributed, report.unlisted) == ([], [])


def test_only_session_files_in_a_folder_are_read(team):
    """A subdirectory or another file beside the transcripts is not a session (the folder holds more than sessions)."""
    home, members = team
    folder = home / ".claude" / "projects" / convert_path_to_folder_name(members[0].workspace)
    (folder / SB).mkdir()
    (folder / "notes.txt").write_text("not a transcript\n")
    report = _read(home, members)
    assert [u.session_id for u in report.unattributed] == [SZ]


def test_a_bound_session_in_a_shared_folder_is_checked_against_its_own_workspace_only(shared):
    """B1's recorded session whose rows ran in the other workspace of the folder is not B1's to be charged."""
    home, dot, dash, members = shared
    _session(home, dot, SA, [{**_row("a1", START + timedelta(hours=1)), "cwd": str(dash)}])
    report = _read(home, members)
    by_member = {m.member: m for m in report.members}
    assert by_member["B1"].cost_usd is None and by_member["B1"].unknown_reasons == ["session_mismatch"]
    # The rejected binding doesn't hide the transcript: it is classified once, by where its rows ran (V1 6088836613).
    rejected = [u for u in report.unattributed if u.session_id == SA]
    assert [(u.workspace, u.messages, u.unknown_reasons) for u in rejected] == [(str(dash), 1, [])]


def test_a_folder_that_vanishes_after_a_member_read_is_a_coverage_gap(team, monkeypatch):
    """Its bound transcript was just read, so a listing that finds no folder is a gap, not "never written"."""
    home, members = team
    folder = home / ".claude" / "projects" / convert_path_to_folder_name(members[0].workspace)
    listdir = os.listdir

    def moved(path):
        if Path(path) == folder:
            raise FileNotFoundError(path)
        return listdir(path)

    monkeypatch.setattr(reader.os, "listdir", moved)
    report = _read(home, members)
    by_member = {m.member: m for m in report.members}
    assert by_member["B1"].cost_usd == pytest.approx(_price((1000, 200, 50, 10)) + _price((2000, 0, 0, 100)))
    assert [(u.workspaces, u.reason) for u in report.unlisted] == [([members[0].workspace], "native_log_unlisted")]

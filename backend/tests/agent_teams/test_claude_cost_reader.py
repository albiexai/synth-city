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

import pytest

from app.services import claude_cost_reader as reader
from app.services.pricing_service import PricingService
from app.utils.path_utils import convert_path_to_folder_name

START = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
END = START + timedelta(days=1)
OPUS = "claude-opus-4-8"  # priced in the dated table: 5 / 25 / 6.25 / 0.50 USD per million tokens
UNPRICED = "claude-opus-5-5"


def _row(message_id, at, *, model=OPUS, tokens=(1000, 200, 50, 10), session="s", block=0):
    inp, cache_creation, cache_read, out = tokens
    return {"type": "assistant", "sessionId": session, "timestamp": at.isoformat().replace("+00:00", "Z"),
            "message": {"id": message_id, "model": model, "content": [{"type": "text", "text": f"block {block}"}],
                        "usage": {"input_tokens": inp, "cache_creation_input_tokens": cache_creation,
                                  "cache_read_input_tokens": cache_read, "output_tokens": out}}}


def _session(home, workspace, session_id, rows, *, mode=0o600):
    folder = home / ".claude" / "projects" / convert_path_to_folder_name(str(workspace))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{session_id}.jsonl"
    lines = [json.dumps({"type": "user", "sessionId": session_id, "timestamp": START.isoformat()})]
    lines += [json.dumps(row) if isinstance(row, dict) else row for row in rows]
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
    _session(home, a, "aaaa", [
        _row("m1", START + timedelta(hours=1), session="aaaa"),
        _row("m1", START + timedelta(hours=1, seconds=1), session="aaaa", block=1),  # the same message's next block
        _row("m2", START + timedelta(hours=2), session="aaaa", tokens=(2000, 0, 0, 100)),
        _row("m0", START - timedelta(minutes=5), session="aaaa", tokens=(9999, 9999, 9999, 9999)),  # before
    ])
    _session(home, b, "bbbb", [
        _row("n1", START + timedelta(hours=3), session="bbbb", tokens=(500, 0, 1000, 50)),
        _row("n2", END + timedelta(minutes=1), session="bbbb", tokens=(7777, 0, 0, 7777)),  # after the window
    ])
    _session(home, a, "zzzz", [_row("u1", START + timedelta(hours=4), session="zzzz", tokens=(300, 0, 0, 30))])
    members = [reader.ClaudeMember("B1", str(a), "aaaa"), reader.ClaudeMember("B2", str(b), "bbbb")]
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
    assert (other.session_id, other.messages, other.models[0].input) == ("zzzz", 1, 300)
    assert all(m.messages <= 2 for m in report.members)  # and it isn't counted as B1's


def test_a_member_without_a_recorded_session_is_unknown(team):
    home, members = team
    members[0] = reader.ClaudeMember("B1", members[0].workspace, None)
    by_member = {m.member: m for m in _read(home, members).members}
    assert by_member["B1"].cost_usd is None and by_member["B1"].unknown_reasons == ["binding_unverified"]
    assert {u.session_id for u in _read(home, members).unattributed} == {"aaaa", "zzzz"}


def test_repeated_rows_of_one_message_count_once_at_the_first_rows_time(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, "s1", [
        _row("x", START - timedelta(seconds=1), session="s1"),  # its first row is before the window
        _row("x", START + timedelta(seconds=1), session="s1", block=1),
    ])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), "s1")]).members
    assert member.messages == 0 and member.cost_usd == 0.0


def test_an_unpriced_model_shows_its_tokens_with_cost_unknown(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, "s1", [_row("x", START + timedelta(hours=1), model=UNPRICED, session="s1")])
    report = _read(home, [reader.ClaudeMember("B1", str(workspace), "s1")])
    (member,) = report.members
    assert member.cost_usd is None and member.unknown_reasons == [f"price_unknown:{UNPRICED}"]
    assert (member.models[0].model, member.models[0].input, member.models[0].cost_usd) == (UNPRICED, 1000, None)
    assert (report.members_priced, report.members_total) == (0, 1)


@pytest.mark.parametrize("damage", ["symlink", "other_writable", "group_writable", "oversized", "unreadable"])
def test_a_file_that_cannot_be_read_safely_is_unknown(tmp_path, damage):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    path = _session(home, workspace, "s1", [_row("x", START + timedelta(hours=1), session="s1")])
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
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), "s1")], **kwargs).members
    assert member.cost_usd is None and member.unknown_reasons == ["native_log_unreadable"]


def test_a_file_owned_by_another_user_is_unknown(tmp_path):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, "s1", [_row("x", START + timedelta(hours=1), session="s1")])
    report = reader.read_claude_usage(START, END, [reader.ClaudeMember("B1", str(workspace), "s1")],
                                      home=home, uid=os.getuid() + 1)
    assert report.members[0].unknown_reasons == ["native_log_unreadable"]


@pytest.mark.parametrize("bad", ['{"type": "assistant", "message": {"id": "x"}}', "not json",
                                 ('{"type": "assistant", "timestamp": "2026-11-20T10:00:00Z", "message": '
                                  '{"id": "x", "model": "claude-opus-4-8", "usage": {"input_tokens": "many"}}}'),
                                 ('{"type": "assistant", "timestamp": "2026-11-20T10:00:00Z", "message": '
                                  '{"id": "x", "model": "claude-opus-4-8"}}')],
                         ids=["no_usage", "not_json", "bad_count", "usage_missing"])
def test_a_row_in_an_unknown_format_is_unknown(tmp_path, bad):
    home, workspace = tmp_path / "home", tmp_path / "w"
    workspace.mkdir()
    _session(home, workspace, "s1", [_row("ok", START + timedelta(hours=1), session="s1"), bad])
    (member,) = _read(home, [reader.ClaudeMember("B1", str(workspace), "s1")]).members
    assert member.cost_usd is None and member.unknown_reasons == ["format_unknown"]


def test_a_member_whose_session_file_is_missing_is_unknown(tmp_path):
    workspace = tmp_path / "w"
    workspace.mkdir()
    (member,) = _read(tmp_path / "home", [reader.ClaudeMember("B1", str(workspace), "absent")]).members
    assert member.cost_usd is None and member.unknown_reasons == ["no_native_session"]


def test_a_codex_member_is_not_read_here(tmp_path):
    (member,) = _read(tmp_path / "home", [reader.ClaudeMember("C1", str(tmp_path), "s", provider="codex-cli")]).members
    assert member.cost_usd is None and member.unknown_reasons == ["harness_not_read"]


# The dated price table
def test_the_dated_price_is_an_exact_lookup():
    pricing = PricingService()
    assert pricing.dated_price(OPUS) == PricingService.MODEL_PRICING[OPUS]
    assert pricing.dated_price(f"anthropic/{OPUS}") == PricingService.MODEL_PRICING[OPUS]
    assert pricing.dated_price("claude-sonnet-5-5") is None  # get_model_pricing would give claude-sonnet-5's
    assert pricing.get_model_pricing("claude-sonnet-5-5") == PricingService.MODEL_PRICING["claude-sonnet-5"]
    assert pricing.dated_price("claude-fable-5-1") is None
    assert pricing.dated_price("opus-4-8") is None  # the family prefix is part of the id

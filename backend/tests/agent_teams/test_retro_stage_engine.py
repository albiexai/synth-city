"""#1245 split 1: the "where the time went" stage engine (design §3; the Supervisor's split and answers, 2026-11-27).

One attempt's window and its typed intervals become per-stage and per-group seconds. At each instant the highest
group present takes the time (waiting on someone, then process, then active), and concurrent stages in that group
share it equally, so the shares reconcile. Time no interval covers is unaccounted, never active. Each stage's own
wall time and the overlap are reported beside its share.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.retro_stage_engine import AttemptWindow, StageInterval, breakdown

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


def _at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _window(end: float = 60, label: str | None = None) -> AttemptWindow:
    return AttemptWindow(start=T0, end=_at(end), start_label=label)


def _iv(group, stage, actor, start, end):
    return StageInterval(group=group, stage=stage, actor=actor, start=_at(start), end=_at(end))


def _share(result, group, stage, actor):
    (row,) = [s for s in result.stages if (s.group, s.stage, s.actor) == (group, stage, actor)]
    return row


# The acceptance: totals reconcile with item time
def test_groups_and_unaccounted_add_up_to_the_window():
    result = breakdown(_window(60), [
        _iv("active", "planning", "B3", 0, 10),
        _iv("waiting", "decision", "leader", 10, 20),
        _iv("process", "required_check", "e2e", 25, 40),
        _iv("active", "building", "B3", 20, 50),
    ])
    assert result.total_seconds == 3600
    assert result.groups == {"waiting": 600, "process": 900, "active": 1500}
    assert result.unaccounted_seconds == 600  # 50 to 60 minutes: nothing recorded
    assert sum(result.groups.values()) + result.unaccounted_seconds == result.total_seconds
    assert sum(s.share_seconds for s in result.stages) == sum(result.groups.values())


def test_an_open_attempt_ends_at_its_cutoff():
    """An attempt still open is measured to ``as_of``: an interval running past it is clipped, not dropped."""
    result = breakdown(_window(30), [_iv("process", "review", "V1", 20, 90)])
    assert (result.total_seconds, result.groups["process"], result.unaccounted_seconds) == (1800, 600, 1200)


# Overlap: precedence between groups, equal shares within one
def test_two_parallel_checks_share_their_time_and_report_their_own_wall_time():
    result = breakdown(_window(10), [_iv("process", "required_check", "e2e", 0, 10),
                                     _iv("process", "required_check", "backend", 0, 10)])
    assert result.groups["process"] == 600  # 10 minutes, not 20
    for check in ("e2e", "backend"):
        row = _share(result, "process", "required_check", check)
        assert (row.share_seconds, row.wall_seconds) == (300, 600)
    assert result.overlap_seconds == 600


def test_the_overlap_is_the_time_covered_by_more_than_one_stage():
    """Three checks over the same 10 minutes overlap for 10 minutes, not 20 (V1 review 6088363261)."""
    result = breakdown(_window(15), [_iv("process", "required_check", name, 0, 10) for name in ("a", "b", "c")]
                       + [_iv("active", "building", "B3", 10, 15)])
    assert (result.overlap_seconds, result.groups["process"], result.groups["active"]) == (600, 600, 300)
    assert [s.share_seconds for s in result.stages if s.stage == "required_check"] == [200, 200, 200]


@pytest.mark.parametrize(("winner", "loser"), [("waiting", "process"), ("process", "active"), ("waiting", "active")])
def test_a_higher_group_takes_the_instant(winner, loser):
    result = breakdown(_window(10), [_iv(loser, "low", "x", 0, 10), _iv(winner, "high", "y", 0, 10)])
    assert (result.groups[winner], result.groups[loser]) == (600, 0)
    assert (_share(result, winner, "high", "y").share_seconds, _share(result, loser, "low", "x").share_seconds) == (
        600, 0)
    assert _share(result, loser, "low", "x").wall_seconds == 600  # its own time stays visible
    assert result.overlap_seconds == 600


def test_an_overlapping_effort_wait_and_mail_request_count_their_time_once():
    """The Supervisor's added acceptance (2026-11-26): each overlapping instant is counted once."""
    result = breakdown(_window(30), [_iv("waiting", "effort_extension", "leader", 0, 20),
                                     _iv("waiting", "mail_request", "supervisor", 10, 30)])
    assert result.groups["waiting"] == 1800
    assert (_share(result, "waiting", "effort_extension", "leader").share_seconds,
            _share(result, "waiting", "mail_request", "supervisor").share_seconds) == (900, 900)


def test_process_splits_per_check_and_per_verifier_across_repositories_with_different_check_names():
    """Two repositories' check names stay separate keys; review is keyed by verifier."""
    result = breakdown(_window(40), [
        _iv("process", "required_check", "o/a1:e2e", 0, 10),
        _iv("process", "required_check", "o/b1:ci/build", 10, 20),
        _iv("process", "review", "V1", 20, 30),
        _iv("process", "review", "V2", 30, 40),
    ])
    assert {(s.stage, s.actor): s.share_seconds for s in result.stages} == {
        ("required_check", "o/a1:e2e"): 600, ("required_check", "o/b1:ci/build"): 600,
        ("review", "V1"): 600, ("review", "V2"): 600}


def test_overlapping_intervals_of_one_stage_count_their_time_once():
    """Two overlapping waits on the Leader are one stage: 15 minutes, not 20, and nothing overlaps another stage."""
    result = breakdown(_window(15), [_iv("waiting", "decision", "leader", 0, 10),
                                     _iv("waiting", "decision", "leader", 5, 15)])
    row = _share(result, "waiting", "decision", "leader")
    assert (row.share_seconds, row.wall_seconds, result.overlap_seconds) == (900, 900, 0)


def test_one_stage_in_several_intervals_is_one_row():
    result = breakdown(_window(30), [_iv("waiting", "decision", "leader", 0, 5),
                                     _iv("waiting", "decision", "leader", 10, 15)])
    assert [(s.share_seconds, s.wall_seconds) for s in result.stages] == [(600, 600)]


# Unaccounted, never active
def test_time_no_interval_covers_is_unaccounted_never_active():
    result = breakdown(_window(60), [_iv("waiting", "decision", "leader", 10, 20)])
    assert (result.groups["active"], result.unaccounted_seconds) == (0, 3000)


def test_an_attempt_with_no_intervals_is_all_unaccounted():
    result = breakdown(_window(15), [])
    assert (result.groups, result.unaccounted_seconds, result.stages) == (
        {"waiting": 0, "process": 0, "active": 0}, 900, [])


# The window and its intervals
def test_intervals_outside_the_window_are_clipped():
    result = breakdown(_window(20), [_iv("active", "building", "B3", -30, 5), _iv("active", "building", "B3", 25, 40)])
    row = _share(result, "active", "building", "B3")
    assert (row.share_seconds, row.wall_seconds, result.unaccounted_seconds) == (300, 300, 900)


@pytest.mark.parametrize("bad", [
    lambda: _iv("active", "building", "B3", 10, 5),
    lambda: _iv("elsewhere", "building", "B3", 0, 5),
], ids=["reversed", "unknown_group"])
def test_an_invalid_interval_is_refused(bad):
    with pytest.raises(ValueError):
        breakdown(_window(20), [bad()])


def test_a_reversed_window_is_refused():
    with pytest.raises(ValueError):
        breakdown(AttemptWindow(start=_at(10), end=_at(5)), [])


def test_the_start_label_is_carried_through():
    """Q1: waiting for dispatch before the first attempt starts at campaign membership, and says so."""
    label = "from campaign membership; ready time not recorded"
    assert breakdown(_window(10, label), []).start_label == label

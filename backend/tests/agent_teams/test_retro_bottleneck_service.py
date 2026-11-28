from datetime import UTC, datetime, timedelta

import pytest

from app.services.retro_bottleneck_service import RankingAttempt, build_bottlenecks
from app.services.retro_stage_engine import AttemptWindow, StageInterval

START = datetime(2026, 11, 26)
AS_OF = START + timedelta(hours=10)
FULL = {"waiting": "full", "process": "full"}


def at(minutes):
    return START + timedelta(minutes=minutes)


def attempt(number, *stages, start=0, end=600):
    return RankingAttempt(("albiexai/repo", number), AttemptWindow(at(start), at(end)), [
        StageInterval(group, stage, actor, at(left), at(right)) for group, stage, actor, left, right in stages
    ])


def rankings(attempts, coverage=FULL, reasons=None):
    return build_bottlenecks(attempts, start=START, as_of=AS_OF, group_coverage=coverage,
                             unknown_reasons=reasons or [])


def test_rankings_clip_to_labelled_windows_and_keep_checks_and_verifiers_separate():
    source = [attempt(issue,
        ("waiting", "decision", "leader", 20, 400),
        ("process", "required_check", "e2e", 380, 480),
        ("process", "required_check", "backend", 400, 440),
        ("process", "review", "V2", 480, 510),
        ("process", "review", "V1", 510, 530),
        ("active", "building", "builder", 530, 600)) for issue in range(5)]
    result = rankings(source)
    whole, recent = result.whole_campaign, result.last_four_hours
    assert (whole.label, whole.started_at, whole.as_of) == (
        "Whole campaign", START.replace(tzinfo=UTC), AS_OF.replace(tzinfo=UTC))
    assert (recent.label, recent.started_at, recent.as_of) == (
        "Last 4 hours", at(360).replace(tzinfo=UTC), AS_OF.replace(tzinfo=UTC))
    assert whole.largest_observed.actor == "leader"
    assert recent.largest_observed.actor == "e2e"
    assert recent.credited_seconds == 5 * (40 + 80 + 30 + 20) * 60
    assert [(stage.actor, stage.credited_seconds) for stage in recent.stages] == [
        ("e2e", 5 * 60 * 60), ("leader", 5 * 40 * 60), ("V2", 5 * 30 * 60),
        ("backend", 5 * 20 * 60), ("V1", 5 * 20 * 60),
    ]
    assert all(stage.items == 5 and not stage.small_sample for stage in recent.stages)


def test_includes_exact_thresholds_and_counts_each_issue_once_across_attempts():
    source = [attempt(issue, ("waiting", "decision", "leader", 500, 510),
                      ("process", "required_check", "e2e", 510, 600)) for issue in range(5)]
    result = rankings(source).last_four_hours
    decision = result.stages[1]
    assert (decision.actor, decision.items, decision.share, decision.median_seconds) == (
        "leader", 5, 0.10, 10 * 60)

    retries = [attempt(9, ("process", "review", "V1", minute, minute + 5), start=minute, end=minute + 5)
               for minute in range(500, 550, 10)]
    retry_result = rankings(retries).last_four_hours
    assert retry_result.state == "below_thresholds" and retry_result.stages == []
    assert retry_result.largest_observed.items == 1
    assert retry_result.largest_observed.small_sample
    assert retry_result.largest_observed.median_seconds == 25 * 60


def test_largest_observed_and_deterministic_ties_are_stable_with_shuffled_input():
    source = [attempt(1, ("process", "required_check", "z", 500, 510)),
              attempt(2, ("process", "required_check", "a", 500, 510))]
    result = rankings(source).last_four_hours
    assert result == rankings(list(reversed(source))).last_four_hours
    assert result.state == "below_thresholds"
    assert result.largest_observed.actor == "a"


def test_excludes_time_taken_by_the_higher_group_from_process_sample_count():
    source = [attempt(issue, ("process", "review", "V1", 500, 510),
                      ("waiting", "decision", "leader", 500, 510)) for issue in range(5)]
    result = rankings(source).last_four_hours
    assert len(result.stages) == 1
    assert (result.stages[0].actor, result.stages[0].share) == ("leader", 1)


def test_incomplete_evidence_has_unknown_or_lower_bound_results():
    source = [attempt(1, ("process", "review", "V1", 500, 510))]
    result = rankings(source, {"waiting": "partial", "process": "full"}, ["late_wait_recording"]).last_four_hours
    assert (result.coverage, result.state, result.credited_seconds) == ("partial", "unknown", 600)
    assert result.largest_observed.actor == "V1"
    assert result.largest_observed.coverage == "partial"
    absent = rankings(source, {"waiting": "partial", "process": "unavailable"},
                      ["missing_process_recording"]).last_four_hours
    assert (absent.state, absent.items, absent.credited_seconds, absent.largest_observed) == (
        "unknown", None, None, None)
    unobserved = rankings([], {"waiting": "unavailable", "process": "unavailable"}).last_four_hours
    assert (unobserved.coverage, unobserved.credited_seconds, unobserved.unknown_reasons) == (
        "unavailable", None, ["no_attempts_in_window"])


def test_unidentified_actor_contributes_to_total_without_becoming_a_ranked_actor():
    source = [attempt(issue, ("waiting", "decision", "unknown", 500, 595),
                      ("process", "review", "V1", 595, 600)) for issue in range(5)]
    result = rankings(source).last_four_hours
    assert (result.coverage, result.state, result.credited_seconds) == ("partial", "unknown", 5 * 6000)
    assert result.stages == []
    assert result.unknown_reasons == ["actor_unavailable"]
    assert result.largest_observed.actor == "V1"
    assert result.largest_observed.share == 0.05


def test_previous_top_comparison_respects_first_visit_and_incomplete_coverage():
    source = [attempt(issue, ("process", "review", "V1", 500, 510)) for issue in range(5)]
    result = rankings(source)
    top_key = result.whole_campaign.stages[0].key
    assert result.with_previous_top(None).new_since_last_visit is None
    assert result.with_previous_top(top_key).new_since_last_visit is False
    assert result.with_previous_top("").new_since_last_visit is True
    assert result.with_previous_top("other").new_since_last_visit is True
    assert result.new_since_last_visit is None  # caller-specific marker never modifies the snapshot
    assert rankings(source[:1]).with_previous_top(top_key).new_since_last_visit is False
    assert rankings(source, reasons=["membership_unknown"]).with_previous_top(top_key).new_since_last_visit is None


@pytest.mark.parametrize("elapsed", [0, 120])
def test_recent_window_never_starts_before_campaign(elapsed):
    result = build_bottlenecks([], start=START, as_of=at(elapsed), group_coverage=FULL, unknown_reasons=[])
    assert result.whole_campaign.started_at == result.last_four_hours.started_at == START.replace(tzinfo=UTC)

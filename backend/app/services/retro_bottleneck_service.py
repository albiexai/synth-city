"""Rank recorded waiting and process time for a campaign (#1257)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from math import fsum, isclose
from statistics import median
from typing import Literal

from pydantic import BaseModel

from app.services.retro_stage_engine import AttemptStages, AttemptWindow, StageInterval, breakdown

MINIMUM_SHARE = 0.10
MINIMUM_ITEMS = 5
PANEL_WINDOW = timedelta(hours=4)
RANKED_GROUPS = ("waiting", "process")
Coverage = Literal["full", "partial", "unavailable"]
StageKey = tuple[str, str, str]
IssueKey = tuple[str, int]  # canonical repository and issue number, shared across retries


@dataclass(frozen=True)
class RankingAttempt:
    issue: IssueKey
    window: AttemptWindow
    intervals: list[StageInterval]
    full_breakdown: AttemptStages | None = None


class BottleneckStage(BaseModel):
    key: str
    group: Literal["waiting", "process"]
    stage: str
    actor: str
    credited_seconds: float
    share: float
    items: int
    median_seconds: float
    small_sample: bool
    coverage: Coverage


class BottleneckWindow(BaseModel):
    window: Literal["whole_campaign", "last_four_hours"]
    label: str
    started_at: datetime
    as_of: datetime
    coverage: Coverage
    unknown_reasons: list[str]
    items: int | None
    credited_seconds: float | None
    state: Literal["ranked", "below_thresholds", "unknown"]
    stages: list[BottleneckStage]
    largest_observed: BottleneckStage | None


class CampaignBottlenecks(BaseModel):
    whole_campaign: BottleneckWindow
    last_four_hours: BottleneckWindow
    minimum_share: float = MINIMUM_SHARE
    minimum_items: int = MINIMUM_ITEMS
    new_since_last_visit: bool | None = None

    def with_previous_top(self, previous_top: str | None) -> CampaignBottlenecks:
        """An empty key means a prior complete visit with no qualifying stage."""
        current = self.whole_campaign
        changed = None
        if previous_top is not None and current.coverage == "full":
            changed = bool(current.stages and current.stages[0].key != previous_top)
        return self.model_copy(update={"new_since_last_visit": changed})


class CampaignBottleneckResponse(BaseModel):
    campaign_id: int
    source: Literal["live", "frozen", "unavailable"]
    bottlenecks: CampaignBottlenecks


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def rank_window(attempts: list[RankingAttempt], *, start: datetime, as_of: datetime,
                window: Literal["whole_campaign", "last_four_hours"],
                group_coverage: dict[str, Coverage], unknown_reasons: list[str]) -> BottleneckWindow:
    """Clip before applying the shared stage credits. Samples are distinct issues with credited time."""
    label = "Whole campaign" if window == "whole_campaign" else "Last 4 hours"
    included_groups = {group for group in RANKED_GROUPS if group_coverage[group] != "unavailable"}
    by_stage: dict[StageKey, dict[IssueKey, list[float]]] = defaultdict(lambda: defaultdict(list))
    observed_items = set()
    for attempt in attempts:
        left, right = max(start, attempt.window.start), min(as_of, attempt.window.end)
        if left >= right:
            continue
        observed_items.add(attempt.issue)
        result = attempt.full_breakdown if (left, right) == (attempt.window.start, attempt.window.end) else None
        if result is None:
            result = breakdown(AttemptWindow(left, right), attempt.intervals)
        for row in result.stages:
            if row.group in included_groups and row.share_seconds > 0:
                by_stage[row.group, row.stage, row.actor][attempt.issue].append(row.share_seconds)

    samples = {key: [fsum(parts) for parts in issues.values()] for key, issues in by_stage.items()}
    totals = {key: fsum(times) for key, times in samples.items()}
    denominator = fsum(totals.values())
    reasons = set(unknown_reasons)
    named = [key for key in totals if key[2] != "unknown"]
    if len(named) != len(totals):
        reasons.add("actor_unavailable")
    coverage: Coverage = "unavailable"
    if not reasons and all(group_coverage[group] == "full" for group in RANKED_GROUPS):
        coverage = "full"
    elif observed_items and included_groups:
        coverage = "partial"
    if not observed_items and coverage != "full":
        reasons.add("no_attempts_in_window")
    # Partial history needs recorded evidence before it can report a number.
    known_time = coverage == "full" or bool(totals)
    ordered = sorted(named, key=lambda key: (-totals[key], RANKED_GROUPS.index(key[0]), key[1], key[2]))
    rows = []
    for key in ordered:
        group, stage, actor = key
        times = samples[key]
        rows.append(BottleneckStage(
            key=sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest(),
            group=group, stage=stage, actor=actor, credited_seconds=totals[key],
            share=totals[key] / denominator, items=len(times), median_seconds=median(times),
            small_sample=len(times) < MINIMUM_ITEMS, coverage=coverage))
    ranked = [row for row in rows if row.items >= MINIMUM_ITEMS and (
        row.share >= MINIMUM_SHARE or isclose(row.share, MINIMUM_SHARE, rel_tol=1e-12))]
    return BottleneckWindow(
        window=window, label=label, started_at=_utc(start), as_of=_utc(as_of), coverage=coverage,
        unknown_reasons=sorted(reasons),
        items=len(observed_items) if known_time else None, credited_seconds=denominator if known_time else None,
        state="ranked" if ranked else ("below_thresholds" if coverage == "full" else "unknown"),
        stages=ranked, largest_observed=rows[0] if rows else None)


def build_bottlenecks(attempts: list[RankingAttempt], *, start: datetime, as_of: datetime,
                      group_coverage: dict[str, Coverage], unknown_reasons: list[str]) -> CampaignBottlenecks:
    """Recalculate the recent window's stage credits after clipping its intervals."""
    start = min(start, as_of)
    return CampaignBottlenecks(
        whole_campaign=rank_window(attempts, start=start, as_of=as_of, window="whole_campaign",
                                   group_coverage=group_coverage, unknown_reasons=unknown_reasons),
        last_four_hours=rank_window(attempts, start=max(start, as_of - PANEL_WINDOW), as_of=as_of,
                                    window="last_four_hours", group_coverage=group_coverage,
                                    unknown_reasons=unknown_reasons))

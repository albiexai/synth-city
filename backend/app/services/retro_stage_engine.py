"""#1245 split 1: the "where the time went" stage engine (design §3).

A pure function, with no database: one attempt's window and its typed intervals become per-stage and per-group
seconds that reconcile with the attempt's time. The collectors that build the intervals are #1417 and #1418, and the
snapshot section that shows them is #1419.

The rules are the Supervisor's design answers (2026-11-27):
- At each instant the highest group present takes the time: waiting on someone, then process, then active.
  Concurrent stages in that group share the instant equally, so the shares add up to the group's time.
- Active time comes only from recorded intervals. Time no interval covers is unaccounted, never active.
- Each stage's own wall time and the overlap (time covered more than once) are reported beside the shares.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise

GROUPS = ("waiting", "process", "active")  # precedence, highest first


@dataclass(frozen=True)
class AttemptWindow:
    start: datetime
    end: datetime  # the merge delivery, or ``as_of`` while the attempt is open
    start_label: str | None = None  # e.g. "from campaign membership; ready time not recorded"


@dataclass(frozen=True)
class StageInterval:
    group: str
    stage: str  # e.g. "decision", "required_check", "review", "planning"
    actor: str  # who had to act: a role, a check name or a verifier
    start: datetime
    end: datetime


@dataclass
class StageShare:
    group: str
    stage: str
    actor: str
    share_seconds: float = 0.0  # this stage's part of the attempt's time
    wall_seconds: float = 0.0  # its own time in the window, whatever else ran


@dataclass
class AttemptStages:
    total_seconds: float
    start_label: str | None
    groups: dict[str, float] = field(default_factory=dict)
    stages: list[StageShare] = field(default_factory=list)
    unaccounted_seconds: float = 0.0
    overlap_seconds: float = 0.0  # time covered by more than one stage


def breakdown(window: AttemptWindow, intervals: list[StageInterval]) -> AttemptStages:
    """Split the window over its intervals. The groups plus the unaccounted time equal the window."""
    if window.end < window.start:
        raise ValueError("an attempt window ends before it starts")
    clipped: list[tuple[datetime, datetime, tuple[str, str, str]]] = []
    for interval in intervals:
        if interval.group not in GROUPS:
            raise ValueError(f"unknown stage group: {interval.group}")
        if interval.end < interval.start:
            raise ValueError("a stage interval ends before it starts")
        start, end = max(interval.start, window.start), min(interval.end, window.end)
        if start < end:
            clipped.append((start, end, (interval.group, interval.stage, interval.actor)))
    keys = sorted({key for _, _, key in clipped}, key=lambda key: (GROUPS.index(key[0]), key[1], key[2]))
    shares = {key: StageShare(*key) for key in keys}
    groups = dict.fromkeys(GROUPS, 0.0)
    unaccounted = covered = 0.0
    edges = sorted({window.start, window.end, *(t for start, end, _ in clipped for t in (start, end))})
    for left, right in pairwise(edges):
        seconds = (right - left).total_seconds()
        present = {key for start, end, key in clipped if start <= left and right <= end}  # one stage counts once
        for key in present:
            shares[key].wall_seconds += seconds
        if not present:
            unaccounted += seconds
            continue
        covered += seconds
        top = min(GROUPS.index(key[0]) for key in present)
        sharing = [key for key in present if GROUPS.index(key[0]) == top]
        groups[GROUPS[top]] += seconds
        for key in sharing:
            shares[key].share_seconds += seconds / len(sharing)
    return AttemptStages(
        total_seconds=(window.end - window.start).total_seconds(), start_label=window.start_label, groups=groups,
        stages=[shares[key] for key in keys], unaccounted_seconds=unaccounted,
        overlap_seconds=sum(share.wall_seconds for share in shares.values()) - covered)

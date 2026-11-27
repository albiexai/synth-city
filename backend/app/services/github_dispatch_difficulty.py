"""One capability ordering for issue routing and its final database fence."""
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import case, func, or_

from app.models.database import GithubWorkItem
from app.models.schemas import IssueDifficultyProvenance


_RANK = {"low": 1, "medium": 2, "high": 3}


def difficulty_from_labels(labels: Iterable[str]) -> str | None:
    names = set(labels)
    return next((tier for tier in reversed(_RANK) if f"difficulty:{tier}" in names), None)


def difficulty_labels(labels: Iterable[str]) -> set[str]:
    return set(labels).intersection(f"difficulty:{tier}" for tier in _RANK)


def observe_issue_difficulty(
    item: GithubWorkItem, labels: Iterable[str], *, read_started_at: datetime | None = None,
) -> None:
    observed = difficulty_labels(labels)
    proof = item.issue_difficulty_provenance
    # A poll can read GitHub before the Factory write and enter its DB transaction after it.
    if proof and read_started_at and proof.get("confirmed_at", "") > read_started_at.isoformat():
        return
    if proof and observed != {f"difficulty:{proof['difficulty']}"}:
        item.issue_difficulty_provenance = None
    item.issue_difficulty = difficulty_from_labels(observed)


def project_issue_difficulty(item: GithubWorkItem) -> IssueDifficultyProvenance | None:
    if item.issue_difficulty not in _RANK:
        return None
    proof = item.issue_difficulty_provenance
    if proof and proof["difficulty"] == item.issue_difficulty:
        return IssueDifficultyProvenance.model_validate(proof)
    return IssueDifficultyProvenance(difficulty=item.issue_difficulty, source="github", actor_reference=None)


def tier_allows_difficulty(tier: str | None, difficulty: str | None) -> bool:
    if difficulty is None:
        return True
    return _RANK.get("high" if tier is None else tier, 0) >= _RANK.get(difficulty, 4)


def tier_allows_difficulty_sql(tier, difficulty):
    return or_(
        difficulty.is_(None),
        case(_RANK, value=func.coalesce(tier, "high"), else_=0)
        >= case(_RANK, value=difficulty, else_=4),
    )

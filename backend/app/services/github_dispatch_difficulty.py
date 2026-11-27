"""One capability ordering for issue routing and its final database fence."""
from collections.abc import Iterable

from sqlalchemy import case, func, or_


_RANK = {"low": 1, "medium": 2, "high": 3}


def difficulty_from_labels(labels: Iterable[str]) -> str | None:
    names = set(labels)
    return next((tier for tier in reversed(_RANK) if f"difficulty:{tier}" in names), None)


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

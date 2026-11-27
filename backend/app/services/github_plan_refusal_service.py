"""Store and project plan-limit refusals without creating approval authority."""
from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import ScalarSelect

from app.models.database import GithubPlanRefusal, GithubWorkItem
from app.services.github_effort_limit_service import plan_split_suggestion


MAX_REFUSALS_PER_ATTEMPT = 20
PLAN_LIMIT_REFUSAL = "plan_estimate_exceeds_limit"


def _attempt_refusals(work_item_id: int, dispatch_nonce: str | None):
    return (
        GithubPlanRefusal.work_item_id == work_item_id,
        GithubPlanRefusal.dispatch_nonce == dispatch_nonce,
    )


async def record_plan_refusal(
    db: AsyncSession,
    item: GithubWorkItem,
    estimate_minutes: float,
    limit_minutes: int,
    *,
    already_admitted: bool,
) -> None:
    """Append under the caller's item writer guard, with no authority side effects."""
    db.add(GithubPlanRefusal(
        work_item_id=item.id,
        dispatch_nonce=item.dispatch_nonce,
        estimate_minutes=estimate_minutes,
        limit_minutes=limit_minutes,
        code=PLAN_LIMIT_REFUSAL,
        split_suggestion=plan_split_suggestion(limit_minutes),
        cleared=already_admitted,
    ))
    await db.flush()
    older = select(GithubPlanRefusal.id).where(*_attempt_refusals(item.id, item.dispatch_nonce)).order_by(
        GithubPlanRefusal.id.desc()
    ).offset(MAX_REFUSALS_PER_ATTEMPT)
    await db.execute(delete(GithubPlanRefusal).where(GithubPlanRefusal.id.in_(older)).execution_options(
        synchronize_session=False
    ))


async def clear_plan_refusal(db: AsyncSession, work_item_id: int, dispatch_nonce: str) -> None:
    await db.execute(update(GithubPlanRefusal).where(
        *_attempt_refusals(work_item_id, dispatch_nonce), GithubPlanRefusal.cleared.is_(False),
    ).values(cleared=True).execution_options(synchronize_session=False))


def current_plan_refusal_id() -> ScalarSelect[int | None]:
    """Correlate the latest uncleared refusal with the work item's current attempt."""
    return (
        select(func.max(GithubPlanRefusal.id))
        .where(
            *_attempt_refusals(GithubWorkItem.id, GithubWorkItem.dispatch_nonce),
            GithubPlanRefusal.cleared.is_(False),
        )
        .correlate(GithubWorkItem)
        .scalar_subquery()
    )

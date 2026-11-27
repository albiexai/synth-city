"""Store and project plan-limit refusals without creating approval authority."""
from sqlalchemy import delete, func, select, update

from app.models.database import GithubPlanRefusal, GithubWorkItem
from app.services.github_effort_limit_service import plan_split_suggestion


MAX_REFUSALS_PER_ATTEMPT = 20
PLAN_LIMIT_REFUSAL = "plan_estimate_exceeds_limit"


def _attempt_refusals(work_item_id, dispatch_nonce):
    return (
        GithubPlanRefusal.work_item_id == work_item_id,
        GithubPlanRefusal.dispatch_nonce == dispatch_nonce,
    )


async def record_plan_refusal(db, item, estimate_minutes, limit_minutes, *, already_admitted):
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


async def clear_plan_refusal(db, work_item_id, dispatch_nonce):
    await db.execute(update(GithubPlanRefusal).where(
        *_attempt_refusals(work_item_id, dispatch_nonce), GithubPlanRefusal.cleared.is_(False),
    ).values(cleared=True).execution_options(synchronize_session=False))


async def current_plan_refusals(db, item_ids):
    latest = (
        select(func.max(GithubPlanRefusal.id))
        .join(GithubWorkItem, GithubWorkItem.id == GithubPlanRefusal.work_item_id)
        .where(
            GithubPlanRefusal.work_item_id.in_(item_ids),
            GithubPlanRefusal.dispatch_nonce == GithubWorkItem.dispatch_nonce,
            GithubPlanRefusal.cleared.is_(False),
        )
        .group_by(GithubPlanRefusal.work_item_id)
    )
    rows = await db.scalars(select(GithubPlanRefusal).where(GithubPlanRefusal.id.in_(latest)))
    return {row.work_item_id: row for row in rows}

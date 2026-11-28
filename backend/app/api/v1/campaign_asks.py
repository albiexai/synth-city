"""#1260: the Director's asks of the Leader, the Supervisor or a verifier about a campaign (design §6).

Writes are Director only, like the campaign start. Reading an ask writes nothing: a reply is already stored in Mail.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import operator_write_context, require_director_action
from app.database import get_db
from app.models.database import Campaign
from app.models.schemas import CampaignAskCreate, CampaignAskLessonCreate, CampaignAskView
from app.services import campaign_ask_service as asks
from app.services import factory_audit_service as audit
from app.services.github_client import github_client

router = APIRouter()
_STATUS = {"campaign_ask_not_found": 404, "campaign_ask_role_unavailable": 404, "campaign_ask_not_available": 409,
           "campaign_ask_open": 409, "campaign_ask_verifier_warning": 409, "campaign_ask_not_answered": 409,
           "campaign_ask_lesson_recorded": 409}


async def _campaign(db: AsyncSession, campaign_id: int) -> Campaign:
    campaign = await db.get(Campaign, campaign_id) if 0 < campaign_id < 2**63 else None
    if campaign is None:
        raise HTTPException(404, "campaign_not_found")
    return campaign


def _refused(error: asks.AskError) -> HTTPException:
    return HTTPException(_STATUS.get(error.code, 409), {"code": error.code, "message": error.detail})


@router.get("/campaigns/{campaign_id}/asks", response_model=list[CampaignAskView])
async def list_campaign_asks(campaign_id: int, db: AsyncSession = Depends(get_db)):
    return await asks.list_asks(db, await _campaign(db, campaign_id), github_client)


@router.post("/campaigns/{campaign_id}/asks", response_model=CampaignAskView, status_code=201,
             dependencies=[Depends(require_director_action())])
async def create_campaign_ask(campaign_id: int, request: CampaignAskCreate,
                              context: dict = Depends(operator_write_context), db: AsyncSession = Depends(get_db)):
    """Freeze the numbers into a version and send the role's question, with the Director's optional one, by Mail."""
    campaign = await _campaign(db, campaign_id)
    actor = audit.derive_actor(actor_kind="operator", via=context["via"], session_id=context.get("session_id"))
    try:
        return await asks.create_ask(
            db, campaign, role=request.role, member_id=request.member_id, client=github_client, actor=actor,
            director_question=request.director_question,
            acknowledge_verifier_warning=request.acknowledge_verifier_warning)
    except asks.AskError as error:
        raise _refused(error) from None


@router.post("/campaigns/{campaign_id}/asks/{request_id}/lesson", response_model=CampaignAskView,
             dependencies=[Depends(require_director_action())])
async def record_campaign_ask_lesson(campaign_id: int, request_id: int, request: CampaignAskLessonCreate,
                                     context: dict = Depends(operator_write_context),
                                     db: AsyncSession = Depends(get_db)):
    """Record the answered diagnosis as a lesson, once."""
    campaign = await _campaign(db, campaign_id)
    actor = audit.derive_actor(actor_kind="operator", via=context["via"], session_id=context.get("session_id"))
    try:
        return await asks.record_lesson(db, campaign, request_id, text=request.text, actor=actor, client=github_client)
    except asks.AskError as error:
        raise _refused(error) from None

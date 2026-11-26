"""#1243: the Director's campaign actions. Director only: the operator key over the UI or the operator API."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import operator_write_context, require_director_action
from app.database import get_db
from app.models.schemas import CampaignStartUpdate
from app.services import campaign_record_service as campaigns
from app.services import factory_audit_service as audit

router = APIRouter()
_STATUS = {"campaign_not_found": 404, "campaign_not_active": 409, "campaign_start_changed": 409,
           "campaign_start_in_future": 422}


@router.patch("/{preset_id}/campaign/start", dependencies=[Depends(require_director_action())])
async def adjust_campaign_start(preset_id: int, request: CampaignStartUpdate,
                                context: dict = Depends(operator_write_context),
                                db: AsyncSession = Depends(get_db)):
    """Adjust the start of the team's active campaign, for one created on upgrade or started by hand (design §2)."""
    if not 0 < preset_id < 2**63:
        raise HTTPException(404, "campaign_not_found")
    actor = audit.derive_actor(actor_kind="operator", via=context["via"], session_id=context.get("session_id"))
    try:
        campaign = await campaigns.adjust_start(db, preset_id, started_at=request.started_at,
                                                expected_started_at=request.expected_started_at, actor=actor)
    except campaigns.CampaignError as error:
        raise HTTPException(_STATUS.get(error.code, 409), error.code) from None
    return {"campaign_id": campaign.id, "preset_id": campaign.preset_id, "selector": campaign.selector,
            "status": campaign.status, "started_at": campaign.started_at}

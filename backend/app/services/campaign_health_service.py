"""#1256: the numbers behind the Overview campaign health block (design section 8).

Read only. It reads a snapshot, never applies anything and writes nothing. A finished campaign reads its frozen version.
A live one reads the snapshot that the bottleneck route also uses (cached for a short time, so a polling panel does not
read GitHub on every call). A number that the factory has not recorded is None with a reason, never zero.

Cost: ``cost`` is an optional section of the snapshot payload, written by the cost headline (#1250). This service passes
it through in the shape of ``HealthCost``. A snapshot without that section reads as unavailable with the reason
``cost_not_recorded``, so the block picks up the cost when the section appears and nothing else changes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.models.database import Campaign
from app.services import campaign_snapshot_service as snapshots
from app.services.retro_bottleneck_service import BottleneckWindow

Coverage = Literal["full", "partial", "unavailable"]


class HealthUnknownMember(BaseModel):
    member: str
    reason: str


class HealthCost(BaseModel):
    """The API-equivalent cost so far, with what it covers. ``value_usd`` is None when nothing can be said."""

    value_usd: float | None = None
    coverage: Coverage = "unavailable"
    members_total: int | None = None
    members_known: int | None = None
    unknown_members: list[HealthUnknownMember] = []
    prices_as_of: str | None = None
    reasons: list[str] = []


class CampaignHealthResponse(BaseModel):
    schema_version: Literal[1] = 1
    campaign_id: int
    source: Literal["live", "frozen", "unavailable"]
    as_of: datetime | None  # the cutoff that the numbers describe
    item_time: snapshots.ItemTimeSection | None
    item_time_reason: str | None  # why there is no item time
    cost: HealthCost
    bottleneck: BottleneckWindow | None  # the last-four-hours window of the ranking
    bottleneck_reason: str | None


def _cost(payload: dict | None, missing: str) -> HealthCost:
    section = (payload or {}).get("cost")
    if section is None:
        return HealthCost(reasons=[missing])
    try:
        return HealthCost.model_validate(section)
    except ValidationError:
        return HealthCost(reasons=["cost_section_invalid"])


def _section(model, payload: dict | None, key: str, missing: str):
    value = (payload or {}).get(key)
    if value is None:
        return None, missing
    try:
        return model.model_validate(value), None
    except ValidationError:
        return None, f"{key}_invalid"


async def campaign_health(db, campaign: Campaign, client) -> CampaignHealthResponse:
    if campaign.status == "finished":
        stored = await snapshots.latest(db, campaign.id)
        payload = stored.payload if stored is not None else None
        source = "frozen" if stored is not None else "unavailable"
    else:
        payload = (await snapshots.build_snapshot(db, campaign, client)).model_dump(mode="json")
        source = "live"
    missing = "no_snapshot" if payload is None else None
    item_time, item_reason = _section(snapshots.ItemTimeSection, payload, "item_time", missing or "snapshot_without_item_time")
    bottlenecks = (payload or {}).get("bottlenecks")
    window, window_reason = _section(BottleneckWindow, bottlenecks or {}, "last_four_hours",
                                     missing or "snapshot_without_bottlenecks")
    as_of = datetime.fromisoformat(payload["as_of"]) if payload and payload.get("as_of") else None
    return CampaignHealthResponse(
        campaign_id=campaign.id, source=source, as_of=as_of, item_time=item_time, item_time_reason=item_reason,
        cost=_cost(payload, missing or "cost_not_recorded"), bottleneck=window, bottleneck_reason=window_reason)

"""Operations findings: stuck states and silent background failures the factory found in itself (#941)."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.schemas import GithubRateLimitStatus
from app.services.operations_health_service import operations_health_service

# Under /health on purpose: it is the detail behind the health fields, and the factory pages keep "operations" out
# of the paths they read.
router = APIRouter(prefix="/health", tags=["Operations"])


class OperationsFindingResponse(BaseModel):
    id: int
    kind: str
    subject: str
    scope_id: Optional[int] = None
    work_item_id: Optional[int] = None
    detail: str
    status: str
    occurrences: int
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: Optional[datetime] = None
    notified_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class OperationsFindingsResponse(BaseModel):
    health: str
    reasons: list[str]
    open_findings: Optional[int] = None
    github_rate_limit: GithubRateLimitStatus | None = None
    findings: list[OperationsFindingResponse]


@router.get("/findings", response_model=OperationsFindingsResponse)
async def list_operations_findings(
    limit: int = Query(50, ge=1, le=200), db: AsyncSession = Depends(get_db)
) -> OperationsFindingsResponse:
    """Open findings first, then the most recently resolved. Read-only and public-safe."""
    health = await operations_health_service.health()
    rows = await operations_health_service.findings(db, limit=limit)
    return OperationsFindingsResponse(
        **health, findings=[OperationsFindingResponse.model_validate(row) for row in rows])

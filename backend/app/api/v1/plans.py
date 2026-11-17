"""Plan history browser API endpoints."""
import os
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from app.services.codex_plan_service import CodexPlanService
from app.services.plan_service import PlanService
from app.utils.path_utils import get_claude_plans_dir, get_user_home
from app.api.v1.harness_scope import current_scope
from app.models.schemas import (
    PlanDetailResponse,
    PlanListResponse,
    PlanSearchResponse,
    PlanStatsResponse,
)

router = APIRouter()


PLAN_PROVIDERS = {"claude-code", "codex-cli"}


def _claude_plans_dir(project_path: Optional[str]) -> Path:
    """The Claude plans directory for this request.

    Listing a directory that cannot be read yields nothing instead of an error, so an unreadable plans folder of
    another user's home would look like "no plans". Under a non-controller scope the home's own plans folder is
    opened first, so a denial raises and the scope answers 403 with the directory named (#1065). Only that folder is
    opened, never a path the request chose: a plansDirectory setting that points elsewhere is not the home's data.
    The controller's own scope is unchanged.
    """
    plans_dir = PlanService.resolve_plans_dir(project_path)
    home_plans = get_claude_plans_dir()
    if plans_dir == home_plans and get_user_home() != Path.home() and home_plans.is_dir():
        os.scandir(home_plans).close()
    return plans_dir


def _validate_provider(provider: str) -> str:
    if provider not in PLAN_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_provider_operation",
                "message": f"Plans are not supported for provider: {provider}",
                "provider": provider,
                "operation": "plans",
                "supported_providers": sorted(PLAN_PROVIDERS),
            },
        )
    return provider


@router.get("/plans", response_model=PlanListResponse)
async def list_plans(
    project_path: Optional[str] = Query(None, description="Active project path for settings resolution"),
    provider: str = Query("claude-code", description="Agent provider id"),
):
    """List all plan files sorted by modification time (newest first)."""
    try:
        provider = _validate_provider(provider)
        data_only = provider == "codex-cli"      # the Codex plans never read .claude
        if provider == "codex-cli":
            codex = CodexPlanService()
            plans = codex.list_plans(project_path)
            data_dir = codex.codex_home
        else:
            plans_dir = _claude_plans_dir(project_path)
            plans = PlanService.list_plans(plans_dir)
            # Judge readability by the home's own plans folder only, never by a configured one (see _claude_plans_dir).
            # The folder handed on is the home's own, not plans_dir: plans_dir can come from the request.
            home_plans = get_claude_plans_dir()
            data_dir = home_plans if plans_dir == home_plans else None
        return {"plans": plans, "total": len(plans), "scope": current_scope(data_dir, data_only=data_only)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list plans: {str(e)}")


@router.get("/plans/stats", response_model=PlanStatsResponse)
async def get_plan_stats(
    project_path: Optional[str] = Query(None, description="Active project path"),
    provider: str = Query("claude-code", description="Agent provider id"),
):
    """Get plan statistics for dashboard."""
    try:
        provider = _validate_provider(provider)
        if provider == "codex-cli":
            return CodexPlanService().get_plan_stats(project_path)

        plans_dir = _claude_plans_dir(project_path)
        return PlanService.get_plan_stats(plans_dir)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get plan stats: {str(e)}")


@router.get("/plans/search", response_model=PlanSearchResponse)
async def search_plans(
    q: str = Query(..., min_length=1, description="Search query"),
    project_path: Optional[str] = Query(None, description="Active project path"),
    provider: str = Query("claude-code", description="Agent provider id"),
):
    """Search plans by title and content."""
    try:
        provider = _validate_provider(provider)
        if provider == "codex-cli":
            results = CodexPlanService().search_plans(q, project_path)
        else:
            plans_dir = _claude_plans_dir(project_path)
            results = PlanService.search_plans(plans_dir, q)
        return {"results": results, "query": q, "total": len(results)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to search plans: {str(e)}")


@router.get("/plans/{filename}", response_model=PlanDetailResponse)
async def get_plan_detail(
    filename: str,
    project_path: Optional[str] = Query(None, description="Active project path"),
    provider: str = Query("claude-code", description="Agent provider id"),
):
    """Get full plan detail with linked sessions."""
    try:
        provider = _validate_provider(provider)
        if provider == "codex-cli":
            plan_data = CodexPlanService().get_plan(filename, project_path)
        else:
            plans_dir = _claude_plans_dir(project_path)
            plan_data = PlanService.get_plan(plans_dir, filename)

        if not plan_data:
            raise HTTPException(status_code=404, detail="Plan not found")

        if provider == "claude-code":
            linked_sessions = PlanService.get_plan_sessions(plan_data["slug"])
            plan_data["linked_sessions"] = linked_sessions
        return {"plan": plan_data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get plan: {str(e)}")

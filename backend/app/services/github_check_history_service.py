"""Append completed GitHub checks from the existing tracked-PR observation."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models.database import GithubCheckRunHistory, GithubCheckRunHistoryCoverage, GithubWorkItem, TeamGithubScope
from app.models.schemas import FactoryDeliveryPolicy
from app.services.github_check_observation import (
    ACTIONS_APP_ID, RawCheckObservation, capture_raw_check_observation,
)
from app.services.github_client import GithubClient

logger = logging.getLogger(__name__)


def _insert_once(table: Table, dialect: str):
    insert = postgres_insert if dialect == "postgresql" else sqlite_insert
    return insert(table).on_conflict_do_nothing(index_elements=list(table.primary_key.columns))


async def install_check_history_coverage(conn: AsyncConnection) -> None:
    table = cast(Table, GithubCheckRunHistoryCoverage.__table__)
    await conn.execute(_insert_once(table, conn.dialect.name), {"id": 1, "started_at": datetime.utcnow()})


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _identifier(value: object) -> int | None:
    return value if type(value) is int and 0 < value < 2**63 else None


def _completed_rows(
    observation: RawCheckObservation, context: dict, policy: FactoryDeliveryPolicy,
) -> list[dict]:
    required = {(check.name, check.app_slug) for check in policy.required_checks}
    rows = []
    for check in observation.checks:
        if check.get("status") != "completed" or check.get("head_sha") != context["head_sha"]:
            continue
        check_id = _identifier(check.get("id"))
        if check_id is None:
            continue
        app = check.get("app")
        app_id = _identifier(app.get("id")) if isinstance(app, dict) else None
        app_slug = _string(app.get("slug")) if isinstance(app, dict) else None
        name = _string(check.get("name"))
        attempt, created, workflow_name, path = None, None, None, None
        if app_id == ACTIONS_APP_ID and app_slug == "github-actions":
            attempt, created = observation.jobs.get(check_id, (None, None))
            suite = check.get("check_suite")
            workflow = observation.workflows.get(_identifier(suite.get("id")) or 0) if isinstance(suite, dict) else None
            if workflow is not None:
                workflow_name = _string(workflow.name)
                path = _string(workflow.path)
                if path is not None:
                    path = re.split(r"(?<=\.yml)@|(?<=\.yaml)@", path, maxsplit=1)[0]
        rows.append({
            **context,
            "check_run_id": check_id,
            "check_name": name,
            "app_id": app_id,
            "app_slug": app_slug,
            "required": (name, app_slug) in required,
            "conclusion": _string(check.get("conclusion")),
            "started_at": _timestamp(check.get("started_at")),
            "completed_at": _timestamp(check.get("completed_at")),
            "job_created_at": _timestamp(created),
            "run_attempt": _identifier(attempt),
            "workflow_name": workflow_name,
            "workflow_path": path,
        })
    return rows


async def observe_tracked_checks(
    db: AsyncSession, scope: TeamGithubScope, item: GithubWorkItem, client: GithubClient,
    head_sha: str, policy: FactoryDeliveryPolicy,
) -> list[dict]:
    owner, repo = scope.repo_owner, scope.repo_name
    context = {
        "repository": f"{owner}/{repo}".lower(),
        "scope_id": scope.id,
        "pr_number": item.pr_number,
        "policy_revision": item.delivery_policy_revision,
        "head_sha": head_sha,
        "observed_at": datetime.utcnow(),
    }
    try:
        with capture_raw_check_observation() as observation:
            return await client.list_check_runs_for_ref(owner, repo, head_sha)
    finally:
        await _persist(db, observation, context, policy)


async def _persist(
    db: AsyncSession, observation: RawCheckObservation, context: dict, policy: FactoryDeliveryPolicy,
) -> None:
    rows = _completed_rows(observation, context, policy)
    bind = db.bind
    if not rows or bind is None:
        return
    # A separate transaction preserves the observation even when verification rolls back.
    try:
        async with async_sessionmaker(bind).begin() as history_db:
            table = cast(Table, GithubCheckRunHistory.__table__)
            await history_db.execute(_insert_once(table, bind.dialect.name), rows)
    except SQLAlchemyError as exc:
        logger.warning("github_check_history_write_failed error_type=%s", type(exc).__name__)

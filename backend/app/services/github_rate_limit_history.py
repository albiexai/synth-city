"""Append observed GitHub quota windows and measure their overlap with a campaign."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.models.database import GithubRateLimitHistoryCoverage, GithubRateLimitWindow
from app.services.sqlite_busy_retry import SqliteBusyExhausted, retry_when_busy

logger = logging.getLogger(__name__)
_MAX_PENDING_WINDOWS = 256


def _insert_once(table: Table, dialect: str):
    insert = postgres_insert if dialect == "postgresql" else sqlite_insert
    return insert(table).on_conflict_do_nothing(index_elements=list(table.primary_key.columns))


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


async def install_rate_limit_history_coverage(conn: AsyncConnection) -> None:
    table = cast(Table, GithubRateLimitHistoryCoverage.__table__)
    await conn.execute(_insert_once(table, conn.dialect.name), {"id": 1, "started_at": datetime.utcnow()})


class GithubRateLimitHistory:
    """Queue sanitized observations without writing inside the HTTP response hook."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions
        self._pending: dict[tuple[str, datetime], datetime] = {}
        self._task: asyncio.Task[None] | None = None

    def observe(self, source: str, started_at: datetime, reset_at: datetime) -> None:
        key = (source, _utc(reset_at))
        if key not in self._pending:
            if len(self._pending) >= _MAX_PENDING_WINDOWS:
                logger.warning("github_rate_limit_history_queue_full")
            else:
                self._pending[key] = _utc(started_at)
        if self._pending and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._write_pending())

    async def drain(self) -> None:
        if self._task is not None:
            await self._task

    async def _write_pending(self) -> None:
        while self._pending:
            batch = self._pending.copy()
            rows = [
                {"source": source, "reset_at": reset_at, "started_at": started_at}
                for (source, reset_at), started_at in batch.items()
            ]
            try:
                async with self._sessions() as db:
                    table = cast(Table, GithubRateLimitWindow.__table__)
                    statement = _insert_once(table, db.get_bind().dialect.name)

                    async def append() -> None:
                        await db.execute(statement, rows)
                        await db.commit()

                    await retry_when_busy(db, append, route="github_rate_limit_history", attempts=2)
            except (SQLAlchemyError, SqliteBusyExhausted) as exc:
                logger.warning("github_rate_limit_history_write_failed error_type=%s", type(exc).__name__)
                return
            for key in batch:
                del self._pending[key]


@dataclass(frozen=True)
class CampaignRateLimitTime:
    seconds: float | None
    sample_count: int
    coverage_started_at: datetime | None


async def campaign_rate_limit_time(
    db: AsyncSession, started_at: datetime, ended_at: datetime,
) -> CampaignRateLimitTime:
    """Measure wall time spent rate-limited, counting shared and overlapping windows once."""
    start, end = _utc(started_at), _utc(ended_at)
    if end <= start:
        raise ValueError("Campaign end must be after its start")
    coverage = await db.scalar(select(GithubRateLimitHistoryCoverage.started_at).where(
        GithubRateLimitHistoryCoverage.id == 1,
    ))
    windows = (await db.execute(select(GithubRateLimitWindow.started_at, GithubRateLimitWindow.reset_at).where(
        GithubRateLimitWindow.reset_at > start,
        GithubRateLimitWindow.started_at < end,
    ).order_by(GithubRateLimitWindow.started_at))).all()
    if coverage is None or coverage > start:
        return CampaignRateLimitTime(None, len(windows), coverage.replace(tzinfo=timezone.utc) if coverage else None)
    seconds = 0.0
    cursor = start
    for window_start, reset in windows:
        left, right = max(cursor, window_start), min(end, reset)
        if left < right:
            seconds += (right - left).total_seconds()
            cursor = right
    return CampaignRateLimitTime(seconds, len(windows), coverage.replace(tzinfo=timezone.utc))

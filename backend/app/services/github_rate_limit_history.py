"""Append observed GitHub quota windows and measure their overlap with a campaign."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

from sqlalchemy import Table, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.models.database import GithubRateLimitHistoryCoverage, GithubRateLimitHistoryGap, GithubRateLimitWindow

logger = logging.getLogger(__name__)
_MAX_PENDING_WINDOWS = 256
_CHECKPOINT_INTERVAL = 60
_active_writers: dict[AsyncEngine | AsyncConnection, GithubRateLimitHistory] = {}
_starting_writer: ContextVar[GithubRateLimitHistory | None] = ContextVar("starting_rate_limit_writer", default=None)


def _insert_once(table: Table, dialect: str):
    insert = postgres_insert if dialect == "postgresql" else sqlite_insert
    return insert(table).on_conflict_do_nothing(index_elements=list(table.primary_key.columns))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


async def install_rate_limit_history_coverage(conn: AsyncConnection) -> None:
    table = cast(Table, GithubRateLimitHistoryCoverage.__table__)
    now = _now()
    await conn.execute(_insert_once(table, conn.dialect.name), {"id": 1, "started_at": now})
    if (writer := _starting_writer.get()) is not None:
        await writer._install_guard(conn, now)


class GithubRateLimitHistory:
    """Queue sanitized observations without writing inside the HTTP response hook."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]):
        self._sessions = sessions
        self._pending: dict[tuple[str, datetime, datetime], datetime] = {}
        self._gap: tuple[datetime, datetime] | None = None
        self._task: asyncio.Task[None] | None = None
        self._checkpoint_task: asyncio.Task[None] | None = None
        self._bind: AsyncEngine | AsyncConnection | None = None
        self.marker_id: int | None = None
        self.version = 0

    @contextmanager
    def install_on_startup(self) -> Iterator[None]:
        """Install the outage guard in the application's database startup transaction."""
        token = _starting_writer.set(self)
        try:
            yield
        finally:
            _starting_writer.reset(token)
        if self._bind is not None:
            _active_writers[self._bind] = self
            self._checkpoint_task = asyncio.create_task(self._checkpoint_loop())

    async def _install_guard(self, conn: AsyncConnection, now: datetime) -> None:
        # An open guard survives a shutdown that cannot persist its pending windows.
        # On restart only the tail after the last durable checkpoint remains unknown.
        await conn.execute(update(GithubRateLimitHistoryGap).where(
            GithubRateLimitHistoryGap.ended_at.is_(None), GithubRateLimitHistoryGap.started_at <= now,
        ).values(ended_at=now))
        table = cast(Table, GithubRateLimitHistoryGap.__table__)
        self.marker_id = await conn.scalar(table.insert().values(started_at=now).returning(table.c.id))
        self._bind = conn.engine

    def observe(
        self, source: str, started_at: datetime, reset_at: datetime, window_reset_at: datetime | None = None,
    ) -> None:
        start, reset = _utc(started_at), _utc(reset_at)
        key = (source, _utc(window_reset_at) if window_reset_at is not None else reset, reset)
        if key not in self._pending:
            self.version += 1
            if len(self._pending) >= _MAX_PENDING_WINDOWS:
                if self._gap is None:
                    self._gap = (start, reset)
                else:
                    self._gap = (min(start, self._gap[0]), max(reset, self._gap[1]))
                logger.warning("github_rate_limit_history_queue_full")
            else:
                self._pending[key] = start
        self._ensure_task()

    def overlaps_unwritten(self, start: datetime, end: datetime) -> bool:
        if self._gap is not None and self._gap[0] < end and self._gap[1] > start:
            return True
        return any(starts < end and reset > start for (_, _, reset), starts in self._pending.items())

    def _ensure_task(self) -> None:
        if (self._pending or self._gap is not None) and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._write_pending())

    async def drain(self) -> None:
        if self._task is not None:
            await self._task
        self._ensure_task()
        if self._task is not None:
            await self._task

    async def _advance_guard(self, db: AsyncSession, through: datetime) -> None:
        if self.marker_id is not None:
            await db.execute(update(GithubRateLimitHistoryGap).where(
                GithubRateLimitHistoryGap.id == self.marker_id,
                GithubRateLimitHistoryGap.ended_at.is_(None),
                GithubRateLimitHistoryGap.started_at < through,
            ).values(started_at=through))

    async def _checkpoint_loop(self) -> None:
        while True:
            await asyncio.sleep(_CHECKPOINT_INTERVAL)
            if self._pending or self._gap is not None:
                self._ensure_task()
                continue
            # Later observations belong to the unchecked tail, even if they arrive during the write.
            through = _now()
            try:
                async with self._sessions.begin() as db:
                    await self._advance_guard(db, through)
            except SQLAlchemyError as exc:
                logger.warning("github_rate_limit_history_checkpoint_failed error_type=%s", type(exc).__name__)

    async def close(self) -> None:
        try:
            if self._checkpoint_task is not None:
                self._checkpoint_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._checkpoint_task
            await self.drain()
            if self.marker_id is not None and not self._pending and self._gap is None:
                async with self._sessions.begin() as db:
                    await db.execute(delete(GithubRateLimitHistoryGap).where(
                        GithubRateLimitHistoryGap.id == self.marker_id,
                    ))
        except SQLAlchemyError as exc:
            logger.warning("github_rate_limit_history_close_failed error_type=%s", type(exc).__name__)
        finally:
            if self._bind is not None and _active_writers.get(self._bind) is self:
                del _active_writers[self._bind]

    async def _write_pending(self) -> None:
        while self._pending or self._gap is not None:
            batch, gap, through = self._pending.copy(), self._gap, _now()
            rows = [
                {"source": source, "window_reset_at": window_reset, "reset_at": reset, "started_at": start}
                for (source, window_reset, reset), start in batch.items()
            ]
            for attempt in range(2):
                try:
                    async with self._sessions.begin() as db:
                        if rows:
                            table = cast(Table, GithubRateLimitWindow.__table__)
                            await db.execute(_insert_once(table, db.get_bind().dialect.name), rows)
                        if gap is not None:
                            db.add(GithubRateLimitHistoryGap(started_at=gap[0], ended_at=gap[1]))
                        await self._advance_guard(db, through)
                except SQLAlchemyError as exc:
                    if attempt == 1:
                        logger.warning("github_rate_limit_history_write_failed error_type=%s", type(exc).__name__)
                        return
                    await asyncio.sleep(0.05)
                else:
                    break
            for key in batch:
                del self._pending[key]
            if self._gap == gap:
                self._gap = None


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

    writer = _active_writers.get(db.bind) if db.bind is not None else None
    version = writer.version if writer is not None else None
    overlaps_unwritten = writer is not None and writer.overlaps_unwritten(start, end)
    coverage = await db.scalar(select(GithubRateLimitHistoryCoverage.started_at).where(
        GithubRateLimitHistoryCoverage.id == 1,
    ))
    first = func.min(GithubRateLimitWindow.started_at)
    last = func.max(GithubRateLimitWindow.reset_at)
    windows = (await db.execute(select(first, last)
        .group_by(GithubRateLimitWindow.source, GithubRateLimitWindow.window_reset_at)
        .having(last > start, first < end)
        .order_by(first))).all()
    gap_query = select(GithubRateLimitHistoryGap.id).where(
        GithubRateLimitHistoryGap.started_at < end,
        or_(GithubRateLimitHistoryGap.ended_at.is_(None), GithubRateLimitHistoryGap.ended_at > start),
    )
    if writer is not None and writer.marker_id is not None:
        # The local writer accounts for its own pending data. Other open guards
        # are incomplete from this reader's point of view.
        gap_query = gap_query.where(GithubRateLimitHistoryGap.id != writer.marker_id)
    has_gap = await db.scalar(gap_query.limit(1)) is not None
    coverage_started_at = coverage.replace(tzinfo=timezone.utc) if coverage is not None else None
    if (coverage is None or coverage > start or has_gap or overlaps_unwritten
            or (writer is not None and writer.version != version)):
        return CampaignRateLimitTime(None, len(windows), coverage_started_at)
    seconds = 0.0
    cursor = start
    for window_start, reset in windows:
        left, right = max(cursor, window_start), min(end, reset)
        if left < right:
            seconds += (right - left).total_seconds()
            cursor = right
    return CampaignRateLimitTime(seconds, len(windows), coverage_started_at)

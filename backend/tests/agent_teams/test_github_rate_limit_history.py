"""Rate-limit history at the existing HTTP response boundary."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import database
from app.services.github_rate_limit_service import GithubRateLimited, GithubRateLimitService

NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)


@pytest_asyncio.fixture
async def history_database(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}")
    monkeypatch.setattr(database, "engine", engine)
    try:
        await database.init_db()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            yield db, sessions
    finally:
        await engine.dispose()


async def table_exists(db):
    return bool(await db.scalar(text(
        "SELECT 1 FROM sqlite_master WHERE name = 'github_rate_limit_windows'"
    )))


def response(status, headers, path="/repos/owner/a/pulls"):
    return httpx.Response(
        status, headers=headers, request=httpx.Request(
            "GET", "https://api.github.com" + path, headers={"Authorization": "Bearer dummy-credential"},
        ),
    )


@pytest.mark.asyncio
async def test_exhausted_windows_are_distinct_and_campaign_time_unions_overlaps(history_database):
    db, sessions = history_database
    assert await table_exists(db), "the exhausted windows table is missing"
    from app.services.github_rate_limit_history import GithubRateLimitHistory, campaign_rate_limit_time

    writer = GithubRateLimitHistory(sessions)
    now = NOW + 2 * MINUTE
    service = GithubRateLimitService(now=lambda: now, window_observer=writer.observe)
    core = response(200, {
        "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int((NOW + 10 * MINUTE).timestamp())),
    })
    await service._after_response(core)

    now += MINUTE
    with pytest.raises(GithubRateLimited):
        await service._after_response(response(403, core.headers))  # already in flight
    other = response(429, {"retry-after": "120", "x-ratelimit-remaining": "5"})
    now += MINUTE
    with pytest.raises(GithubRateLimited):
        await service._after_response(other)
    now += timedelta(seconds=15)
    with pytest.raises(GithubRateLimited):
        await service._after_response(other)
    await service._after_response(response(403, {}))
    await service._after_response(response(200, {"x-ratelimit-remaining": "0"}, "/graphql"))

    await writer.drain()
    # Reobserving the known core reset after rebuilding the in-memory client keeps the durable row.
    restarted = GithubRateLimitService(now=lambda: now, window_observer=writer.observe)
    await restarted._after_response(core)
    now = NOW + 11 * MINUTE
    await service._after_response(response(200, {
        "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int((NOW + 20 * MINUTE).timestamp())),
    }))
    await writer.drain()

    rows = (await db.execute(text(
        "SELECT source, started_at, reset_at FROM github_rate_limit_windows ORDER BY started_at"
    ))).all()
    assert [(source, datetime.fromisoformat(started).minute, datetime.fromisoformat(reset).minute)
            for source, started, reset in rows] == [
        ("core", 2, 10), ("secondary", 4, 6), ("core", 11, 20),
    ]
    sample = await campaign_rate_limit_time(db, NOW + 5 * MINUTE, NOW + 15 * MINUTE)
    assert (sample.seconds, sample.sample_count) == (540, 3)
    assert sample.coverage_started_at is not None
    empty = await campaign_rate_limit_time(db, NOW + 21 * MINUTE, NOW + 22 * MINUTE)
    assert (empty.seconds, empty.sample_count) == (0, 0)
    partial = await campaign_rate_limit_time(db, sample.coverage_started_at - MINUTE, NOW)
    assert partial.seconds is None


@pytest.mark.asyncio
async def test_history_coverage_exists_before_polling_and_survives_restart(history_database):
    db, _ = history_database
    table = "github_rate_limit_history_coverage"
    assert await db.scalar(text("SELECT 1 FROM sqlite_master WHERE name = :table"), {"table": table})
    started = await db.scalar(text(f"SELECT started_at FROM {table}"))
    assert started is not None
    await db.commit()
    await database.init_db()
    assert (await db.scalars(text(f"SELECT started_at FROM {table}"))).all() == [started]


@pytest.mark.asyncio
async def test_database_failure_preserves_github_cooldown(history_database, caplog):
    db, sessions = history_database
    assert await table_exists(db), "the exhausted windows table is missing"
    from app.services.github_rate_limit_history import GithubRateLimitHistory

    await db.execute(text("DROP TABLE github_rate_limit_windows"))
    await db.commit()
    writer = GithubRateLimitHistory(sessions)
    service = GithubRateLimitService(now=lambda: NOW, window_observer=writer.observe)
    with pytest.raises(GithubRateLimited) as refused:
        await service._after_response(response(429, {"retry-after": "60"}))
    assert refused.value.retry_at == NOW + MINUTE
    await writer.drain()
    assert "github_rate_limit_history_write_failed" in caplog.text
    with pytest.raises(GithubRateLimited):
        service.require_available("Bearer dummy-credential")


@pytest.mark.asyncio
async def test_lifespan_records_without_awaiting_an_open_database_writer(history_database, monkeypatch):
    db, sessions = history_database
    from app import main
    from app.config import settings
    from app.services import provider_upgrade
    from app.services.cc_bridge import pty_relay

    monkeypatch.setattr(main, "AsyncSessionLocal", sessions)
    monkeypatch.setattr(settings, "test_instance", True)
    monkeypatch.setattr(provider_upgrade, "upgrade_providers", AsyncMock())
    monkeypatch.setattr(main.test_instance_campaign_poller, "start", AsyncMock())
    monkeypatch.setattr(main.test_instance_campaign_poller, "shutdown", AsyncMock())
    monkeypatch.setattr(main.github_dispatch_scheduler, "shutdown", AsyncMock())
    monkeypatch.setattr(pty_relay, "close_all_relays", AsyncMock())
    monkeypatch.setattr(main.github_rate_limit_service, "_now", lambda: NOW)

    async with main.lifespan(main.app):
        await db.execute(text(
            "INSERT INTO github_rate_limit_windows (source, started_at, reset_at) "
            "VALUES ('core', '2029-01-01 00:00:00', '2029-01-01 00:01:00')"
        ))
        try:
            with pytest.raises(GithubRateLimited):
                await asyncio.wait_for(main.github_rate_limit_service._after_response(
                    response(429, {"retry-after": "60"}),
                ), timeout=1)
        finally:
            await db.rollback()

    assert main.github_rate_limit_service.window_observer is None
    assert (await db.scalars(text("SELECT source FROM github_rate_limit_windows"))).all() == ["secondary"]

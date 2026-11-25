"""Rate-limit history at the existing HTTP response boundary."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
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
        await service._after_response(response(429, {"retry-after": "105", "x-ratelimit-remaining": "5"}))
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
    from app.services.github_rate_limit_history import campaign_rate_limit_time

    await db.execute(text("DELETE FROM github_rate_limit_history_coverage"))
    assert (await campaign_rate_limit_time(db, NOW, NOW + MINUTE)).seconds is None
    with pytest.raises(ValueError, match="after its start"):
        await campaign_rate_limit_time(db, NOW, NOW)


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


@pytest.mark.asyncio
async def test_later_secondary_deadline_adds_only_its_extension(history_database):
    db, sessions = history_database
    from app.services.github_rate_limit_history import GithubRateLimitHistory, campaign_rate_limit_time

    writer = GithubRateLimitHistory(sessions)
    now = NOW
    service = GithubRateLimitService(now=lambda: now, window_observer=writer.observe)
    for offset, delay in [(0, "60"), (1, "300"), (2, "299")]:
        now = NOW + timedelta(seconds=offset)
        with pytest.raises(GithubRateLimited):
            await service._after_response(response(429, {"retry-after": delay}))
    await writer.drain()
    sample = await campaign_rate_limit_time(db, NOW, NOW + 10 * MINUTE)
    assert (sample.seconds, sample.sample_count) == (301, 2)
    starts = (await db.scalars(text("SELECT started_at FROM github_rate_limit_windows ORDER BY started_at"))).all()
    assert [datetime.fromisoformat(value).replace(tzinfo=timezone.utc) for value in starts] == [NOW, NOW + MINUTE]


@pytest.mark.asyncio
async def test_core_window_identity_uses_the_current_github_reset():
    started = NOW
    observed = []
    service = GithubRateLimitService(now=lambda: started, window_observer=lambda *window: observed.append(window))

    def refusal(reset):
        return response(403, {
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(reset.timestamp())), "retry-after": "1800",
        })

    for reset in (NOW + 10 * MINUTE, NOW + 20 * MINUTE):
        with pytest.raises(GithubRateLimited) as result:
            await service._after_response(refusal(reset))
        assert result.value.retry_at == NOW + 30 * MINUTE
    assert observed == [("core", NOW, NOW + 10 * MINUTE), ("core", NOW, NOW + 20 * MINUTE)]

    service = GithubRateLimitService(now=lambda: started, window_observer=lambda *window: observed.append(window))
    with pytest.raises(GithubRateLimited):
        await service._after_response(refusal(NOW - MINUTE))
    assert observed[-1] == ("core", NOW, NOW + 30 * MINUTE)

    def observer_error(*_):
        raise RuntimeError("fixture recorder error")

    service = GithubRateLimitService(now=lambda: started, window_observer=observer_error)
    with pytest.raises(GithubRateLimited):
        await service._after_response(response(429, {"retry-after": "60"}))
    with pytest.raises(GithubRateLimited):
        service.require_available("Bearer dummy-credential")


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["retry", "drain", "abandoned"])
async def test_failed_write_recovers_or_leaves_unknown_durable_coverage(history_database, recovery):
    db, sessions = history_database
    from app.services.github_rate_limit_history import GithubRateLimitHistory, campaign_rate_limit_time

    writer = GithubRateLimitHistory(sessions)
    with writer.install_on_startup():
        await database.init_db()
    began = datetime.now(timezone.utc)
    failing = True
    attempts = 0
    write_retried = asyncio.Event()

    def fail_insert(_conn, _cursor, statement, _params, _context, _executemany):
        nonlocal attempts
        if not statement.startswith("INSERT INTO github_rate_limit_windows "):
            return
        attempts += 1
        if failing and (recovery != "retry" or attempts == 1):
            raise OperationalError(None, None, RuntimeError("fixture unavailable"))
        write_retried.set()

    event.listen(db.bind.sync_engine, "before_cursor_execute", fail_insert)
    writer.observe("secondary", began, began + MINUTE)
    if recovery == "retry":
        await asyncio.wait_for(write_retried.wait(), timeout=2)
    await writer.drain()

    async def sample():
        return await campaign_rate_limit_time(db, began, began + MINUTE)

    if recovery == "abandoned":
        assert (await sample()).seconds is None
        await writer.close()
        assert (await sample()).seconds is None
        failing = False
        replacement = GithubRateLimitHistory(sessions)
        with replacement.install_on_startup():
            await database.init_db()
        try:
            assert (await sample()).seconds is None
            restarted = datetime.now(timezone.utc)
            uninterrupted = await campaign_rate_limit_time(db, restarted, restarted + MINUTE)
            assert uninterrupted.seconds == 0
        finally:
            await replacement.close()
    else:
        failing = False
        await writer.drain()
        assert (await sample()).seconds == 60
        await writer.close()


@pytest.mark.asyncio
async def test_queue_overflow_leaves_a_durable_unknown_interval(history_database, monkeypatch):
    db, sessions = history_database
    from app.services import github_rate_limit_history as history

    monkeypatch.setattr(history, "_MAX_PENDING_WINDOWS", 1)
    writer = history.GithubRateLimitHistory(sessions)
    with writer.install_on_startup():
        await database.init_db()
    writer.observe("core", NOW, NOW + MINUTE)
    writer.observe("secondary", NOW + 5 * MINUTE, NOW + 6 * MINUTE)
    await writer.close()
    earlier = await history.campaign_rate_limit_time(db, NOW, NOW + MINUTE)
    assert earlier.seconds == 60
    lost = await history.campaign_rate_limit_time(db, NOW + 5 * MINUTE, NOW + 6 * MINUTE)
    assert lost.seconds is None

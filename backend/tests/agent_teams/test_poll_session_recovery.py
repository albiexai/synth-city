"""#1148: every broad except in the poll path recovers the session before it is reused (Supervisor option C).

recover_session(db, error) rolls back whenever the session cannot be reused: any SQLAlchemyError, or a transaction
that is no longer active. Writes staged before a non-database failure (an HTTP error from a notification, say) are
kept, so nothing is silently discarded (#920). If the rollback itself fails, the error propagates: an unusable
session is never reused.
"""
from __future__ import annotations

import ast
import pathlib

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.database import AgentTeamPreset

SERVICES = pathlib.Path(__file__).resolve().parents[2] / "app" / "services"
POLL_PATH = ["github_dispatch_service.py", "github_watcher_service.py", "github_verification_service.py",
             "github_coordination_service.py", "wake_followup_service.py"]
BROAD = {"Exception", "BaseException", "OperationalError", "SQLAlchemyError", "DBAPIError"}


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _preset(name):
    return AgentTeamPreset(name=name, description="", created_by="test")


async def _poison_with_a_failed_flush(db):
    """The state a lock during a write leaves: the transaction needs a rollback before the session can be used."""
    db.add(AgentTeamPreset(id=1, name="first", description="", created_by="test"))
    await db.flush()
    db.add(AgentTeamPreset(id=1, name="duplicate", description="", created_by="test"))
    with pytest.raises(IntegrityError) as caught:
        await db.flush()
    return caught.value


@pytest.mark.asyncio
async def test_a_failed_transaction_is_recovered_and_the_session_works_again(maker):
    from app.services.session_recovery import recover_session

    async with maker() as db:
        error = await _poison_with_a_failed_flush(db)
        assert await recover_session(db, error) is True
        # Without recovery the next statement raises PendingRollbackError; with it, the session works.
        db.add(_preset("after recovery"))
        await db.commit()
        assert (await db.scalar(select(AgentTeamPreset.name).where(AgentTeamPreset.name == "after recovery")))


@pytest.mark.asyncio
async def test_a_lock_error_is_recovered(maker):
    from app.services.session_recovery import recover_session

    async with maker() as db:
        await db.execute(text("SELECT 1"))
        locked = OperationalError("INSERT", {}, Exception("database is locked"))
        assert await recover_session(db, locked) is True
        db.add(_preset("after lock"))
        await db.commit()


@pytest.mark.asyncio
async def test_a_caught_non_database_error_after_writes_keeps_the_staged_writes(maker):
    from app.services.session_recovery import recover_session

    async with maker() as db:
        db.add(_preset("staged before the notification"))
        await db.flush()
        http_error = httpx.ConnectError("notification transport down")
        assert await recover_session(db, http_error) is False
        await db.commit()
    async with maker() as db:
        assert await db.scalar(select(AgentTeamPreset.name).where(
            AgentTeamPreset.name == "staged before the notification")) is not None


@pytest.mark.asyncio
async def test_an_inactive_transaction_is_rolled_back_even_for_a_non_database_error(maker):
    from app.services.session_recovery import recover_session

    async with maker() as db:
        await _poison_with_a_failed_flush(db)
        # The caught error is not a database error, but the session can no longer be used.
        assert await recover_session(db, ValueError("unrelated")) is True
        db.add(_preset("usable again"))
        await db.commit()


@pytest.mark.asyncio
async def test_a_failed_rollback_propagates_so_the_session_is_not_reused(maker, monkeypatch):
    from app.services.session_recovery import recover_session

    async with maker() as db:
        error = await _poison_with_a_failed_flush(db)

        async def broken_rollback():
            raise OperationalError("ROLLBACK", {}, Exception("disk I/O error"))

        monkeypatch.setattr(db, "rollback", broken_rollback)
        with pytest.raises(OperationalError):
            await recover_session(db, error)


def _handlers_needing_recovery():
    """Broad handlers whose try body uses `db` but which neither recover it nor re-raise."""
    missing = []
    for name in POLL_PATH:
        tree = ast.parse((SERVICES / name).read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef) or "db" not in {a.arg for a in fn.args.args + fn.args.kwonlyargs}:
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Try):
                    continue
                uses_db = any(isinstance(n, ast.Name) and n.id == "db" for stmt in node.body for n in ast.walk(stmt))
                for handler in node.handlers:
                    types = [] if handler.type is None else (
                        handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type])
                    names = {getattr(t, "id", getattr(t, "attr", "")) for t in types}
                    if handler.type is not None and not names & BROAD:
                        continue
                    body = ast.unparse(ast.Module(body=handler.body, type_ignores=[]))
                    if uses_db and "recover_session(db" not in body and not any(
                            isinstance(s, ast.Raise) for s in handler.body):
                        missing.append(f"{name}:{handler.lineno} {fn.name}")
    return missing


def test_every_broad_except_in_the_poll_path_recovers_the_session():
    # A new broad except that uses the session must call recover_session(db, error) or re-raise.
    assert _handlers_needing_recovery() == []


@pytest.mark.asyncio
async def test_a_poll_failure_finding_names_the_failing_stage(monkeypatch):
    from app.services import github_dispatch_scheduler as scheduler_module
    from app.services.operations_health_service import operations_health_service

    recorded = []

    async def record(owner, repo, error, stage=None):
        recorded.append((owner, repo, type(error).__name__, stage))

    async def run_once(self, db, owner, repo, **kwargs):
        scheduler_module._poll_stage.set("monitor_dispatched")
        raise OperationalError("UPDATE", {}, Exception("database is locked"))

    monkeypatch.setattr(operations_health_service, "record_poll_failure", record)
    monkeypatch.setattr(scheduler_module.GithubDispatchScheduler, "run_repo_once", run_once)
    await scheduler_module.GithubDispatchScheduler().run_repo_job("owner", "repo")
    assert recorded == [("owner", "repo", "OperationalError", "monitor_dispatched")]

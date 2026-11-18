"""#1149: less write contention on the SQLite database.

Status reads (GET /agent-bridge/sessions and Mail status projections) no longer write input-wait state; a lock that
outlasts the busy timeout answers 503 with Retry-After instead of 500; the busy timeout is 15 s; and the controller's
checkpoint backup copies step-wise, releasing its read lock between steps, with a bound on steps that make no progress.
"""
from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models.database  # noqa: F401  (registers the tables)
from app.database import Base


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'contention.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
async def test_the_status_projection_never_records_input_waits(maker, monkeypatch):
    # GET /agent-bridge/sessions and Mail status reads reach observe_team through agent_status_service.
    from app.services import agent_activity_service
    from app.services.agent_status_service import agent_status_service

    calls = []

    async def observe(db, preset_id, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(slots=[])

    monkeypatch.setattr(agent_activity_service, "observe_team", observe)
    async with maker() as db:
        await agent_status_service.for_members(db, [SimpleNamespace(id=1, team_preset_id=3, team_slot_id=None)])
    assert calls and all(call.get("record_input_waits") is False for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [True, False])
async def test_observe_team_records_input_waits_only_when_asked(maker, monkeypatch, record):
    from app.services import agent_activity_service

    recorded = []

    async def inputs(db, preset_id, *args):
        return []

    async def record_waits(*args, **kwargs):
        recorded.append(args)
        return False

    monkeypatch.setattr(agent_activity_service, "_team_inputs", inputs)
    monkeypatch.setattr(agent_activity_service, "_record_input_waits", record_waits)
    async with maker() as db:
        await agent_activity_service.observe_team(
            db, 3, include_agent_status=False, persist_identities=False, record_input_waits=record)
    assert bool(recorded) is record


def test_the_busy_timeout_is_fifteen_seconds(tmp_path):
    from app.database import configure_sqlite_connection

    connection = sqlite3.connect(tmp_path / "pragma.db")
    configure_sqlite_connection(connection)
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 15000
    connection.close()


@pytest.mark.asyncio
async def test_a_write_that_outlasts_the_busy_timeout_answers_503(tmp_path):
    from app.main import app as product_app
    from app.main import install_database_busy_handler

    path = tmp_path / "locked.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("CREATE TABLE t (x INTEGER)")
    holder.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 0.05})
    sessions = async_sessionmaker(engine)

    async def get_db():
        async with sessions() as db:
            yield db

    app = FastAPI()
    install_database_busy_handler(app)

    @app.post("/write")
    async def write(db=Depends(get_db)):
        await db.execute(text("INSERT INTO t VALUES (1)"))
        await db.commit()
        return {"ok": True}

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/write")
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        assert response.json() == {"detail": "database_busy"}
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        await engine.dispose()
    from sqlalchemy.exc import OperationalError

    assert OperationalError in product_app.exception_handlers


def _source(path, rows=600):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE t (x BLOB)")
    connection.executemany("INSERT INTO t VALUES (randomblob(4000))", [()] * rows)
    connection.commit()
    connection.close()


def test_the_stepwise_backup_lets_a_writer_commit_between_steps(tmp_path):
    from app.services.maintenance_operations import _stepwise_backup

    source, destination = tmp_path / "source.db", tmp_path / "copy.db"
    _source(source)
    writer = sqlite3.connect(source, isolation_level=None, timeout=0.05)
    committed = []
    steps = []

    def write_once(remaining, total):
        steps.append(remaining)
        if len(steps) == 2:  # between steps the copy holds no lock, so this commits without waiting
            writer.execute("UPDATE t SET x = randomblob(4000) WHERE rowid = 1")
            committed.append(True)

    _stepwise_backup(source, destination, pages=64, max_stalled_steps=20, sleep=0, on_step=write_once)
    writer.close()
    assert committed and len(steps) > 2
    copy = sqlite3.connect(destination)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT count(*) FROM t").fetchone()[0] == 600
    copy.close()


@pytest.mark.parametrize("statement", [
    "INSERT INTO t VALUES (randomblob(4000))",  # the remaining count grows with the database
    "UPDATE t SET x = randomblob(4000) WHERE rowid = 1",  # the remaining count does not grow (Leader's case)
])
def test_continuous_writes_stop_the_backup_and_leave_no_partial_copy(tmp_path, statement):
    from app.services.maintenance_operations import BackupStalled, _stepwise_backup

    source, destination = tmp_path / "source.db", tmp_path / "copy.db"
    _source(source)
    destination.touch()  # the caller's exclusive reservation
    writer = sqlite3.connect(source, isolation_level=None)

    def write_every_step(remaining, total):
        writer.execute(statement)  # every step restarts the copy

    with pytest.raises(BackupStalled):
        _stepwise_backup(source, destination, pages=64, max_stalled_steps=5, sleep=0, on_step=write_every_step)
    writer.close()
    assert not destination.exists()


def test_the_checkpoint_backup_uses_the_stepwise_copy():
    import ast
    import inspect

    from app.services import maintenance_operations

    tree = ast.parse(inspect.getsource(maintenance_operations))
    calls = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    assert "_stepwise_backup" in calls
    # Only the helper may call sqlite3's backup; a one-step copy elsewhere would hold the read lock throughout.
    direct = [fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name != "_stepwise_backup"
              for node in ast.walk(fn) if isinstance(node, ast.Call) and ast.unparse(node.func).endswith(".backup")]
    assert direct == []

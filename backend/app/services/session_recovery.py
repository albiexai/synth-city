"""Recover a database session after a caught exception in the poll path (#1148, Supervisor option C).

A caught failure must never leave the session unusable for the next statement: after "database is locked" or a
failed flush, the transaction needs a rollback, and anything run on it raises PendingRollbackError (live #1144).
recover_session rolls back whenever the session cannot be reused. A non-database failure, such as a notification's
HTTP error, leaves the stage's staged writes in place, so they commit with the stage as before (#920). If the rollback
itself fails, its error propagates: an unusable session is never handed back for reuse.
"""
from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


def _transaction_unusable(db: AsyncSession) -> bool:
    transaction = db.sync_session.get_transaction()
    return transaction is not None and not transaction.is_active


async def recover_session(db: AsyncSession, error: BaseException) -> bool:
    """Roll back when ``db`` cannot be reused after ``error``; return whether it did. A failed rollback raises."""
    if isinstance(error, SQLAlchemyError) or _transaction_unusable(db):
        await db.rollback()
        return True
    return False

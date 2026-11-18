"""Recover a database session after a caught exception in the poll path (#1148, Supervisor option C).

A caught failure must never leave the session unusable for the next statement: after "database is locked" or a
failed flush, the transaction needs a rollback, and anything run on it raises PendingRollbackError (live #1144).
recover_session rolls back whenever the session cannot be reused. A non-database failure, such as a notification's
HTTP error, leaves the stage's staged writes in place, so they commit with the stage as before (#920). If the rollback
itself fails, its error propagates: an unusable session is never handed back for reuse. After a rollback the objects
the session still holds are reloaded, so a caller never reads an expired attribute. Call it first in a handler.
"""
from __future__ import annotations

from sqlalchemy.exc import InvalidRequestError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession


def _transaction_unusable(db: AsyncSession) -> bool:
    transaction = db.sync_session.get_transaction()
    return transaction is not None and not transaction.is_active


async def recover_session(db: AsyncSession, error: BaseException, *, discard: bool = False) -> bool:
    """Roll back when ``db`` cannot be reused after ``error``; return whether it did. A failed rollback raises."""
    # discard=True: the handler drops the stage's writes for any error, as it did before #1148; it must not
    # roll back again itself, because a second rollback would expire the objects reloaded here.
    if discard or isinstance(error, SQLAlchemyError) or _transaction_unusable(db):
        await db.rollback()
        # A rollback expires every loaded object; reading one afterwards would lazy-load synchronously
        # (MissingGreenlet) in the caller (V1 review of #1155). Reload what the session still holds.
        for instance in list(db.sync_session.identity_map.values()):
            try:
                await db.refresh(instance)
            except InvalidRequestError:
                db.expunge(instance)  # the row is gone after the rollback; the caller must not use it
        return True
    return False

"""One definition of a done work item, shared by every view that counts done items (#1106)."""
from __future__ import annotations

DONE_STATUSES = ("merged", "completed")


def is_done(status) -> bool:
    return status in DONE_STATUSES

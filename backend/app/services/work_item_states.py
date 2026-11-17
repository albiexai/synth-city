"""One definition of a done work item, shared by every view that counts done items (#1106)."""
from __future__ import annotations

DONE_STATUSES = ("merged", "completed")


def is_done(status) -> bool:
    return status in DONE_STATUSES

# Items that need someone's attention, the same set the tiles' "attention" category counts (#1129).
ATTENTION_STATUSES = ("escalated", "failed")


def needs_attention(status) -> bool:
    return status in ATTENTION_STATUSES

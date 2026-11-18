"""#1181: the projection's known escalation reasons are the dispatcher's, and no escalated or failed item projects
"unknown". Every escalated or failed item maps to abandoned_by_operator or attempt_escalated; a reason the dispatcher
does not know is still never echoed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import factory_projection_service as projection
from app.services.github_dispatch_service import ESCALATION_REASONS

PRIVATE = "synthetic-private-reason-1181"


def _code(status, reason):
    item = SimpleNamespace(dispatch_status=status, escalation_reason=reason, ack_received_at=None)
    legacy = SimpleNamespace(pending_approval_status=None)
    result = projection.waiting(item, legacy, None)
    assert result.actor == "operator"
    assert PRIVATE not in result.model_dump_json()
    return result.reason_code


def test_the_known_list_is_the_dispatchers():
    assert set(projection.KNOWN_ESCALATIONS) == set(ESCALATION_REASONS)


@pytest.mark.parametrize("status", ["escalated", "failed"])
@pytest.mark.parametrize("reason", sorted(ESCALATION_REASONS - {"abandoned_by_operator"}))
def test_every_dispatcher_reason_projects_attempt_escalated(status, reason):
    assert _code(status, reason) == "attempt_escalated"


@pytest.mark.parametrize("status", ["escalated", "failed"])
def test_an_operator_stop_keeps_its_own_code(status):
    assert _code(status, "abandoned_by_operator") == "abandoned_by_operator"


@pytest.mark.parametrize("status", ["escalated", "failed"])
@pytest.mark.parametrize("reason", [None, "", PRIVATE])
def test_no_reason_or_an_unrecognized_one_is_the_fixed_generic_code(status, reason):
    assert _code(status, reason) == "attempt_escalated"

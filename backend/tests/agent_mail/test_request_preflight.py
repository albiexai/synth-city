"""Request preflight (#937): one passing and one failing case per rule, read-only, and parity with the server."""
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.database import (
    FactoryAuditEvent,
    GithubApprovalRequest,
    GithubAttemptScopeRevision,
    MailMessage,
    MailReceipt,
    TeamGithubScope,
)
from app.services import request_preflight
from app.services.github_approval_service import GithubApprovalError, github_approval_service
from tests.agent_mail.test_api import (  # noqa: F401  (fixtures and helpers of the real route tests)
    _dispatch_approval_fixture,
    client,
    live_slot_session_bindings,
)
from tests.agent_teams.test_github_workspace_api import (
    _continuation_proposal_context,
    _continuation_proposal_kwargs,
    _stub_continuation_github,
)
from tests.agent_teams.test_scope_amendments import amendment, healthy_attempt  # noqa: F401

URL = "/api/v1/agent-mail/preflight-request"
SCOPE = SimpleNamespace(max_scope_paths=32, max_scope_commands=16, max_failed_heads_per_revision=2)
IMPLEMENTATION = ["edit_production", "push_pr_head", "request_verification"]
FALLBACK = {"target": "hosted_ci", "if_missing": "install_temporarily", "package": "ruff", "revert_required": True}


def continuation(**overrides):
    values = dict(
        summary="Apply the approved bounded fix", phase="implementation", execution_target="workspace",
        allowed_paths=["src/example.py"], allowed_actions=list(IMPLEMENTATION), allowed_commands=["pytest -q"],
        prohibited_actions=["Do not edit CI"], max_failed_heads=1, tool_fallbacks={}, scope=SCOPE,
    )
    values.update(overrides)
    return values


def codes(result):
    return [problem.code for problem in result.problems]


# --- Each rule: a passing and a failing case --------------------------------------------------


def test_a_clean_continuation_passes():
    result = request_preflight.preflight("continuation", **continuation())
    assert result.ok and result.problems == [] and result.warnings == []
    assert result.as_dict() == {"passed": True, "problems": [], "warnings": []}


@pytest.mark.parametrize("overrides,code", [
    (dict(phase="bogus"), "continuation_phase_invalid"),
    (dict(execution_target="nowhere"), "execution_target_invalid"),
    (dict(summary="   "), "continuation_summary_required"),
    (dict(allowed_paths=[]), "allowed_paths_required"),
    (dict(allowed_paths=["/etc/passwd"]), "allowed_paths_invalid"),
    (dict(allowed_paths=["src/../x.py"]), "allowed_paths_invalid"),
    (dict(allowed_paths=["src/*.py"]), "allowed_paths_invalid"),
    (dict(allowed_paths=[""]), "allowed_paths_invalid"),
    (dict(allowed_actions=["push_pr_head", "request_verification", "made_up"]), "allowed_actions_invalid"),
    (dict(allowed_actions=["edit_production"]), "implementation_completion_actions_required"),
    (dict(allowed_actions=["push_pr_head"]), "implementation_completion_actions_required"),
    (dict(allowed_commands=[""]), "allowed_commands_invalid"),
    (dict(prohibited_actions=[" "]), "prohibited_actions_invalid"),
    (dict(tool_fallbacks={"ruff": {}}), "tool_fallbacks_invalid"),
    (dict(tool_fallbacks={"ruff": FALLBACK}), "hosted_tool_target_required"),
    (dict(allowed_actions=[*IMPLEMENTATION, "install_hosted_ci_tool"]), "hosted_tool_fallback_required"),
    (dict(max_failed_heads=3), "continuation_failed_head_limit_exceeded"),
    (dict(max_failed_heads="1"), "max_failed_heads_invalid"),
    (dict(allowed_paths=[f"src/f{n}.py" for n in range(33)]), "continuation_path_limit_exceeded"),
    (dict(allowed_commands=[f"pytest {n}" for n in range(17)]), "continuation_command_limit_exceeded"),
])
def test_each_continuation_rule_fails_with_its_code(overrides, code):
    result = request_preflight.preflight("continuation", **continuation(**overrides))
    assert code in codes(result), result.as_dict()
    assert not result.ok
    assert all(item["code"] and item["field"] and item["message"] for item in result.as_dict()["problems"])


@pytest.mark.parametrize("overrides", [
    dict(phase="diagnostic", execution_target="hosted_ci",
         allowed_actions=["revert_diagnostic_changes", "collect_hosted_logs"], allowed_commands=["pytest -q"]),
    dict(phase="diagnostic", execution_target="workspace",
         allowed_actions=["revert_diagnostic_changes"], allowed_commands=["make all"]),
    dict(allowed_actions=[*IMPLEMENTATION, "install_hosted_ci_tool"], execution_target="hosted_ci",
         tool_fallbacks={"ruff": FALLBACK}),
])
def test_other_valid_continuation_shapes_pass(overrides):
    assert request_preflight.preflight("continuation", **continuation(**overrides)).ok


def test_diagnostic_needs_revert_and_must_not_build_locally_when_hosted_only():
    base = dict(phase="diagnostic", execution_target="hosted_ci", allowed_actions=["collect_hosted_logs"],
                allowed_commands=["pytest -q"])
    assert "diagnostic_revert_required" in codes(request_preflight.preflight("continuation", **continuation(**base)))
    local = dict(base, allowed_actions=["revert_diagnostic_changes"], allowed_commands=["make all"])
    assert "hosted_diagnostic_local_build_forbidden" in codes(
        request_preflight.preflight("continuation", **continuation(**local)))


def test_every_problem_is_reported_at_once():
    result = request_preflight.preflight("continuation", **continuation(
        execution_target="nowhere", summary="", allowed_paths=["/abs"], allowed_actions=["edit_production"]))
    assert {"execution_target_invalid", "continuation_summary_required", "allowed_paths_invalid",
            "implementation_completion_actions_required"} <= set(codes(result))
    assert len(result.problems) == 4


def amendment_values(**overrides):
    values = continuation(phase="implementation", allowed_paths=["src/a.py"],
                          allowed_actions=["edit_tests", "push_pr_head", "request_verification"])
    values.update(overrides)
    return values


def test_a_clean_amendment_passes():
    assert request_preflight.preflight("scope_amendment", **amendment_values()).ok


@pytest.mark.parametrize("overrides,code", [
    (dict(phase="diagnostic"), "amendment_diagnostic_restoration_required"),
    (dict(attempt_phase="diagnostic"), "amendment_diagnostic_restoration_required"),
    (dict(execution_target="nowhere"), "execution_target_invalid"),
    (dict(allowed_paths=["src/"]), "amendment_closed_file_paths_required"),
    (dict(allowed_paths=[]), "allowed_paths_required"),
    (dict(allowed_actions=["edit_tests"]), "implementation_completion_actions_required"),
    (dict(allowed_actions=["push_pr_head", "request_verification", "made_up"]),
     "implementation_completion_actions_required"),
    (dict(tool_fallbacks={"ruff": FALLBACK}, execution_target="hosted_ci"), "hosted_tool_fallback_required"),
    (dict(summary=""), "continuation_summary_required"),
    (dict(max_failed_heads=0), "continuation_failed_head_limit_exceeded"),
    (dict(max_failed_heads=3), "continuation_failed_head_limit_exceeded"),
    (dict(allowed_paths=[f"src/f{n}.py" for n in range(33)]), "continuation_path_limit_exceeded"),
    (dict(allowed_commands=[f"pytest {n}" for n in range(17)]), "continuation_path_limit_exceeded"),
])
def test_each_amendment_rule_fails_with_its_code(overrides, code):
    result = request_preflight.preflight("scope_amendment", **amendment_values(**overrides))
    assert code in codes(result), result.as_dict()


def test_the_reserved_companion_trailer_in_a_summary_is_refused():
    trailer = request_preflight.companion_paths.TRAILER_HEADER
    for kind, values in (("continuation", continuation()), ("scope_amendment", amendment_values())):
        values["summary"] = f"Fix\n\n{trailer}\n- x"
        assert "continuation_summary_reserved" in codes(request_preflight.preflight(kind, **values))


def test_companions_the_server_adds_are_warned_and_counted_toward_the_limit(monkeypatch):
    monkeypatch.setattr(request_preflight.companion_paths, "companions_for",
                        lambda paths, mapping=None: ["docs/guide.md", "tests/test_a.py"])
    result = request_preflight.preflight("continuation", **continuation())
    assert result.ok
    assert [w.code for w in result.warnings] == ["companion_paths_added"]
    assert "docs/guide.md" in result.warnings[0].message
    tight = SimpleNamespace(max_scope_paths=2, max_scope_commands=16, max_failed_heads_per_revision=2)
    over = request_preflight.preflight("continuation", **continuation(scope=tight))
    assert "continuation_path_limit_exceeded" in codes(over)
    assert request_preflight.preflight("continuation", **continuation(
        scope=SimpleNamespace(max_scope_paths=3, max_scope_commands=16, max_failed_heads_per_revision=2))).ok


@pytest.mark.parametrize("metadata,code", [
    ({}, "plan_allowed_paths_missing"),
    ({"paths": []}, "plan_allowed_paths_missing"),
    ({"allowed_paths": "src/a.py"}, "plan_allowed_paths_missing"),
    ({"allowed_paths": ["src/a.py", 5]}, "plan_allowed_paths_missing"),
    ({"allowed_paths": ["/etc/passwd"]}, "allowed_paths_invalid"),
    ({"allowed_paths": ["src/"]}, "amendment_closed_file_paths_required"),
    ({"allowed_paths": ["src/a.py"], "companion_paths": ["x"]}, "plan_metadata_reserved_key"),
])
def test_initial_plan_metadata_rules_fail(metadata, code):
    result = request_preflight.preflight("initial_plan", summary="Plan", scope=SCOPE, plan_metadata=metadata)
    assert code in codes(result), result.as_dict()


def test_initial_plan_passes_with_closed_allowed_paths_and_needs_a_summary():
    assert request_preflight.preflight("initial_plan", summary="Plan", scope=SCOPE,
                                       plan_metadata={"allowed_paths": ["src/a.py"], "commands": ["x"]}).ok
    empty = request_preflight.preflight("initial_plan", summary=" ", scope=SCOPE,
                                        plan_metadata={"allowed_paths": ["src/a.py"]})
    assert codes(empty) == ["approval_summary_required"]
    # The alias the server accepts is not a problem.
    assert request_preflight.preflight("initial_plan", summary="Plan", scope=SCOPE,
                                       plan_metadata={"paths": ["src/a.py"]}).ok


def test_unread_authority_mail_is_a_problem_and_names_only_the_message_id():
    result = request_preflight.preflight("continuation", **continuation(unread_authority_message_id=14745))
    assert codes(result) == ["unread_authority_mail"]
    assert "14745" in result.problems[0].message


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        request_preflight.preflight("nope", summary="x", scope=SCOPE)


# --- The estimate against the current window ---------------------------------------------------


def test_the_window_today_is_the_fixed_proposal_expiry(monkeypatch):
    assert request_preflight.authority_window_minutes(SCOPE) == settings.github_continuation_proposal_expiry_seconds / 60 == 60
    monkeypatch.setattr(settings, "github_continuation_proposal_expiry_seconds", 7200)
    assert request_preflight.authority_window_minutes(SCOPE) == 120
    assert request_preflight.preflight("continuation", **continuation(estimate=90)).ok


@pytest.mark.parametrize("estimate,code,warned", [
    (90, "estimate_exceeds_window", False),
    ({"low": 30, "high": 90}, "estimate_exceeds_window", False),
    (60.5, "estimate_exceeds_window", False),
    (30, None, False),
    (60, None, True),
    (55, None, True),
    ({"low": 20, "high": 40}, None, False),
    (None, None, False),
])
def test_estimate_against_the_fixed_window(estimate, code, warned):
    for kind, values in (("continuation", continuation(estimate=estimate)),
                         ("scope_amendment", amendment_values(estimate=estimate))):
        result = request_preflight.preflight(kind, **values)
        assert (code in codes(result)) == (code is not None), result.as_dict()
        assert bool(result.warnings and any(w.code == "estimate_near_window" for w in result.warnings)) == warned
        if estimate is None:
            assert result.ok and result.warnings == []


@pytest.mark.parametrize("estimate", [0, -5, True, "30", float("nan"), float("inf"), {"low": 5}, {"high": -1},
                                      {"low": 50, "high": 10}, {"high": 10, "extra": 1}, [30]])
def test_an_unusable_estimate_is_a_problem(estimate):
    assert codes(request_preflight.preflight("continuation", **continuation(estimate=estimate))) == [
        "estimate_invalid"]


def test_changing_one_function_changes_the_window(monkeypatch):
    monkeypatch.setattr(request_preflight, "authority_window_minutes", lambda scope=None: 120)
    assert request_preflight.preflight("continuation", **continuation(estimate=90)).ok
    monkeypatch.setattr(request_preflight, "authority_window_minutes", lambda scope=None: 20)
    assert "estimate_exceeds_window" in codes(
        request_preflight.preflight("continuation", **continuation(estimate=30)))


def test_the_estimate_is_not_part_of_an_initial_plan():
    assert request_preflight.preflight("initial_plan", summary="Plan", scope=SCOPE,
                                       plan_metadata={"allowed_paths": ["src/a.py"]}, estimate=500).ok


# --- Parity with the real submit methods ------------------------------------------------------

CONTINUATION_CASES = [
    dict(phase="bogus"), dict(execution_target="nowhere"), dict(summary="   "),
    dict(allowed_paths=[]), dict(allowed_paths=["/etc/passwd"]), dict(allowed_paths=["src/../x.py"]),
    dict(allowed_actions=["push_pr_head", "request_verification", "made_up"]),
    dict(allowed_actions=["edit_production"]),
    dict(allowed_commands=[""]), dict(prohibited_actions=[" "]),
    dict(tool_fallbacks={"ruff": {}}), dict(tool_fallbacks={"ruff": FALLBACK}),
    dict(allowed_actions=[*IMPLEMENTATION, "install_hosted_ci_tool"]),
    dict(phase="diagnostic", allowed_actions=["collect_hosted_logs"]),
    dict(phase="diagnostic", execution_target="hosted_ci", allowed_actions=["revert_diagnostic_changes"],
         allowed_commands=["make all"]),
    dict(phase="diagnostic", execution_target="hosted_ci", allowed_actions=["revert_diagnostic_changes"],
         tool_fallbacks={"ruff": FALLBACK}),
    dict(max_failed_heads=9),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", CONTINUATION_CASES, ids=lambda case: ",".join(sorted(case)) + str(list(case.values()))[:40])
async def test_continuation_preflight_reports_the_code_the_server_raises(db, tmp_path, monkeypatch, overrides):
    scope, item, _workspace, owner_slot, owner = await _continuation_proposal_context(db, tmp_path)
    _stub_continuation_github(monkeypatch)
    kwargs = {**_continuation_proposal_kwargs(item, owner_slot, owner), **overrides}
    with pytest.raises(GithubApprovalError) as raised:
        await github_approval_service.create_continuation_request(db, item, scope, **kwargs)
    result = request_preflight.preflight(
        "continuation", summary=kwargs["summary"], scope=scope, phase=kwargs["phase"],
        execution_target=kwargs["execution_target"], allowed_paths=kwargs["allowed_paths"],
        allowed_actions=kwargs["allowed_actions"], allowed_commands=kwargs["allowed_commands"],
        prohibited_actions=kwargs["prohibited_actions"], max_failed_heads=kwargs["max_failed_heads"],
        tool_fallbacks=kwargs["tool_fallbacks"])
    assert result.problems, "preflight passed a request the server refuses"
    assert (result.problems[0].code, result.problems[0].status_code) == (
        raised.value.detail, raised.value.status_code)


@pytest.mark.asyncio
async def test_a_continuation_preflight_passes_is_accepted_by_the_server(db, tmp_path, monkeypatch):
    scope, item, _workspace, owner_slot, owner = await _continuation_proposal_context(db, tmp_path)
    _stub_continuation_github(monkeypatch)
    kwargs = _continuation_proposal_kwargs(item, owner_slot, owner)
    result = request_preflight.preflight(
        "continuation", summary=kwargs["summary"], scope=scope, phase=kwargs["phase"],
        execution_target=kwargs["execution_target"], allowed_paths=kwargs["allowed_paths"],
        allowed_actions=kwargs["allowed_actions"], allowed_commands=kwargs["allowed_commands"],
        prohibited_actions=kwargs["prohibited_actions"], max_failed_heads=kwargs["max_failed_heads"],
        tool_fallbacks=kwargs["tool_fallbacks"], estimate=45)
    assert result.ok
    _revision, _approval, created = await github_approval_service.create_continuation_request(
        db, item, scope, **kwargs)
    assert created


AMENDMENT_CASES = [
    dict(phase="diagnostic"), dict(execution_target="nowhere"), dict(allowed_paths=["src/"]),
    dict(allowed_paths=[]), dict(allowed_paths=["/etc/passwd"]), dict(allowed_actions=["edit_tests"]),
    dict(allowed_actions=["push_pr_head", "request_verification", "made_up"]),
    dict(allowed_commands=[""]), dict(prohibited_actions=[" "]),
    dict(tool_fallbacks={"ruff": FALLBACK}, execution_target="hosted_ci"),
    dict(allowed_actions=["edit_tests", "push_pr_head", "request_verification", "install_hosted_ci_tool"]),
    dict(summary=" "), dict(max_failed_heads=0), dict(max_failed_heads=9),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", AMENDMENT_CASES, ids=lambda case: ",".join(sorted(case)) + str(list(case.values()))[:40])
async def test_amendment_preflight_reports_the_code_the_server_raises(db, healthy_attempt, overrides):
    a = healthy_attempt
    kwargs = {**amendment(a), **overrides}
    with pytest.raises(GithubApprovalError) as raised:
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **kwargs)
    result = request_preflight.preflight(
        "scope_amendment", summary=kwargs["summary"], scope=a.scope, phase=kwargs["phase"],
        attempt_phase=a.item.attempt_phase, execution_target=kwargs["execution_target"],
        allowed_paths=kwargs["allowed_paths"], allowed_actions=kwargs["allowed_actions"],
        allowed_commands=kwargs["allowed_commands"], prohibited_actions=kwargs["prohibited_actions"],
        max_failed_heads=kwargs["max_failed_heads"], tool_fallbacks=kwargs["tool_fallbacks"])
    assert result.problems, "preflight passed a request the server refuses"
    assert (result.problems[0].code, result.problems[0].status_code) == (
        raised.value.detail, raised.value.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,limit_value", [("max_scope_paths", 1), ("max_scope_commands", 0)])
async def test_amendment_limits_match_the_server(db, healthy_attempt, field, limit_value):
    a = healthy_attempt
    setattr(a.scope, field, limit_value)
    kwargs = amendment(a)
    with pytest.raises(GithubApprovalError) as raised:
        await github_approval_service.create_scope_amendment_request(db, a.item, a.scope, **kwargs)
    result = request_preflight.preflight(
        "scope_amendment", summary=kwargs["summary"], scope=a.scope, phase=kwargs["phase"],
        attempt_phase=a.item.attempt_phase, execution_target=kwargs["execution_target"],
        allowed_paths=kwargs["allowed_paths"], allowed_actions=kwargs["allowed_actions"],
        allowed_commands=kwargs["allowed_commands"], prohibited_actions=kwargs["prohibited_actions"],
        max_failed_heads=kwargs["max_failed_heads"], tool_fallbacks=kwargs["tool_fallbacks"])
    assert result.problems[0].code == raised.value.detail == "continuation_path_limit_exceeded"


@pytest.mark.asyncio
async def test_an_amendment_preflight_passes_is_accepted_by_the_server(db, healthy_attempt):
    a = healthy_attempt
    kwargs = amendment(a)
    result = request_preflight.preflight(
        "scope_amendment", summary=kwargs["summary"], scope=a.scope, phase=kwargs["phase"],
        attempt_phase=a.item.attempt_phase, execution_target=kwargs["execution_target"],
        allowed_paths=kwargs["allowed_paths"], allowed_actions=kwargs["allowed_actions"],
        allowed_commands=kwargs["allowed_commands"], prohibited_actions=kwargs["prohibited_actions"],
        max_failed_heads=kwargs["max_failed_heads"], tool_fallbacks=kwargs["tool_fallbacks"], estimate=20)
    assert result.ok
    _revision, _approval, created = await github_approval_service.create_scope_amendment_request(
        db, a.item, a.scope, **kwargs)
    assert created


# --- The route: read-only, owner only ----------------------------------------------------------


async def _counts(db):
    return [await db.scalar(select(func.count()).select_from(model)) for model in (
        GithubApprovalRequest, GithubAttemptScopeRevision, MailMessage, MailReceipt, FactoryAuditEvent)]


def body(item, **overrides):
    values = dict(
        kind="continuation", work_item_id=item.id, dispatch_nonce=item.dispatch_nonce,
        summary="Apply the approved bounded fix", phase="implementation", execution_target="workspace",
        allowed_paths=["src/example.py"], allowed_actions=list(IMPLEMENTATION), allowed_commands=["pytest -q"],
        prohibited_actions=["Do not edit CI"], max_failed_heads=1, tool_fallbacks={})
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_the_route_reports_problems_and_writes_nothing(client, db):
    item, _members, tokens = await _dispatch_approval_fixture(db)
    before = await _counts(db)
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    clean = await client.post(URL, headers=headers, json=body(item, estimate_active_minutes=30))
    assert clean.status_code == 200, clean.text
    assert clean.json() == {"passed": True, "problems": [], "warnings": []}
    bad = await client.post(URL, headers=headers, json=body(
        item, phase="bogus", allowed_paths=["/abs"], estimate_active_minutes=90))
    assert bad.status_code == 200
    payload = bad.json()
    assert payload["passed"] is False
    assert {"continuation_phase_invalid", "allowed_paths_invalid", "estimate_exceeds_window"} <= {
        p["code"] for p in payload["problems"]}
    assert set(payload["problems"][0]) == {"code", "field", "message"}
    plan = await client.post(URL, headers=headers, json=dict(
        kind="initial_plan", work_item_id=item.id, dispatch_nonce=item.dispatch_nonce, summary="Plan",
        plan_metadata={}))
    assert "plan_allowed_paths_missing" in [p["code"] for p in plan.json()["problems"]]
    assert await _counts(db) == before


@pytest.mark.asyncio
async def test_the_route_judges_the_scope_limits_and_the_attempt_phase(client, db):
    item, _members, tokens = await _dispatch_approval_fixture(db)
    scope = await db.get(TeamGithubScope, item.scope_id)
    scope.max_scope_paths = 1
    await db.commit()
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    response = await client.post(URL, headers=headers, json=body(item, allowed_paths=["src/a.py", "src/b.py"]))
    assert "continuation_path_limit_exceeded" in [p["code"] for p in response.json()["problems"]]
    item.attempt_phase = "diagnostic"
    await db.commit()
    amendment_response = await client.post(URL, headers=headers, json=body(item, kind="scope_amendment"))
    assert "amendment_diagnostic_restoration_required" in [p["code"] for p in amendment_response.json()["problems"]]


@pytest.mark.asyncio
async def test_the_route_only_serves_the_item_owner(client, db):
    item, _members, tokens = await _dispatch_approval_fixture(db)
    leader = await client.post(URL, headers={"X-Synth-City-Session-Token": tokens[0]}, json=body(item))
    assert leader.status_code == 403 and leader.json()["detail"] == "not_item_owner"
    anonymous = await client.post(URL, json=body(item))
    assert anonymous.status_code == 401
    missing = await client.post(URL, headers={"X-Synth-City-Session-Token": tokens[1]},
                                json=body(item, work_item_id=999999))
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_the_route_reports_a_stale_nonce_and_a_wrong_lease_without_echoing_either(client, db):
    item, _members, tokens = await _dispatch_approval_fixture(db)
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    response = await client.post(URL, headers=headers, json=body(
        item, dispatch_nonce="stale-nonce-value", lease_token="wrong-lease-value"))
    found = {p["code"] for p in response.json()["problems"]}
    assert "stale_nonce" in found
    assert "wrong-lease-value" not in response.text and "stale-nonce-value" not in response.text
    assert item.dispatch_nonce not in response.text


@pytest.mark.asyncio
async def test_the_route_checks_the_lease_only_when_one_is_given(client, db):
    from datetime import datetime

    from app.models.database import GithubWorkspace

    item, _members, tokens = await _dispatch_approval_fixture(db)
    db.add(GithubWorkspace(scope_id=item.scope_id, path="/tmp/preflight-lease", leased_item_id=item.id,
                           leased_at=datetime.utcnow(), lease_token="the-real-lease"))
    await db.commit()
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    right = await client.post(URL, headers=headers, json=body(item, lease_token="the-real-lease"))
    assert right.json()["passed"] is True
    none_given = await client.post(URL, headers=headers, json=body(item))
    assert none_given.json()["passed"] is True
    wrong = await client.post(URL, headers=headers, json=body(item, lease_token="another-lease"))
    assert [p["code"] for p in wrong.json()["problems"]] == ["lease_token_mismatch"]
    assert "the-real-lease" not in wrong.text and "another-lease" not in wrong.text


@pytest.mark.asyncio
async def test_the_route_flags_unread_authority_mail(client, db):
    from app.models.database import MailMessage as Message, MailReceipt as Receipt

    item, members, tokens = await _dispatch_approval_fixture(db)
    leader, owner = members[0], members[1]
    message = Message(kind="message", subject="STOP: hold your edits", body_markdown="private body",
                      sender_member_id=leader.id, sender_type="member", recipient_member_id=owner.id,
                      audience_type="member", audience_id=str(owner.id))
    db.add(message)
    await db.flush()
    db.add(Receipt(message_id=message.id, member_id=owner.id))
    await db.commit()
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    response = await client.post(URL, headers=headers, json=body(item))
    problems = response.json()["problems"]
    assert [p["code"] for p in problems] == ["unread_authority_mail"]
    assert str(message.id) in problems[0]["message"]
    assert "private body" not in response.text and "STOP" not in response.text


@pytest.mark.asyncio
async def test_the_route_rejects_unknown_fields_and_kinds(client, db):
    item, _members, tokens = await _dispatch_approval_fixture(db)
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    assert (await client.post(URL, headers=headers, json=body(item, kind="other"))).status_code == 422
    assert (await client.post(URL, headers=headers, json=body(item, surprise=1))).status_code == 422


# --- Review batch 1 (#955): the submit schemas, and fields judged independently ---------------

SUBMIT = {
    "continuation": "/api/v1/agent-teams/github-work-items/{}/continuation-requests",
    "scope_amendment": "/api/v1/agent-teams/github-work-items/{}/scope-amendments",
}
INSTALL_ACTIONS = [*IMPLEMENTATION, "install_hosted_ci_tool"]


def fallback(package="ruff"):
    return {"ruff": {**FALLBACK, "package": package}}


SCHEMA_CASES = [
    # (overrides, the real submit route answers 422)
    (dict(), False),
    (dict(max_failed_heads=0), True),
    (dict(max_failed_heads=-1), True),
    (dict(max_failed_heads=2), False),
    (dict(summary="x" * 12001), True),
    (dict(summary="x" * 12000), False),
    (dict(execution_target="hosted_ci", allowed_actions=INSTALL_ACTIONS, tool_fallbacks=fallback("p" * 201)), True),
    (dict(execution_target="hosted_ci", allowed_actions=INSTALL_ACTIONS, tool_fallbacks=fallback("p" * 200)), False),
    (dict(execution_target="hosted_ci", allowed_actions=INSTALL_ACTIONS, tool_fallbacks=fallback("")), True),
    (dict(allowed_paths=[5]), True),
    (dict(allowed_actions="push_pr_head"), True),
    (dict(phase="bogus"), True),
    (dict(execution_target="nowhere"), True),
    (dict(tool_fallbacks={"ruff": {**FALLBACK, "extra": 1}}, execution_target="hosted_ci"), True),
    (dict(tool_fallbacks={"ruff": {**FALLBACK, "target": "workspace"}}, execution_target="hosted_ci"), True),
]


def _submit_body(item, lease="not-the-real-lease", **overrides):
    values = dict(
        dispatch_nonce=item.dispatch_nonce, phase="implementation", execution_target="workspace",
        summary="Apply the approved bounded fix", allowed_paths=["src/example.py"],
        allowed_actions=list(IMPLEMENTATION), allowed_commands=["pytest -q"], prohibited_actions=["Do not edit CI"],
        max_failed_heads=1, tool_fallbacks={}, lease_token=lease)
    values.update(overrides)
    return values


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["continuation", "scope_amendment"])
@pytest.mark.parametrize("overrides,refused", SCHEMA_CASES, ids=[
    ",".join(f"{k}={str(v)[:14]}" for k, v in case.items()) or "valid" for case, _ in SCHEMA_CASES])
async def test_preflight_passes_exactly_what_the_real_submit_route_does_not_422(client, db, kind, overrides, refused):
    item, _members, tokens = await _dispatch_approval_fixture(db)
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    if kind == "scope_amendment" and overrides.get("phase") == "bogus":
        pytest.skip("the amendment schema fixes the phase")
    real = await client.post(SUBMIT[kind].format(item.id), headers=headers, json=_submit_body(item, **overrides))
    check = await client.post(URL, headers=headers, json={
        "kind": kind, **{k: v for k, v in _submit_body(item, **overrides).items() if k != "lease_token"},
        "work_item_id": item.id})
    assert check.status_code == 200, check.text
    answer = check.json()
    if refused:
        assert real.status_code == 422, real.text
        assert answer["passed"] is False, answer
        assert answer["problems"], answer
        assert all(p["code"] and p["field"] and p["message"] for p in answer["problems"])
    else:
        assert real.status_code != 422, real.text
        assert answer["passed"] is True, answer
    # A schema problem never echoes the submitted value.
    assert "x" * 100 not in check.text and "p" * 100 not in check.text


@pytest.mark.asyncio
async def test_schema_problems_name_the_field_and_do_not_duplicate_a_mechanical_problem(client, db):
    item, _members, tokens = await _dispatch_approval_fixture(db)
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    answer = (await client.post(URL, headers=headers, json=body(
        item, max_failed_heads=0, summary="x" * 12001, phase="bogus"))).json()
    by_field = {}
    for problem in answer["problems"]:
        by_field.setdefault(problem["field"], []).append(problem["code"])
    assert "request_schema_invalid" in by_field["max_failed_heads"] or "continuation_failed_head_limit_exceeded" in by_field["max_failed_heads"]
    assert by_field["summary"] == ["request_schema_invalid"]
    assert by_field["phase"] == ["continuation_phase_invalid"]  # reported once, not again by the schema


@pytest.mark.asyncio
async def test_an_initial_plan_summary_over_the_submit_limit_is_a_problem(client, db):
    item, _members, tokens = await _dispatch_approval_fixture(db)
    headers = {"X-Synth-City-Session-Token": tokens[1]}
    request = dict(kind="initial_plan", work_item_id=item.id, dispatch_nonce=item.dispatch_nonce,
                   plan_metadata={"allowed_paths": ["src/a.py"]})
    real = await client.post("/api/v1/agent-mail/approval-requests", headers=headers,
                             json={k: request[k] for k in ("work_item_id", "dispatch_nonce", "plan_metadata")}
                             | {"summary": "x" * 12001})
    assert real.status_code == 422
    answer = (await client.post(URL, headers=headers, json={**request, "summary": "x" * 12001})).json()
    assert answer["passed"] is False and "request_schema_invalid" in [p["code"] for p in answer["problems"]]
    assert (await client.post(URL, headers=headers, json={**request, "summary": "x" * 12000})).json()["passed"] is True


COMPANION = lambda paths, mapping=None: ["docs/guide.md"]  # noqa: E731


def test_a_bad_action_list_does_not_hide_the_companion_path_limit(monkeypatch):
    monkeypatch.setattr(request_preflight.companion_paths, "companions_for", COMPANION)
    tight = SimpleNamespace(max_scope_paths=1, max_scope_commands=16, max_failed_heads_per_revision=2)
    for kind, values in (("continuation", continuation(allowed_actions=["edit_production"], scope=tight)),
                         ("scope_amendment", amendment_values(allowed_actions=["edit_tests"], scope=tight))):
        found = codes(request_preflight.preflight(kind, **values))
        assert "implementation_completion_actions_required" in found, (kind, found)
        assert "continuation_path_limit_exceeded" in found, (kind, found)


def test_an_invalid_command_list_does_not_hide_an_amendment_path_overflow():
    tight = SimpleNamespace(max_scope_paths=1, max_scope_commands=16, max_failed_heads_per_revision=2)
    result = request_preflight.preflight("scope_amendment", **amendment_values(
        allowed_paths=["src/a.py", "src/b.py"], allowed_commands=[""], scope=tight))
    assert {"allowed_commands_invalid", "continuation_path_limit_exceeded"} <= set(codes(result))


def test_an_invalid_path_list_does_not_hide_a_command_overflow():
    tight = SimpleNamespace(max_scope_paths=32, max_scope_commands=1, max_failed_heads_per_revision=2)
    for kind, values in (("continuation", continuation(allowed_paths=["/abs"], allowed_commands=["a", "b"], scope=tight)),
                         ("scope_amendment", amendment_values(allowed_paths=["/abs"], allowed_commands=["a", "b"],
                                                              scope=tight))):
        found = codes(request_preflight.preflight(kind, **values))
        assert "allowed_paths_invalid" in found, (kind, found)
        assert ("continuation_command_limit_exceeded" if kind == "continuation" else
                "continuation_path_limit_exceeded") in found, (kind, found)


def test_each_checker_keeps_the_fields_that_validated():
    problems, normalized = request_preflight.continuation_body(**{
        key: value for key, value in continuation(allowed_actions=["edit_production"]).items()
        if key not in {"summary"}})
    assert problems and normalized is not None
    assert normalized.paths == ["src/example.py"] and normalized.commands == ["pytest -q"]
    assert normalized.actions == ["edit_production"]
    problems, normalized = request_preflight.continuation_body(**{
        key: value for key, value in continuation(allowed_paths=["/abs"]).items() if key not in {"summary"}})
    assert normalized.paths is None and normalized.commands == ["pytest -q"]


@pytest.mark.parametrize("field", ["allowed_paths", "allowed_actions", "allowed_commands", "prohibited_actions"])
def test_a_wrong_container_type_is_one_schema_problem_and_no_noise(field):
    values = continuation()
    values[field] = "not-a-list"
    result = request_preflight.preflight("continuation", **values)
    assert [(p.code, p.field) for p in result.problems] == [("request_schema_invalid", field)]
    assert "not-a-list" not in str(result.as_dict())

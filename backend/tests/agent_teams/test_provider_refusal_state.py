"""The provider-refusal state on a member: recogniser -> wait state -> summary (#1091).

A refusal-blocked member is recorded as blocked with the label "Blocked: provider refusal", and it stays ordinary
idle: it does NOT count as waiting on someone else, so idle nudges and the re-wake still try to recover it.

The refusal screen is `codex-provider-refusal.txt`: the three notice lines are a real capture (#1062), the
surroundings are the real idle Codex capture, and where the notice sits on the screen is derived.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.database import MailMessage
from app.models.factory_schemas import TeamWaitingInput
from app.models.schemas import NativeInputWait
from app.services import agent_activity_service as activity
from app.services.agent_team_supervisor_service import (
    is_supervisor_message, require_supervisor_thread, supervisor_inbox,
)

from tests.agent_teams.test_native_input_wait import record_wait, wait_team
from tests.agent_teams.test_provider_refusal_notice import generic_notices, refusal_notices

SCREENS = Path(__file__).resolve().parents[1] / "fixtures/native_screens"
REFUSAL = "provider_refusal"
BLOCKED = "Blocked: provider refusal"
WAITING = "Waiting for input"


@pytest.fixture
def pane(monkeypatch):
    """A fake tmux pane that shows the given fixture screen."""
    def show(name):
        screen = (SCREENS / name).read_text(encoding="utf-8")
        identity, process = ["41000:%8", "41000:%8"], [("S", "27"), ("S", "27")]

        def run(args, **options):
            return SimpleNamespace(returncode=0, stdout=screen if args[1] == "capture-pane" else identity.pop(0))

        monkeypatch.setattr(activity.subprocess, "run", run)
        monkeypatch.setattr(activity, "_process", lambda _: process.pop(0))
        return activity.ActivityBinding(41000, "27", "/synthetic/project", tmux_target="%8", binding_id=1)
    return show


# ---- the capture ------------------------------------------------------------------------------------------


def test_a_codex_refusal_screen_is_captured_as_a_refusal_not_as_an_idle_composer(pane):
    """The refusal screen also reads as an idle composer ("clear"). The refusal has to win."""
    assert activity.recognize_native_input_wait("codex-cli", (SCREENS / "codex-provider-refusal.txt").read_text()) == "clear"
    assert activity._capture_native_input_wait("codex-cli", pane("codex-provider-refusal.txt")) == REFUSAL


@pytest.mark.parametrize("provider,name,expected", [
    ("codex-cli", "codex-idle-prompt.txt", "clear"),
    ("codex-cli", "codex-sign-in.txt", "sign_in"),
    ("claude-code", "claude-question-form.txt", "question"),
    ("claude-code", "claude-idle-after-declined-question.txt", "clear"),
])
def test_the_capture_of_other_screens_is_unchanged(pane, provider, name, expected):
    assert activity._capture_native_input_wait(provider, pane(name)) == expected


def test_a_refusal_notice_on_a_claude_code_pane_is_not_a_refusal(pane):
    assert activity._capture_native_input_wait("claude-code", pane("codex-provider-refusal.txt")) != REFUSAL


# ---- the observation --------------------------------------------------------------------------------------


def test_the_observation_records_the_refusal_and_keeps_the_member_idle(monkeypatch):
    monkeypatch.setattr(activity, "_process", lambda _: ("S", "27"))
    monkeypatch.setattr(activity, "_capture_native_input_wait", lambda *args: REFUSAL)
    binding = activity.ActivityBinding(41000, "27", "/synthetic/project", tmux_target="%8", binding_id=1)
    metadata = {}

    observed = activity._observe(1, "claude-code", None, [binding], False, datetime.now(timezone.utc), provenance=metadata)

    assert observed.waiting_input.reason == REFUSAL
    assert observed.waiting_input.label == BLOCKED
    assert observed.state == "idle"
    assert metadata == {"input_wait_known": True, "input_wait_reason": REFUSAL, "input_wait_binding_id": 1}
    assert "synthetic/project" not in observed.model_dump_json()


# ---- the schemas: the label follows the reason ---------------------------------------------------------------


@pytest.mark.parametrize("model", [NativeInputWait, TeamWaitingInput])
@pytest.mark.parametrize("reason,label", [
    ("question", WAITING), ("trust", WAITING), ("sign_in", WAITING), (REFUSAL, BLOCKED)])
def test_the_label_is_filled_from_the_reason(model, reason, label):
    extra = {"slot_id": 1} if model is TeamWaitingInput else {"observed_at": datetime.now(timezone.utc)}
    assert model(reason=reason, **extra).label == label


@pytest.mark.parametrize("model", [NativeInputWait, TeamWaitingInput])
@pytest.mark.parametrize("reason,label", [(REFUSAL, WAITING), ("question", BLOCKED), ("sign_in", BLOCKED)])
def test_a_label_that_does_not_match_the_reason_is_refused(model, reason, label):
    extra = {"slot_id": 1} if model is TeamWaitingInput else {"observed_at": datetime.now(timezone.utc)}
    with pytest.raises(ValidationError):
        model(reason=reason, label=label, **extra)


@pytest.mark.parametrize("model", [NativeInputWait, TeamWaitingInput])
def test_an_unknown_reason_and_an_extra_field_are_still_refused(model):
    extra = {"slot_id": 1} if model is TeamWaitingInput else {"observed_at": datetime.now(timezone.utc)}
    with pytest.raises(ValidationError):
        model(reason="something_else", **extra)
    with pytest.raises(ValidationError):
        model(reason=REFUSAL, surprise=True, **extra)


# ---- the recorded state and its one notice -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("supervised", [True, False])
async def test_the_refusal_is_recorded_once_per_episode_and_clears_with_the_screen(db, supervised):
    team, actor, slot, binding, _, leader = await wait_team(db, supervised=supervised)
    per_episode = 2 if supervised else 1                      # Leader and Supervisor, or the Leader alone

    await record_wait(db, team, slot, binding, REFUSAL)
    await db.refresh(binding)
    assert binding.waiting_input_reason == REFUSAL
    first = await refusal_notices(db)
    assert len(first) == per_episode
    for notice in first:                                      # the refusal has its own notice, not the generic one
        assert notice.subject == "Provider refusal" and notice.payload["notice"] == REFUSAL
        assert "provider refusal" in notice.body_markdown and "waits for input" not in notice.body_markdown
        assert "synthetic/project" not in notice.body_markdown
    assert await generic_notices(db) == []

    await record_wait(db, team, slot, binding, REFUSAL)              # the same episode: no second notice
    assert len(await refusal_notices(db)) == per_episode

    await record_wait(db, team, slot, binding, "clear")              # the screen moved on
    await db.refresh(binding)
    assert binding.waiting_input_reason is None

    await record_wait(db, team, slot, binding, REFUSAL)              # a new episode
    assert len(await refusal_notices(db)) == 2 * per_episode


# ---- a refusal-blocked member stays ordinary idle -------------------------------------------------------------


@pytest.mark.parametrize("reason,counts", [
    ("question", True), ("trust", True), ("sign_in", True), (REFUSAL, False)])
def test_only_a_refusal_does_not_count_as_waiting_on_someone_else(reason, counts):
    assert activity.counts_as_external_wait(reason) is counts


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,external_wait", [(REFUSAL, False), ("question", True)])
async def test_the_idle_service_does_not_suppress_a_refused_member(monkeypatch, reason, external_wait):
    """A real consumer: github_team_idle_service.projection passes external_wait to the idle observation."""
    from app.services import github_operator_attention_service as attention_module
    from app.services.github_team_idle_service import github_team_idle_service

    async def observe_team(db, preset_id, **kwargs):
        if kwargs.get("input_fence") is not None:
            kwargs["input_fence"].clear()
        wait = SimpleNamespace(reason=reason, label=BLOCKED if reason == REFUSAL else WAITING)
        return SimpleNamespace(slots=[SimpleNamespace(slot_id=1, state="idle", waiting_input=wait)],
                               valid_until=datetime.now(timezone.utc) + timedelta(seconds=60))

    async def team_inputs(db, preset_id, max_rows=None, identity=None):
        return []

    class Attention:
        async def summary(self, db, preset_id):
            return {"coverage_complete": True, "actions": []}

    seen = {}

    async def observe(db, preset_id, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(activity, "observe_team", observe_team)
    monkeypatch.setattr(activity, "_team_inputs", team_inputs)
    monkeypatch.setattr(attention_module, "github_operator_attention_service", Attention())
    monkeypatch.setattr(github_team_idle_service, "observe", observe)

    await github_team_idle_service.projection(None, 1, now=datetime.now(timezone.utc))

    assert seen["external_wait"] is external_wait


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,label,external_wait", [(REFUSAL, BLOCKED, False), ("question", WAITING, True)])
async def test_the_summary_lists_a_refusal_but_does_not_treat_it_as_an_external_wait(
        db, monkeypatch, reason, label, external_wait):
    """A real consumer: the team summary lists the wait for Teams, and passes external_wait to the idle view."""
    from app.services.agent_team_summary_service import AgentTeamSummaryService
    from app.services.github_team_idle_service import github_team_idle_service
    from tests.agent_teams.test_mail_request_stall import _Attention, fake_native, make_team

    monkeypatch.setattr("app.services.agent_team_summary_service.hold_code", lambda: None)

    async def no_supervisor(db, preset_id, now=None):
        return None

    monkeypatch.setattr("app.services.agent_team_summary_service.observe_supervisor", no_supervisor)
    team, people = await make_team(db)
    await db.commit()
    fake_native(monkeypatch, people)
    first_slot = people[0].slot.id

    async def observe_team(db, preset_id, **kwargs):
        if kwargs.get("input_fence") is not None:
            kwargs["input_fence"].clear()
        wait = NativeInputWait(reason=reason, observed_at=datetime.now(timezone.utc))
        return SimpleNamespace(
            slots=[SimpleNamespace(slot_id=p.slot.id, state="idle", waiting_input=wait if p.slot.id == first_slot else None)
                   for p in people],
            valid_until=datetime.now(timezone.utc) + timedelta(seconds=60))

    monkeypatch.setattr("app.services.agent_activity_service.observe_team", observe_team)
    seen = {}

    async def observe(db, preset_id, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(github_team_idle_service, "observe", observe)

    summary = await AgentTeamSummaryService(attention=_Attention()).summary(db, team.id)

    assert [(w.slot_id, w.reason, w.label) for w in summary.waiting_input] == [(first_slot, reason, label)]
    assert seen["external_wait"] is external_wait


# ---- a change into or out of a refusal is a new episode (V1's review of #1096) ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("question", REFUSAL), ("trust", REFUSAL), ("sign_in", REFUSAL),
                                          (REFUSAL, "question"), (REFUSAL, "sign_in")])
@pytest.mark.parametrize("supervised", [True, False])
async def test_a_change_into_or_out_of_a_refusal_stores_the_new_reason_and_one_new_notice(db, supervised, first, second):
    """No clear observation in between: the periodic observation can go straight from one screen to the other."""
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=supervised)
    per_episode = 2 if supervised else 1

    await record_wait(db, team, slot, binding, first)
    await db.refresh(binding)
    first_notice = binding.waiting_input_notice_id
    await record_wait(db, team, slot, binding, second)
    await db.refresh(binding)

    assert binding.waiting_input_reason == second
    assert binding.waiting_input_notice_id not in (None, first_notice)
    generic, refused = await generic_notices(db), await refusal_notices(db)
    # A refusal has its own pair of notices; a wait for input has the generic one. Each episode wrote its own.
    assert [n.payload["reason"] for n in generic] == [r for r in (first, second) if r != REFUSAL]
    assert len(refused) == per_episode
    newest = refused if second == REFUSAL else generic
    assert binding.waiting_input_notice_id in [n.id for n in newest]
    assert all("provider refusal" in n.body_markdown for n in refused)
    assert all("waits for input" in n.body_markdown for n in generic)


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("question", REFUSAL), (REFUSAL, "question")])
async def test_a_repeat_after_a_category_change_adds_no_notice(db, first, second):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    for reason in (first, second, second, second):
        await record_wait(db, team, slot, binding, reason)

    assert len(await generic_notices(db)) == 1 and len(await refusal_notices(db)) == 2


@pytest.mark.asyncio
async def test_a_category_change_and_back_is_a_new_episode_each_time(db):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    for reason in ("question", REFUSAL, "question"):
        await record_wait(db, team, slot, binding, reason)

    assert [n.payload["reason"] for n in await generic_notices(db)] == ["question", "question"]
    assert len(await refusal_notices(db)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", [("question", "trust"), ("trust", "sign_in"), ("sign_in", "question")])
async def test_a_change_between_external_waits_still_keeps_the_first_reason_and_notice(db, first, second):
    """The existing contract: one continuous external wait is one episode, whatever it asks."""
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    await record_wait(db, team, slot, binding, first)
    await record_wait(db, team, slot, binding, second)
    await db.refresh(binding)

    assert binding.waiting_input_reason == first
    assert len((await db.scalars(select(MailMessage))).all()) == 1


@pytest.mark.asyncio
async def test_the_notice_after_a_change_out_of_a_refusal_reaches_the_supervisors_real_inbox_and_thread_gate(db):
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    await record_wait(db, team, slot, binding, REFUSAL)
    await record_wait(db, team, slot, binding, "question")
    notice = (await generic_notices(db))[0]

    assert notice.payload["reason"] == "question"
    assert notice.id in [m.id for m in await supervisor_inbox(db, actor)]
    assert is_supervisor_message(notice) is True
    await require_supervisor_thread(db, actor, notice)


@pytest.mark.asyncio
async def test_a_stale_observation_cannot_change_the_category(db):
    """The observed-at fence still applies: an older observation does not rewrite a newer record."""
    team, actor, slot, binding, _, _ = await wait_team(db, supervised=True)
    now = datetime.now(timezone.utc)
    await record_wait(db, team, slot, binding, "question", now=now)
    await record_wait(db, team, slot, binding, REFUSAL, now=now - timedelta(seconds=30))
    await db.refresh(binding)

    assert binding.waiting_input_reason == "question"
    assert len((await db.scalars(select(MailMessage))).all()) == 1

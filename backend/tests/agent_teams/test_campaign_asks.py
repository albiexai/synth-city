"""#1260: asking the Leader, the Supervisor or a verifier about a campaign, by Mail (design §6).

An ask references an immutable snapshot version (a row of ``campaign_snapshots``). The request and its reply identify
the same version. State lives in Mail: no ask table. The ask is stale when the current version id differs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.database import (
    AgentTeamPreset,
    AgentTeamSlot,
    CampaignSnapshot,
    FactoryAuditEvent,
    GithubWorkItem,
    MailExternalActor,
    MailMessage,
    MailReceipt,
    MailTeamMember,
)
from app.models.schemas import ExternalAgentMailMessageRequest, MailMessageCreate
from app.services import campaign_ask_service as asks
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services import factory_audit_service as audit
from app.services.agent_mail_service import AgentMailService
from app.services.external_agent_mail_service import external_agent_mail_service
from tests.agent_teams.test_campaign_record import (  # noqa: F401  (fixtures)
    FakeGithub,
    _issue,
    db,
    world,
)
from tests.agent_teams.test_campaign_snapshot import _client, fresh_cache  # noqa: F401

pytestmark = pytest.mark.asyncio
T0 = datetime(2026, 11, 20, 9, 0, 0)
OPERATOR = audit.derive_actor(actor_kind="operator")
SYSTEM = audit.derive_actor(actor_kind="system")
mail = AgentMailService()


class Team:
    pass


async def _member(db, preset_id, name, role, position):
    slot = AgentTeamSlot(preset_id=preset_id, position=position, display_name=name, provider="claude",
                         repo_id=name, repo_path=f"/x/{name}", repo_name=name, role=role)
    db.add(slot)
    await db.flush()
    member = MailTeamMember(identity_key=f"team:{preset_id}:{name}", repo_id=name, repo_path=f"/x/{name}",
                            repo_name=name, display_name=name, team_preset_id=preset_id, team_slot_id=slot.id)
    db.add(member)
    await db.flush()
    return slot, member


@pytest_asyncio.fixture
async def team(db, world):  # noqa: F811
    """Team A with a Leader, verifiers V1 and V2, a builder and a bound Supervisor; a live campaign."""
    t = Team()
    preset = await db.get(AgentTeamPreset, world.team)
    t.leader_slot, t.leader = await _member(db, preset.id, "L1", "leader", 0)
    _, t.v1 = await _member(db, preset.id, "V1", " Verifier ", 1)
    _, t.v2 = await _member(db, preset.id, "V2", "verifier", 2)
    _, t.builder = await _member(db, preset.id, "B1", "builder", 3)
    preset.leader_slot_id = t.leader_slot.id
    actor = MailExternalActor(actor_key="sup", display_name="Supervisor", kind="supervisor", token_hash="x")
    db.add(actor)
    await db.flush()
    preset.supervisor_actor_id = actor.id
    t.actor = actor
    await db.commit()
    t.world = world
    t.campaign = await campaigns.start(db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    return t


async def _ask(db, team, role="leader", member=None, client=None, **kwargs):
    kwargs.setdefault("actor", OPERATOR)
    return await asks.create_ask(db, team.campaign, role=role, member_id=member.id if member else None,
                                 client=client or _client(), **kwargs)


async def _list(db, team, client=None):
    """The campaign's asks as the page reads them: the live membership comes from GitHub."""
    return await asks.list_asks(db, team.campaign, client or _client())


async def _answer(db, request_id, member, body="My view."):
    return await mail.send_message(db, MailMessageCreate(kind="answer", thread_root_id=request_id,
                                                         sender_member_id=member.id, body_markdown=body))


async def _supervisor_reply(db, request_id, actor, body="Process view."):
    return await external_agent_mail_service.reply_in_thread(
        db, actor, request_id, ExternalAgentMailMessageRequest(body_markdown=body))


async def _record_changes(db, world):
    """A new record the snapshot reads: an item reaches a new status."""
    item = await db.get(GithubWorkItem, world.items[("a1", 1)])
    item.dispatch_status = "merged"
    await db.commit()


# Each role gets its own question
async def test_the_leader_ask_is_a_context_request_with_its_question_and_the_pinned_version(db, team):
    view = await _ask(db, team, director_question="Why did #1 wait?")
    request = await db.get(MailMessage, view.id)
    assert (request.kind, request.recipient_member_id, request.request_status) == ("context_request", team.leader.id,
                                                                                    "pending")
    assert "coordination and flow" in request.body_markdown.lower() and "Why did #1 wait?" in request.body_markdown
    row = await db.get(CampaignSnapshot, view.referenced_version_id)
    payload = request.payload
    assert (payload["kind"], payload["campaign_id"], payload["snapshot_version_id"], payload["role"]) == (
        "campaign_ask", team.campaign.id, row.id, "leader")
    assert (row.purpose, row.version < 0, row.fingerprint is not None) == ("ask", True, True)


async def test_each_role_has_its_own_question(db, team):
    verifier = await _ask(db, team, "verifier", team.v1, acknowledge_verifier_warning=True)
    supervisor = await _ask(db, team, "supervisor")
    assert "quality and rework" in (await db.get(MailMessage, verifier.id)).body_markdown.lower()
    assert "factory and the process" in (await db.get(MailMessage, supervisor.id)).body_markdown.lower()


async def test_a_supervisor_ask_is_a_notice_the_supervisor_can_read(db, team):
    from app.services.agent_team_supervisor_service import is_supervisor_message, supervisor_inbox
    view = await _ask(db, team, "supervisor")
    request = await db.get(MailMessage, view.id)
    assert is_supervisor_message(request) and request.payload["supervisor_actor_id"] == team.actor.id
    assert view.id in [message.id for message in await supervisor_inbox(db, team.actor)]


async def test_a_role_the_team_lacks_is_refused(db, team):
    preset = await db.get(AgentTeamPreset, team.world.team)
    preset.supervisor_actor_id = None
    await db.commit()
    with pytest.raises(asks.AskError) as error:
        await _ask(db, team, "supervisor")
    assert error.value.code == "campaign_ask_role_unavailable"
    with pytest.raises(asks.AskError) as error:  # a builder is not a verifier
        await _ask(db, team, "verifier", team.builder, acknowledge_verifier_warning=True)
    assert error.value.code == "campaign_ask_role_unavailable"


# The verifier warning
async def test_a_live_verifier_ask_needs_the_warning_acknowledged(db, team):
    with pytest.raises(asks.AskError) as error:
        await _ask(db, team, "verifier", team.v1)
    assert error.value.code == "campaign_ask_verifier_warning" and "takes the verifier from review" in error.value.detail
    assert (await db.scalar(select(func.count()).select_from(MailMessage).where(
        MailMessage.kind == "context_request"))) == 0
    assert (await db.scalar(select(func.count()).select_from(CampaignSnapshot))) == 0  # nothing was pinned
    assert (await _ask(db, team, "verifier", team.v1, acknowledge_verifier_warning=True)).role == "verifier"


async def test_a_frozen_campaign_needs_no_verifier_warning_and_pins_its_final_version(db, team):
    frozen = await snapshots.freeze(db, team.campaign, _client(), actor=SYSTEM, as_of=T0 + timedelta(days=10))
    assert frozen is not None
    view = await _ask(db, team, "verifier", team.v1)
    final = await snapshots.latest(db, team.campaign.id)
    assert view.referenced_version_id == final.id and final.purpose == "final"
    assert (await db.scalar(select(func.count()).select_from(CampaignSnapshot))) == 1  # nothing was written


# The reply is attached to the right request and version
async def test_a_reply_is_attached_to_its_request_with_the_same_version(db, team):
    first, second = await _ask(db, team), await _ask(db, team, "verifier", team.v1, acknowledge_verifier_warning=True)
    await _answer(db, first.id, team.leader, "Queue was the cause.")
    views = {view.id: view for view in await _list(db, team)}
    assert views[first.id].status == "answered" and views[first.id].reply.body == "Queue was the cause."
    assert views[first.id].reply.snapshot_version_id == views[first.id].referenced_version_id
    stored = await db.get(MailMessage, views[first.id].reply.id)  # Mail saved the version on the reply itself
    assert stored.payload["snapshot_version_id"] == views[first.id].referenced_version_id
    assert views[second.id].status == "open" and views[second.id].reply is None


async def test_a_reply_from_another_sender_or_thread_does_not_attach(db, team):
    view = await _ask(db, team, "verifier", team.v1, acknowledge_verifier_warning=True)
    with pytest.raises(Exception):  # the Mail service refuses an answer from a member who was not asked
        await _answer(db, view.id, team.v2)
    await mail.send_message(db, MailMessageCreate(kind="message", recipient_member_id=team.v1.id,
                                                  sender_member_id=team.leader.id, body_markdown="Side note."))
    assert (await _list(db, team))[0].status == "open"


async def test_a_note_in_the_thread_is_not_an_answer(db, team):
    view = await _ask(db, team)
    await mail.send_message(db, MailMessageCreate(kind="message", thread_root_id=view.id,
                                                  sender_member_id=team.leader.id, body_markdown="Looking at it."))
    assert (await _list(db, team))[0].status == "open"
    with pytest.raises(asks.AskError) as error:
        await _ask(db, team)
    assert error.value.code == "campaign_ask_open"


async def test_a_supervisor_reply_attaches_and_closes_the_ask(db, team):
    view = await _ask(db, team, "supervisor")
    await _supervisor_reply(db, view.id, team.actor)
    listed = (await _list(db, team))[0]
    assert (listed.status, listed.reply.body) == ("answered", "Process view.")
    stored = await db.get(MailMessage, listed.reply.id)
    assert stored.payload["snapshot_version_id"] == listed.referenced_version_id == listed.reply.snapshot_version_id
    assert (await _ask(db, team, "supervisor")).id != view.id  # the next ask is allowed


async def test_another_actors_reply_in_the_supervisor_thread_does_not_attach(db, team):
    view = await _ask(db, team, "supervisor")
    other = MailExternalActor(actor_key="other", display_name="Other", kind="external_tool", token_hash="y")
    db.add(other)
    await db.commit()
    try:
        await _supervisor_reply(db, view.id, other)
    except Exception:  # refusing it is as good: it never attaches
        await db.rollback()
        await db.refresh(team.campaign)  # a rollback expires what the session loaded
    assert (await _list(db, team))[0].status == "open"


# One open request per agent per campaign
async def test_one_open_request_per_agent_and_campaign(db, team):
    view = await _ask(db, team)
    with pytest.raises(asks.AskError) as error:
        await _ask(db, team)
    assert error.value.code == "campaign_ask_open"
    assert (await _ask(db, team, "verifier", team.v1, acknowledge_verifier_warning=True)).role == "verifier"
    await _answer(db, view.id, team.leader)
    again = await _ask(db, team)
    assert again.id != view.id
    assert (await db.get(MailMessage, again.id)).delivery_key.endswith(":2")


async def test_two_concurrent_callers_for_one_agent_get_one_request_and_one_conflict(db, team):
    maker = async_sessionmaker(db.bind, expire_on_commit=False)

    async def call():
        async with maker() as session:
            campaign = await campaigns.current(session, team.world.team)
            try:
                return await asks.create_ask(session, campaign, role="leader", member_id=None, client=_client(),
                                             actor=OPERATOR)
            except asks.AskError as error:
                return error

    results = await asyncio.gather(call(), call())
    errors = [r for r in results if isinstance(r, asks.AskError)]
    assert len(errors) == 1 and errors[0].code == "campaign_ask_open"
    assert (await db.scalar(select(func.count()).select_from(MailMessage).where(
        MailMessage.kind == "context_request"))) == 1


async def test_a_reply_racing_a_second_ask_cannot_open_two(db, team):
    view = await _ask(db, team)
    await _answer(db, view.id, team.leader)
    maker = async_sessionmaker(db.bind, expire_on_commit=False)

    async def call():
        async with maker() as session:
            campaign = await campaigns.current(session, team.world.team)
            try:
                return await asks.create_ask(session, campaign, role="leader", member_id=None, client=_client(),
                                             actor=OPERATOR)
            except asks.AskError as error:
                return error

    results = await asyncio.gather(call(), call(), call())
    assert sum(1 for r in results if not isinstance(r, asks.AskError)) == 1
    open_now = [v for v in await _list(db, team) if v.status == "open"]
    assert len(open_now) == 1


async def test_a_failed_send_leaves_no_snapshot_row(db, team, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("mail down")

    monkeypatch.setattr(asks.agent_mail_service, "send_message", broken)
    with pytest.raises(RuntimeError):
        await _ask(db, team)
    await db.rollback()
    assert (await db.scalar(select(func.count()).select_from(CampaignSnapshot))) == 0


# The version: reuse, final-only reads, freeze and recompute unchanged
async def test_two_asks_at_unchanged_records_share_one_version_and_a_change_makes_a_new_one(db, team):
    one = await _ask(db, team)
    two = await _ask(db, team, "supervisor")
    assert one.referenced_version_id == two.referenced_version_id
    assert (await db.scalar(select(func.count()).select_from(CampaignSnapshot))) == 1
    await _record_changes(db, team.world)
    three = await _ask(db, team, "verifier", team.v1, acknowledge_verifier_warning=True)
    assert three.referenced_version_id != one.referenced_version_id
    rows = (await db.scalars(select(CampaignSnapshot).order_by(CampaignSnapshot.version.desc()))).all()
    assert sorted(row.version for row in rows) == [-2, -1] and all(row.purpose == "ask" for row in rows)


async def test_ask_rows_do_not_change_latest_freeze_or_recompute(db, team):
    await _ask(db, team)
    assert await snapshots.latest(db, team.campaign.id) is None  # an ask is not a frozen version
    with pytest.raises(campaigns.CampaignError) as error:
        await snapshots.recompute(db, team.campaign, _client(), actor=OPERATOR)
    assert error.value.code == "campaign_snapshot_not_frozen"
    await snapshots.freeze(db, team.campaign, _client(), actor=SYSTEM, as_of=T0 + timedelta(days=10))
    final = await snapshots.latest(db, team.campaign.id)
    assert (final.version, final.purpose) == (1, "final")
    again = await snapshots.freeze(db, team.campaign, _client(), actor=SYSTEM)  # idempotent
    assert again is not None and (await db.scalar(select(func.count()).select_from(CampaignSnapshot).where(
        CampaignSnapshot.purpose == "final"))) == 1
    await snapshots.recompute(db, team.campaign, _client(), actor=OPERATOR, as_of=T0 + timedelta(days=11))
    assert (await snapshots.latest(db, team.campaign.id)).version == 2


async def test_concurrent_freezes_with_ask_rows_present_store_one_final_version(db, team):
    await _ask(db, team)
    maker = async_sessionmaker(db.bind, expire_on_commit=False)

    async def freeze():
        async with maker() as session:
            campaign = await campaigns.current(session, team.world.team)
            return await snapshots.freeze(session, campaign, _client(), actor=SYSTEM, as_of=T0 + timedelta(days=10))

    await asyncio.gather(freeze(), freeze())
    assert (await db.scalar(select(func.count()).select_from(CampaignSnapshot).where(
        CampaignSnapshot.purpose == "final"))) == 1


async def test_the_campaign_list_reads_only_final_versions(db, team):
    from app.services.factory_campaign_list_service import campaign_list
    await _ask(db, team)
    row = (await campaign_list(db, team_id=None, scope_id=None, status="all", limit=10, cursor=None)).campaigns[0]
    assert row.source is None  # an ask row is not a frozen source of counts


# Stale: the current version id differs
async def test_an_ask_is_current_until_the_records_change_then_stale_by_version_id(db, team):
    view = await _ask(db, team)
    listed = (await _list(db, team))[0]
    assert (listed.stale, listed.version_changed, listed.state) == (False, False, "known")
    assert listed.current_version_id == view.referenced_version_id
    await _record_changes(db, team.world)
    listed = (await _list(db, team))[0]
    assert (listed.stale, listed.version_changed, listed.current_version_id) == (True, True, None)
    assert listed.metrics_unchanged is None  # no current version to compare with


async def test_a_new_frozen_version_is_stale_and_metrics_unchanged_is_detail_only(db, team):
    await snapshots.freeze(db, team.campaign, _client(), actor=SYSTEM, as_of=T0 + timedelta(days=10))
    await _ask(db, team)
    await snapshots.recompute(db, team.campaign, _client(), actor=OPERATOR, as_of=T0 + timedelta(days=10))
    same = (await _list(db, team))[0]
    assert (same.stale, same.version_changed, same.metrics_unchanged) == (True, True, True)  # same numbers, still stale
    await snapshots.recompute(db, team.campaign, FakeGithub(labelled={"a1": [_issue("a1", 1)]}), actor=OPERATOR,
                              as_of=T0 + timedelta(days=10))
    changed = (await _list(db, team))[0]
    assert (changed.stale, changed.metrics_unchanged) == (True, False)


async def test_missing_evidence_is_unknown_never_current(db, team):
    view = await _ask(db, team)
    await db.execute(text("UPDATE campaign_snapshots SET fingerprint = NULL"))
    await db.commit()
    listed = (await _list(db, team))[0]
    assert (listed.state, listed.stale) == ("unknown", None) and listed.unknown_reason == "no_fingerprint"
    assert listed.id == view.id


# V1 review 128: the version on the stored reply, the live measurements, the link, the numbers, the digest
async def test_the_stored_reply_carries_the_root_version_and_the_sender_cannot_name_another(db, team):
    view = await _ask(db, team)
    await mail.send_message(db, MailMessageCreate(kind="answer", thread_root_id=view.id, sender_member_id=team.leader.id,
                                                  body_markdown="Mine.", payload={"snapshot_version_id": 999999}))
    reply = (await db.scalars(select(MailMessage).where(MailMessage.thread_root_id == view.id))).one()
    assert reply.payload["snapshot_version_id"] == view.referenced_version_id
    assert (await _list(db, team))[0].reply.snapshot_version_id == view.referenced_version_id


async def test_a_reply_that_stores_no_version_does_not_attach(db, team):
    view = await _ask(db, team)
    db.add(MailMessage(kind="answer", thread_root_id=view.id, sender_type="member", sender_member_id=team.leader.id,
                       audience_type="member", audience_id=str(team.leader.id), body_markdown="No version."))
    await db.commit()
    listed = (await _list(db, team))[0]
    assert (listed.status, listed.reply) == ("open", None)


async def test_a_reply_that_stores_another_version_does_not_attach(db, team):
    view = await _ask(db, team)
    db.add(MailMessage(kind="answer", thread_root_id=view.id, sender_type="member", sender_member_id=team.leader.id,
                       audience_type="member", audience_id=str(team.leader.id), body_markdown="Other version.",
                       payload={"snapshot_version_id": view.referenced_version_id + 1}))
    await db.commit()
    assert (await _list(db, team))[0].status == "open"


async def test_a_second_labelled_issue_makes_a_new_version_and_the_old_ask_stale(db, team):
    first = await _ask(db, team)
    grown = _client()
    grown.labelled["a1"].append(_issue("a1", 4))
    assert (await _list(db, team, grown))[0].stale is True  # the live membership moved: no version holds it yet
    second = await _ask(db, team, "supervisor", client=grown)
    assert second.referenced_version_id != first.referenced_version_id
    row = await db.get(CampaignSnapshot, second.referenced_version_id)
    assert next(m for m in row.payload["metrics"] if m["name"] == "issues_in_scope")["value"] == 4
    views = {v.id: v for v in await _list(db, team, grown)}
    assert (views[second.id].stale, views[first.id].stale) == (False, True)
    assert views[first.id].current_version_id == second.referenced_version_id


async def _audit_event(db, preset_id, key):
    await audit.record_event(db, event_kind="policy_change", source="agent_teams.update_github_scope", occurred_at=T0,
                             actor=OPERATOR, action_outcome="applied", team_preset_id=preset_id, team_context_key=key)
    await db.commit()


async def test_an_audit_event_of_another_team_leaves_the_ask_current_and_its_own_team_event_does_not(db, team):
    await _ask(db, team)
    await _audit_event(db, team.world.other, await audit.current_context_key(db, "team", team.world.other))
    listed = (await _list(db, team))[0]
    assert (listed.stale, listed.state) == (False, "known")  # another team's record never moves this campaign
    again = await _ask(db, team, "supervisor")
    assert again.referenced_version_id == listed.referenced_version_id  # and its version is reused
    await _audit_event(db, team.world.team, team.campaign.team_context_key)
    assert (await _list(db, team))[0].stale is True  # this team's own event does


async def test_a_membership_that_cannot_be_read_is_unknown_and_never_reused_as_current(db, team):
    view = await _ask(db, team)
    broken = _client(broken={"a2"})
    listed = (await _list(db, team, broken))[0]
    assert (listed.state, listed.stale, listed.current_version_id) == ("unknown", None, None)
    assert listed.unknown_reason == "membership_incomplete"
    assert (await _list(db, team, None))[0].unknown_reason is None  # a good client reads it (the control)
    assert (await asks.list_asks(db, team.campaign))[0].unknown_reason == "membership_not_read"
    other = await _ask(db, team, "supervisor", client=broken)  # a new version, marked incomplete
    row = await db.get(CampaignSnapshot, other.referenced_version_id)
    assert other.referenced_version_id != view.referenced_version_id and row.fingerprint.startswith("incomplete:")
    third = await _ask(db, team, "verifier", team.v1, client=broken, acknowledge_verifier_warning=True)
    assert third.referenced_version_id != other.referenced_version_id  # an unreadable membership is never reused
    views = {v.id: v for v in await _list(db, team)}
    assert views[other.id].stale is True and views[other.id].state == "known"  # read again: stale, not current


async def test_the_link_names_the_version_and_the_request_carries_it(db, team):
    one = await _ask(db, team)
    await _record_changes(db, team.world)
    two = await _ask(db, team, "supervisor")
    assert one.page_link != two.page_link
    for view in (one, two):
        assert view.page_link == f"/insights/retrospectives/{team.campaign.id}?version={view.referenced_version_id}"
        assert view.page_link in (await db.get(MailMessage, view.id)).body_markdown


def _went(**changes):
    went = {"items": 2, "attempts": 5, "unknown_items": 1, "coverage": "partial", "group_coverage": {"process": "full"},
            "stages": [{"group": "process", "stage": "review", "actor": "member:7", "share_seconds": 600.0,
                        "wall_seconds": 900.0, "attempts": 4, "coverage": "full"},
                       {"group": "process", "stage": "required_check", "actor": "ci", "share_seconds": 50.0,
                        "wall_seconds": 50.0, "attempts": 5, "coverage": "full"}]}
    return {**went, **changes}


async def test_a_verifier_request_lists_the_recorded_review_and_rework_first_with_counts_and_coverage(db, team):
    payload = {"metrics": [{"name": "issues_in_scope", "value": 3, "counting_unit": "issue", "coverage": "full",
                            "sample_count": 3}], "time_went": _went()}
    lines = asks._numbers(payload, "verifier").splitlines()
    assert lines[0] == "- Review time: 600 s of item time, 900 s of its own time (coverage full, 4 samples)"
    assert lines[1] == "- Rework: 3 attempts beyond the first, 5 attempts over 2 items (coverage partial, 1 items unknown)"
    assert lines[2].startswith("- issues_in_scope: 3 issue")
    assert asks._numbers(payload, "leader").splitlines()[0].startswith("- issues_in_scope")  # only the verifier's
    none = asks._numbers({"metrics": [], "time_went": _went(stages=[], attempts=0, items=0)}, "verifier")
    assert "Review time: unknown, no review recorded" in none and "Rework: unknown, no attempt recorded" in none
    assert "not recorded in this version" in asks._numbers({"metrics": []}, "verifier")  # an older version
    view = await _ask(db, team, "verifier", team.v1, acknowledge_verifier_warning=True)
    body = (await db.get(MailMessage, view.id)).body_markdown
    assert body.index("- Review time:") < body.index("- issues_in_scope:") and "- Rework:" in body


async def test_metrics_unchanged_compares_the_repository_counts_too(db, team):
    await snapshots.freeze(db, team.campaign, _client(), actor=SYSTEM, as_of=T0 + timedelta(days=10))
    await _ask(db, team)
    moved = FakeGithub(labelled={"a1": [_issue("a1", 1)], "a2": [_issue("a2", 3, state="closed"),
                                                                 _issue("a2", 2, state="closed")]})
    await snapshots.recompute(db, team.campaign, moved, actor=OPERATOR, as_of=T0 + timedelta(days=10))
    first, second = (await db.scalars(select(CampaignSnapshot).order_by(CampaignSnapshot.version))).all()
    scope = lambda row: next(m for m in row.payload["metrics"] if m["name"] == "issues_in_scope")  # noqa: E731
    assert scope(first)["value"] == scope(second)["value"] == 3  # the totals match
    assert [r["issues"] for r in first.payload["repositories"]] != [r["issues"] for r in second.payload["repositories"]]
    assert (await _list(db, team, moved))[0].metrics_unchanged is False


async def test_a_finished_campaign_with_no_final_version_is_unknown_with_a_reason(db, team):
    view = await _ask(db, team)
    team.campaign.status = "finished"
    await db.commit()
    listed = (await _list(db, team))[0]
    assert (listed.state, listed.stale, listed.current_version_id) == ("unknown", None, None)
    assert listed.unknown_reason == "final_version_missing" and listed.id == view.id


# Wait counts and the lesson
async def test_asks_add_no_waits_on_a_decision(db, team):
    await _ask(db, team)
    await _ask(db, team, "supervisor")
    assert (await db.scalar(select(func.count()).select_from(FactoryAuditEvent).where(
        FactoryAuditEvent.event_kind == "mail_wait_started"))) == 0


async def test_a_lesson_is_recorded_once_on_an_answered_ask(db, team):
    view = await _ask(db, team)
    with pytest.raises(asks.AskError) as error:
        await asks.record_lesson(db, team.campaign, view.id, text="Too early.", actor=OPERATOR)
    assert error.value.code == "campaign_ask_not_answered"
    await _answer(db, view.id, team.leader)
    lesson = await asks.record_lesson(db, team.campaign, view.id, text="Batch the approvals.", actor=OPERATOR)
    assert lesson.lesson.text == "Batch the approvals." and lesson.lesson.snapshot_version_id == view.referenced_version_id
    with pytest.raises(asks.AskError) as error:
        await asks.record_lesson(db, team.campaign, view.id, text="Again.", actor=OPERATOR)
    assert error.value.code == "campaign_ask_lesson_recorded"
    assert (await _list(db, team))[0].lesson.text == "Batch the approvals."


async def test_a_finished_campaign_without_a_frozen_version_refuses_asks(db, team):
    team.campaign.status = "finished"
    team.campaign.finished_at = T0 + timedelta(days=3)
    await db.commit()
    with pytest.raises(asks.AskError) as error:
        await _ask(db, team)
    assert error.value.code == "campaign_ask_not_available"


# The Mail rule that lets the asked agent answer a Director ask (amendment 1), and nothing wider
async def _root(db, *, payload, key, recipient, sender_member_id=None, sender_type="system"):
    root = MailMessage(kind="context_request", sender_type=sender_type, sender_member_id=sender_member_id,
                       recipient_member_id=recipient.id, audience_type="member", audience_id=str(recipient.id),
                       body_markdown="Question.", payload=payload, delivery_key=key, request_status="pending")
    db.add(root)
    await db.commit()
    return root


async def test_a_system_request_that_is_not_a_campaign_ask_still_refuses_an_answer(db, team):
    other_kind = await _root(db, payload={"kind": "something_else"}, key="campaign-ask:1:x:1", recipient=team.leader)
    no_prefix = await _root(db, payload={"kind": "campaign_ask"}, key="other:1", recipient=team.leader)
    for root in (other_kind, no_prefix):
        with pytest.raises(ValueError, match="would reach nobody"):
            await _answer(db, root.id, team.leader)


async def test_a_campaign_ask_with_a_member_sender_follows_the_old_rule_and_reaches_its_sender(db, team):
    root = await _root(db, payload={"kind": "campaign_ask"}, key="campaign-ask:1:x:1", recipient=team.leader,
                       sender_member_id=team.builder.id, sender_type="member")
    reply = await _answer(db, root.id, team.leader)
    receipts = (await db.scalars(select(MailReceipt).where(MailReceipt.message_id == reply.id))).all()
    assert [r.member_id for r in receipts] == [team.builder.id]


async def test_nobody_else_can_read_or_receive_a_reply_to_a_director_ask(db, team):
    view = await _ask(db, team)
    reply = await _answer(db, view.id, team.leader, "Private view.")
    assert (await db.scalars(select(MailReceipt).where(MailReceipt.message_id == reply.id))).all() == []
    for outsider in (team.v1, team.builder):
        with pytest.raises(PermissionError):  # a direct read of the thread
            await mail.require_member_thread(db, view.id, outsider.id)
        inbox = await mail.get_inbox(db, outsider.id)
        assert reply.id not in [message.id for message in inbox.messages]
    await mail.require_member_thread(db, view.id, team.leader.id)  # the asked member still can


async def test_a_supervisor_reply_reaches_no_member(db, team):
    view = await _ask(db, team, "supervisor")
    reply = await _supervisor_reply(db, view.id, team.actor)
    assert reply.recipients == []
    assert (await db.scalars(select(MailReceipt).where(MailReceipt.message_id == reply.message.id))).all() == []
    with pytest.raises(PermissionError):
        await mail.require_member_thread(db, view.id, team.leader.id)


# The routes
OPERATOR_KEY = "campaign-ask-operator-key"


@pytest_asyncio.fixture
async def client(db, monkeypatch):
    from app.api.v1 import campaign_asks as routes
    from app.config import settings
    from app.database import get_db
    from app.main import app

    monkeypatch.setattr(settings, "operator_token", OPERATOR_KEY)
    monkeypatch.setattr(routes, "github_client", _client())

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 headers={"X-Synth-City-Operator-Token": OPERATOR_KEY}) as c:
        yield c
    app.dependency_overrides.clear()


def _url(campaign, tail=""):
    return f"/api/v1/agent-teams/campaigns/{campaign.id}/asks{tail}"


async def test_the_routes_ask_list_and_record_a_lesson(client, db, team):
    created = await client.post(_url(team.campaign), json={"role": "leader", "director_question": "Why?"})
    assert created.status_code == 201 and created.json()["status"] == "open"
    request_id = created.json()["id"]
    assert (await client.post(_url(team.campaign), json={"role": "leader"})).json()["detail"]["code"] == \
        "campaign_ask_open"
    assert (await client.post(_url(team.campaign, f"/{request_id}/lesson"), json={"text": "Early."})).json()[
        "detail"]["code"] == "campaign_ask_not_answered"
    await _answer(db, request_id, team.leader)
    lesson = await client.post(_url(team.campaign, f"/{request_id}/lesson"), json={"text": "Batch the approvals."})
    assert lesson.status_code == 200 and lesson.json()["lesson"]["text"] == "Batch the approvals."
    listed = (await client.get(_url(team.campaign))).json()
    assert [(a["id"], a["status"], a["stale"]) for a in listed] == [(request_id, "answered", False)]


async def test_the_verifier_warning_is_a_409_with_its_code_and_text(client, team):
    response = await client.post(_url(team.campaign), json={"role": "verifier", "member_id": team.v1.id})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "campaign_ask_verifier_warning"
    assert "takes the verifier from review" in response.json()["detail"]["message"]
    ok = await client.post(_url(team.campaign), json={"role": "verifier", "member_id": team.v1.id,
                                                      "acknowledge_verifier_warning": True})
    assert ok.status_code == 201


async def test_the_write_routes_need_the_operator_token_and_an_unknown_campaign_is_404(client, team):
    anonymous = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    async with anonymous:
        assert (await anonymous.post(_url(team.campaign), json={"role": "leader"})).status_code in (401, 403)
    assert (await client.get("/api/v1/agent-teams/campaigns/9999/asks")).status_code == 404


# Upgrade: a database from before the columns
async def test_an_old_database_gets_the_columns_and_its_versions_read_as_final(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database import Base, _sqlite_columns, _sqlite_migrate_snapshot_versions

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("DROP INDEX uix_campaign_snapshots_ask"))
            await conn.execute(text("ALTER TABLE campaign_snapshots DROP COLUMN purpose"))
            await conn.execute(text("ALTER TABLE campaign_snapshots DROP COLUMN fingerprint"))
            await conn.execute(text(
                "INSERT INTO campaign_snapshots (campaign_id, version, calculation_version, payload, created_by,"
                " created_at) VALUES (1, 1, 'campaign-snapshot-v3', '{}', 'system', '2026-11-20 09:00:00')"))
            assert {"purpose", "fingerprint"}.isdisjoint(await _sqlite_columns(conn, "campaign_snapshots"))
            for _ in range(2):  # and the migration is repeatable
                await _sqlite_migrate_snapshot_versions(conn)
            assert {"purpose", "fingerprint"} <= await _sqlite_columns(conn, "campaign_snapshots")
            row = (await conn.execute(text("SELECT purpose, fingerprint FROM campaign_snapshots"))).one()
            assert tuple(row) == ("final", None)
            names = [r[1] for r in (await conn.execute(text("PRAGMA index_list(campaign_snapshots)"))).all()]
            assert "uix_campaign_snapshots_ask" in names
    finally:
        await engine.dispose()

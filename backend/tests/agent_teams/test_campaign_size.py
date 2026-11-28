"""#1454 (#1246 part 3): campaign size in the snapshot (design §3 "Size", §6).

PRs merged are the distinct PR artifacts of the members' sourced merges in the campaign; a merge without its PR or its
time is unknown. Lines (additions plus deletions) and files (``changed_files``) come from each merged PR's R2 facts,
as their median and largest with n; a PR without complete facts is unknown with item time's reason names. Team size is
today's configured slot count, disabled slots included (slots keep no history), and model and member changes are
unknown, never zero, until #1455 records them. Agent Mail is team-wide: messages in the campaign sent by a team member or
delivered to one (a receipt, so a broadcast counts only where it reached the team), by the sender's role.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.models.database import (
    AgentTeamSlot,
    CampaignSnapshot,
    CampaignUpgradeCoverage,
    GithubWorkItem,
    MailExternalActor,
    MailMessage,
    MailReceipt,
    MailTeamMember,
)
from app.services import campaign_record_service as campaigns
from app.services import campaign_snapshot_service as snapshots
from app.services import factory_audit_service as audit
from tests.agent_teams.test_campaign_item_time import (
    CREATED,
    FACTS_FROM,
    _dispatch,
    _m,
    _marker,
)
from tests.agent_teams.test_campaign_record import (  # noqa: F401  (fixtures)
    FakeGithub,
    _issue,
    db,
    world,
)
from tests.agent_teams.test_campaign_snapshot import AS_OF, OPERATOR, T0

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fresh_cache():
    snapshots.clear_cache()
    yield
    snapshots.clear_cache()


async def _pr(db, world, item_id, number, minutes, *, pr=None, additions=30, deletions=10, files=4, facts=True,
              timed=True, artifact=True, name=None):
    """A sourced merge of PR ``pr`` (default: the issue number) and, when ``facts``, the R2 facts the watcher records."""
    pr = number if pr is None else pr
    name = (name or f"o/a1/pull/{pr}") if artifact else None
    await audit.record_delivery_fact(
        db, item_id=item_id, delivery_outcome="delivered", completion_kind="merged_code",
        fact_source="github_pull_request_merged", fact_time=_m(minutes) if timed else None, artifact=name,
        attempt=f"item:{item_id}:launch:1:revision:{minutes}:{name}")
    if facts and artifact:
        await audit.record_event(
            db, event_kind=audit.PULL_FACTS_KIND, source="test", occurred_at=_m(minutes), fact_time=_m(minutes),
            actor=audit.derive_actor(actor_kind="system"), scope_id=world.scopes["a1"], team_preset_id=world.team,
            context_snapshot={"artifact": name},
            after_values={"additions": additions, "deletions": deletions, "changed_files": files},
            action_outcome="applied", operation_id=f"pull_facts:{name}")


async def _size(db, world, items, *, facts_from=FACTS_FROM, cached=False, as_of=AS_OF):
    """``items``: (issue number, merge minutes or None, keyword arguments for ``_pr``); a repeated number is another
    delivery on the same issue."""
    await _marker(db)
    if facts_from is not None:
        db.add(CampaignUpgradeCoverage(id=1, started_at=facts_from))
    labelled, created = [], {}
    for number, merged, kwargs in items:
        item = created.get(number)
        if item is None:
            labelled.append(_issue("a1", number, state="closed"))
            item = created[number] = GithubWorkItem(
                scope_id=world.scopes["a1"], issue_number=number, issue_title="I", issue_url=f"u/{number}",
                github_updated_at=T0, dispatch_status="merged", created_at=CREATED)
            db.add(item)
            await db.flush()
            await _dispatch(db, item.id, 0)
        if merged is not None:
            await _pr(db, world, item.id, number, merged, **kwargs)
    await db.commit()
    campaign = await campaigns.current(db, world.team) or await campaigns.start(
        db, world.team, "label:v4.4", started_at=T0, actor=OPERATOR)
    return (await snapshots.build_snapshot(db, campaign, FakeGithub(labelled={"a1": labelled}), as_of=as_of,
                                           cached=cached)).size


async def _slot(db, preset, position):
    slot = AgentTeamSlot(preset_id=preset, position=position, display_name=f"S{position}", provider="codex-cli",
                         repo_id="r", repo_path="/r", repo_name="r", launch_mode="plain", launch_options={})
    db.add(slot)
    await db.flush()
    return slot


async def _member(db, preset, key, role):
    member = MailTeamMember(identity_key=key, repo_id="r", repo_path="/r", repo_name="r", display_name=key,
                            participant_kind="team_slot", team_preset_id=preset, role=role)
    db.add(member)
    await db.flush()
    return member.id


async def _mail(db, minutes, *, sender=None, actor=None, recipient=None, kind="member", audience=(None, None),
                receipts=(), received=None):
    """A message; ``receipts`` are the members it was delivered to (a direct recipient gets one too, as in Mail), at
    ``received`` (default: when it was sent)."""
    message = MailMessage(sender_member_id=sender, sender_actor_id=actor, recipient_member_id=recipient,
                          sender_type=kind, body_markdown="m", created_at=_m(minutes), audience_type=audience[0],
                          audience_id=audience[1], kind="broadcast" if recipient is None and receipts else "message")
    db.add(message)
    await db.flush()
    for member in {*receipts, *([recipient] if recipient is not None else [])}:
        db.add(MailReceipt(message_id=message.id, member_id=member, created_at=received or _m(minutes)))
    await db.flush()
    return message.id


# PRs merged, lines and files
async def test_prs_are_counted_once_each_with_lines_and_files_median_and_largest(db, world):
    size = await _size(db, world, [(10, 60, {"additions": 30, "deletions": 10, "files": 4}),
                                   (11, 70, {"additions": 300, "deletions": 100, "files": 12}),
                                   (12, 80, {"additions": 5, "deletions": 5, "files": 1}),
                                   (13, 90, {"pr": 10, "facts": False})])  # a second issue delivered by PR 10
    assert (size.prs_merged, size.unknown_prs) == (3, 0)
    assert (size.lines.items, size.lines.median, size.lines.largest, size.lines.coverage) == (3, 40, 400, "full")
    assert (size.files.items, size.files.median, size.files.largest) == (3, 4, 12)


@pytest.mark.parametrize(("kwargs", "reason"), [({"artifact": False}, "artifact_not_recorded"),
                                                ({"timed": False}, "merge_time_not_recorded"),
                                                ({"timed": False, "artifact": False}, "merge_time_not_recorded")])
async def test_a_merge_without_its_pr_or_time_is_an_unknown_pr(db, world, kwargs, reason):
    size = await _size(db, world, [(10, 60, {}), (11, 70, kwargs)])
    assert (size.prs_merged, size.unknown_prs, size.coverage) == (1, 1, "partial")
    assert reason in size.unknown_reasons


async def test_a_merge_outside_the_campaign_is_not_counted(db, world):
    size = await _size(db, world, [(10, -60, {}), (11, 60, {})])
    assert (size.prs_merged, size.lines.items) == (1, 1)


@pytest.mark.parametrize(("case", "reason"), [("missing", "pull_request_facts_missing"),
                                              ("incomplete", "pull_request_facts_incomplete"),
                                              ("before_recording", "merged_before_pull_facts_recording"),
                                              ("not_installed", "pull_facts_recording_not_installed")])
async def test_a_pr_without_complete_facts_is_unknown_with_its_reason(db, world, case, reason):
    kwargs = {"missing": {"facts": False}, "incomplete": {"deletions": None, "files": None}}.get(case, {})
    facts_from = {"before_recording": _m(65), "not_installed": None}.get(case, FACTS_FROM)
    size = await _size(db, world, [(10, 60, kwargs)], facts_from=facts_from)
    assert (size.prs_merged, size.lines.items, size.lines.unknown, size.lines.median) == (1, 0, 1, None)
    assert (size.lines.coverage, size.files.unknown) == ("unavailable", 1)
    assert reason in size.lines.unknown_reasons and reason in size.unknown_reasons


async def test_a_pr_with_lines_but_no_file_count_keeps_its_lines(db, world):
    size = await _size(db, world, [(10, 60, {"files": None})])
    assert (size.lines.items, size.files.items, size.files.unknown) == (1, 0, 1)
    assert size.files.unknown_reasons == ["pull_request_facts_incomplete"]


async def test_a_pr_recorded_in_another_case_counts_once_and_finds_its_facts(db, world):
    size = await _size(db, world, [(10, 60, {"name": "O/A1/pull/10"}),  # facts recorded under this spelling
                                   (13, 90, {"pr": 10, "facts": False})])
    assert (size.prs_merged, size.lines.items, size.lines.coverage) == (1, 1, "full")


async def test_facts_for_the_same_pr_in_another_team_lifetime_are_not_used(db, world):
    await audit.record_event(
        db, event_kind=audit.PULL_FACTS_KIND, source="test", occurred_at=_m(60), fact_time=_m(60),
        actor=audit.derive_actor(actor_kind="system"), scope_id=world.scopes["a1"], team_preset_id=world.team,
        team_context_key="team:replaced-lifetime", context_snapshot={"artifact": "o/a1/pull/10"},
        after_values={"additions": 1, "deletions": 1, "changed_files": 1}, action_outcome="applied",
        operation_id="pull_facts:other-lifetime")
    size = await _size(db, world, [(10, 60, {"facts": False})])
    assert (size.prs_merged, size.lines.items, size.lines.unknown_reasons) == (1, 0, ["pull_request_facts_missing"])


async def test_a_pr_merged_twice_is_measured_at_its_earliest_merge(db, world):
    # Recording began between the two merges: the first merge predates it, so the PR's facts are not trusted.
    size = await _size(db, world, [(10, 60, {}), (13, 90, {"pr": 10, "facts": False})], facts_from=_m(65))
    assert (size.prs_merged, size.lines.items) == (1, 0)
    assert size.lines.unknown_reasons == ["merged_before_pull_facts_recording"]


async def test_an_untimed_merge_of_another_pr_on_a_measured_issue_is_an_unknown_pr(db, world):
    """V1 review 478: issue 10 merged PR 15 at a known time and PR 16 at none; PR 16 must not vanish."""
    size = await _size(db, world, [(10, 60, {"pr": 15}), (10, 70, {"pr": 16, "timed": False, "facts": False})])
    assert (size.prs_merged, size.unknown_prs, size.prs_coverage, size.coverage) == (1, 1, "partial", "partial")
    assert "merge_time_not_recorded" in size.unknown_reasons


async def test_untimed_records_of_a_known_pr_are_not_unknown_and_one_untimed_pr_counts_once(db, world):
    size = await _size(db, world, [(10, 60, {"pr": 15}),
                                   (10, 70, {"pr": 15, "timed": False, "facts": False}),  # the same PR again
                                   (10, 72, {"artifact": False, "timed": False}),  # no PR, beside a known one
                                   (11, 75, {"pr": 15, "timed": False, "facts": False,  # PR 15 also delivered 11,
                                             "name": "O/A1/pull/15"}),  # in another case
                                   (12, 80, {"pr": 16, "timed": False, "facts": False}),
                                   (13, 90, {"pr": 16, "timed": False, "facts": False})])  # one PR, two issues
    assert (size.prs_merged, size.unknown_prs) == (1, 1)


async def test_an_untimed_merge_recorded_after_the_cutoff_is_not_an_unknown_pr(db, world):
    """With no merge time, the record time places the merge, as for item time."""
    size = await _size(db, world, [(10, 60, {"pr": 15}), (11, 70, {"pr": 16, "timed": False, "facts": False})],
                       as_of=_m(100))  # recorded now, after the cutoff
    assert (size.prs_merged, size.unknown_prs) == (1, 0)
    assert "merge_time_not_recorded" not in size.unknown_reasons


@pytest.mark.parametrize(("broken", "coverage"), [({"a2"}, "partial"), ({"a1", "a2"}, "unavailable")])
async def test_a_membership_read_gap_makes_the_size_partial_or_unavailable(db, world, broken, coverage):
    await _size(db, world, [(10, 60, {})])
    snapshot = await snapshots.build_snapshot(db, await campaigns.current(db, world.team),
                                              FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]},
                                                         broken=broken), as_of=AS_OF, cached=False)
    size = snapshot.size
    assert size.coverage == coverage
    assert any("a2" in reason for reason in size.unknown_reasons)
    # V1 review 478: the PR count is a lower bound or unknown, never a full zero, and so are lines and files
    assert (size.prs_merged, size.prs_coverage) == ((1, "partial") if coverage == "partial" else (None, "unavailable"))
    for measure in (size.lines, size.files):
        assert measure.coverage == coverage
        assert any("a2" in reason for reason in measure.unknown_reasons)


async def test_a_fully_read_empty_campaign_has_zero_prs_with_full_coverage(db, world):
    size = await _size(db, world, [])
    assert (size.prs_merged, size.prs_coverage, size.lines.items, size.lines.coverage) == (0, "full", 0, "full")


async def test_a_closed_issue_without_delivery_history_makes_the_pr_count_a_lower_bound(db, world):
    size = await _size(db, world, [(10, 60, {}), (11, None, {})])
    assert (size.prs_merged, size.unknown_prs, size.prs_coverage) == (1, 0, "partial")
    assert "delivery_history_missing" in size.unknown_reasons


# Team size, and model and member changes
async def test_team_size_is_todays_configured_slot_count_with_disabled_slots_and_changes_are_unknown(db, world):
    for position in range(3):
        await _slot(db, world.team, position)
    (await _slot(db, world.team, 3)).enabled = False  # configured but disabled: counted, and labelled so
    await _slot(db, world.other, 0)
    size = await _size(db, world, [(10, 60, {})])
    assert (size.team_size, size.team_size_basis) == (4, "current_configured_slots_including_disabled")
    assert (size.model_changes, size.member_changes, size.changes_reason) == (None, None, "slot_history_not_recorded")


async def test_a_retired_team_has_unknown_team_size_and_mail(db, world):
    await _slot(db, world.team, 0)
    leader = await _member(db, world.team, "leader", "leader")
    await _mail(db, 30, sender=leader)
    await _size(db, world, [(10, 60, {})])
    campaign = await campaigns.current(db, world.team)
    key = await audit.current_context_key(db, "team", world.team)
    await db.execute(text("UPDATE factory_context_keys SET retired_at = :at WHERE context_key = :key"),
                     {"at": _m(100), "key": key})
    await db.commit()
    snapshots.clear_cache()
    size = (await snapshots.build_snapshot(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]}),
                                           as_of=AS_OF, cached=False)).size
    assert (size.team_size, size.mail_messages, size.mail_by_role) == (None, None, [])
    assert "team_retired" in size.unknown_reasons


async def test_a_team_retired_after_the_membership_read_still_leaves_team_size_and_mail_unknown(db, world, monkeypatch):
    """The membership read saw the live team; the replacement lands before the size read. The size section's own
    lifetime check, not the membership gap, must mark it."""
    await _slot(db, world.team, 0)
    await _size(db, world, [(10, 60, {})])
    campaign = await campaigns.current(db, world.team)
    key = await audit.current_context_key(db, "team", world.team)
    original = campaigns.members

    async def then_retired(session, *args, **kwargs):
        read = await original(session, *args, **kwargs)
        await session.execute(text("UPDATE factory_context_keys SET retired_at = :at WHERE context_key = :key"),
                              {"at": _m(100), "key": key})
        return read

    monkeypatch.setattr(campaigns, "members", then_retired)
    snapshots.clear_cache()
    snapshot = await snapshots.build_snapshot(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]}),
                                              as_of=AS_OF, cached=False)
    assert "team_retired" not in snapshot.item_time.unknown_reasons  # the membership read saw the live team
    assert (snapshot.size.team_size, snapshot.size.mail_messages) == (None, None)
    assert "team_retired" in snapshot.size.unknown_reasons


# Agent Mail, team-wide, by sender role
async def test_mail_is_counted_team_wide_by_sender_role(db, world):
    leader = await _member(db, world.team, "leader", "leader")
    builder = await _member(db, world.team, "builder", "implementer")
    verifier = await _member(db, world.team, "verifier", "verifier")
    plain = await _member(db, world.team, "plain", None)
    stranger = await _member(db, world.other, "stranger", "implementer")
    supervisor = MailExternalActor(actor_key="sup", display_name="Supervisor", kind="supervisor", token_hash="h")
    db.add(supervisor)
    await db.flush()
    for minutes, kwargs in ((10, {"sender": leader, "recipient": builder}), (20, {"sender": builder, "recipient": leader}),
                            (30, {"sender": builder, "recipient": verifier}), (40, {"sender": verifier}),
                            (50, {"sender": plain, "recipient": leader}),
                            (60, {"actor": supervisor.id, "recipient": leader, "kind": "external_actor"}),
                            (70, {"recipient": builder, "kind": "system"}),
                            (80, {"sender": stranger, "recipient": builder}),  # to the team: counted
                            (90, {"sender": stranger}),  # another team's own message
                            (-60, {"sender": leader}), (11 * 24 * 60, {"sender": leader})):  # outside the window
        await _mail(db, minutes, **kwargs)
    size = await _size(db, world, [(10, 60, {})])
    assert size.mail_messages == 8
    assert {row.role: row.messages for row in size.mail_by_role} == {
        "leader": 1, "implementer": 3, "verifier": 1, "unknown": 1, "supervisor": 1, "system": 1}
    assert size.mail_scope == "team"


async def test_a_broadcast_counts_once_where_it_reached_a_team_member_and_not_merely_for_its_audience(db, world):
    leader = await _member(db, world.team, "leader", "leader")
    builder = await _member(db, world.team, "builder", "implementer")
    stranger = await _member(db, world.other, "stranger", "implementer")
    supervisor = MailExternalActor(actor_key="sup", display_name="Supervisor", kind="supervisor", token_hash="h")
    db.add(supervisor)
    await db.flush()
    team, other = ("team_preset", str(world.team)), ("team_preset", str(world.other))
    for minutes, kwargs in (
            (10, {"actor": supervisor.id, "kind": "external_actor", "audience": team, "receipts": [leader, builder]}),
            (20, {"kind": "system", "audience": team, "receipts": [leader, builder]}),
            (30, {"kind": "system", "audience": ("work_item", "1"), "receipts": [builder]}),
            (40, {"kind": "system", "audience": ("repository", "r"), "receipts": [leader, stranger]}),
            (50, {"kind": "director", "audience": ("operator_global", "global"), "receipts": [leader, stranger]}),
            (60, {"kind": "system", "audience": team}),  # addressed to the team, delivered to no one: not counted
            (70, {"kind": "system", "audience": other, "receipts": [stranger]}),  # another team's broadcast
            (80, {"kind": "director", "audience": ("operator_global", "global"), "receipts": [stranger]})):
        await _mail(db, minutes, **kwargs)
    message = await _mail(db, 90, kind="system", recipient=builder)  # addressed to a team member, its receipt lost
    await db.execute(text("DELETE FROM mail_receipts WHERE message_id = :id"), {"id": message})
    size = await _size(db, world, [(10, 60, {})])
    assert size.mail_messages == 6
    assert {row.role: row.messages for row in size.mail_by_role} == {"supervisor": 1, "system": 4, "director": 1}


@pytest.mark.parametrize(("after", "counted"), [(timedelta(0), 1), (timedelta(seconds=1), 0)])
async def test_a_broadcast_reaches_the_team_only_by_a_receipt_at_or_before_the_cutoff(db, world, after, counted):
    leader = await _member(db, world.team, "leader", "leader")
    await _mail(db, 30, kind="system", audience=("team_preset", str(world.team)), receipts=[leader],
                received=AS_OF + after)
    size = await _size(db, world, [(10, 60, {})])
    assert size.mail_messages == counted


# The cache
@pytest.mark.parametrize("change", ["message", "receipt", "slot", "role", "outside_role", "actor_kind"])
async def test_a_new_message_receipt_slot_or_role_misses_the_cache(db, world, change):
    leader = await _member(db, world.team, "leader", "leader")
    undelivered = await _mail(db, 30, kind="system", audience=("team_preset", str(world.team)))
    await _mail(db, 20, kind="system", recipient=leader)  # system already counts: a receipt adds no new sender
    stranger = await _member(db, world.other, "stranger", "leader")
    await _mail(db, 40, sender=stranger, recipient=leader)  # V1 review 478: inbound from another team
    supervisor = MailExternalActor(actor_key="sup", display_name="Supervisor", kind="supervisor", token_hash="h")
    db.add(supervisor)
    await db.flush()
    await _mail(db, 50, actor=supervisor.id, recipient=leader, kind="external_actor")
    first = await _size(db, world, [(10, 60, {})], cached=True)
    if change == "outside_role":
        await db.execute(text("UPDATE mail_team_members SET role = 'verifier' WHERE id = :id"), {"id": stranger})
    elif change == "actor_kind":
        await db.execute(text("UPDATE mail_external_actors SET kind = 'operator' WHERE id = :id"), {"id": supervisor.id})
    elif change == "message":  # from a sender already counted, so only the message count changes
        await _mail(db, 45, sender=stranger, recipient=leader)
    elif change == "receipt":  # an existing broadcast reaches the team: no new message row
        db.add(MailReceipt(message_id=undelivered, member_id=leader))
    elif change == "slot":
        await _slot(db, world.team, 0)
    else:
        await _mail(db, 30, sender=leader)
        await db.commit()
        first = (await snapshots.build_snapshot(db, await campaigns.current(db, world.team),
                                                FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]}),
                                                as_of=AS_OF, cached=True)).size
        await db.execute(text("UPDATE mail_team_members SET role = 'verifier' WHERE id = :id"), {"id": leader})
    await db.commit()
    again = (await snapshots.build_snapshot(db, await campaigns.current(db, world.team),
                                            FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]}),
                                            as_of=AS_OF, cached=True)).size
    assert again != first
    snapshots.clear_cache()
    assert again == (await snapshots.build_snapshot(db, await campaigns.current(db, world.team),
                                                    FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]}),
                                                    as_of=AS_OF, cached=False)).size  # the cached read is the fresh one


# Frozen snapshots
async def test_a_stored_v6_snapshot_without_the_section_reads_back_unchanged(db, world):
    await _size(db, world, [(10, 60, {})])
    campaign = await campaigns.current(db, world.team)
    snapshot = await snapshots.build_snapshot(db, campaign, FakeGithub(), as_of=AS_OF, cached=False)
    old = snapshot.model_dump(mode="json")
    old.pop("size")
    old["calculation_version"] = "campaign-snapshot-v6"
    db.add(CampaignSnapshot(campaign_id=campaign.id, version=1, calculation_version="campaign-snapshot-v6",
                            payload=old, created_by="operator"))
    await db.commit()
    frozen = await snapshots.freeze(db, campaign, FakeGithub(), actor=OPERATOR)
    assert (frozen.size, frozen.calculation_version) == (None, "campaign-snapshot-v6")
    assert (await snapshots.latest(db, campaign.id)).payload == old


async def test_a_new_freeze_stores_v7_with_the_section(db, world):
    await _size(db, world, [(10, 60, {})])
    campaign = await campaigns.current(db, world.team)
    frozen = await snapshots.freeze(db, campaign, FakeGithub(labelled={"a1": [_issue("a1", 10, state="closed")]}),
                                    actor=OPERATOR, as_of=AS_OF)
    stored = await snapshots.latest(db, campaign.id)
    assert (stored.calculation_version, frozen.calculation_version) == ("campaign-snapshot-v7",) * 2
    assert stored.payload["size"]["prs_merged"] == 1

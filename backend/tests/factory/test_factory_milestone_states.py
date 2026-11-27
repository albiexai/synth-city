"""#1337: items deferred by a milestone move and issues waiting for a team, read-only."""
from datetime import datetime, timedelta
from hashlib import sha256

import pytest

from sqlalchemy import select

from app.models.database import FactoryAuditEvent, GithubMilestoneWait, GithubWorkItem, TeamGithubScope
from app.services import milestone_state_service
from app.services.github_watcher_service import defer_milestone_exit

pytestmark = pytest.mark.asyncio

MOMENT = datetime(2026, 11, 26, 9, 0)


async def get_states(client, **params):
    response = await client.get("/api/v1/factory/milestone-states", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def add_wait(maker, scope_id, number, title, at=MOMENT):
    async with maker() as db:
        db.add(GithubMilestoneWait(scope_id=scope_id, issue_number=number,
                                   milestone_title=title, github_updated_at=at))
        await db.commit()


async def add_item(maker, scope_id, number, status="pending", url=None, **values):
    async with maker() as db:
        scope = await db.get(TeamGithubScope, scope_id)
        # The watcher stores the GitHub html_url of the issue.
        url = url or f"https://github.com/{scope.repo_owner}/{scope.repo_name}/issues/{number}"
        item = GithubWorkItem(scope_id=scope_id, issue_number=number, issue_title=f"Issue {number}",
                              issue_url=url, github_updated_at=MOMENT, dispatch_status=status, **values)
        db.add(item)
        await db.commit()
        return item.id


async def defer(maker, scope_id, item_id, title):
    async with maker() as db:
        scope = await db.get(TeamGithubScope, scope_id)
        item = await db.get(GithubWorkItem, item_id)
        await defer_milestone_exit(db, scope, item, title, "2026-11-26T09:30:00Z")


async def test_no_rows_gives_empty_lists_and_no_truncation(factory_client):
    body = await get_states(factory_client)
    assert body["waiting"] == [] and body["deferred"] == []
    assert body["waiting_truncated"] is False and body["deferred_truncated"] is False


async def test_a_wait_shows_once_for_a_repository_with_two_scopes(factory_client, factory_store):
    first, second = factory_store.ids.scopes[:2]
    await add_wait(factory_store.maker, first, 9001, "v4.5")
    await add_wait(factory_store.maker, second, 9001, "v4.5")
    rows = (await get_states(factory_client))["waiting"]
    assert len(rows) == 1
    row = rows[0]
    assert row["issue_number"] == 9001 and row["milestone"] == "v4.5"
    assert row["repository"]["github"] == {"owner": "fixture", "name": "shared"}


async def test_a_cleared_wait_does_not_show(factory_client, factory_store):
    await add_wait(factory_store.maker, factory_store.ids.scopes[0], 9002, None)
    assert (await get_states(factory_client))["waiting"] == []


async def test_a_deferral_shows_with_its_milestone_and_branch(factory_client, factory_store):
    scope = factory_store.ids.scopes[0]
    item_id = await add_item(factory_store.maker, scope, 9003, dispatch_head_ref="deck/slot-1/issue-9003")
    await defer(factory_store.maker, scope, item_id, "v4.5")
    rows = (await get_states(factory_client))["deferred"]
    assert len(rows) == 1
    row = rows[0]
    assert row["work_item_id"] == item_id and row["issue_number"] == 9003
    assert row["issue_title"] == "Issue 9003" and row["target_milestone"] == "v4.5"
    assert row["previous_branch"] == "deck/slot-1/issue-9003"
    assert row["team"]["id"] == factory_store.ids.teams[0]


async def test_an_issue_admitted_again_does_not_show_as_deferred(factory_client, factory_store):
    first, second = factory_store.ids.scopes[:2]
    item_id = await add_item(factory_store.maker, first, 9004)
    await defer(factory_store.maker, first, item_id, "v4.5")
    assert len((await get_states(factory_client))["deferred"]) == 1
    await add_item(factory_store.maker, second, 9004)
    assert (await get_states(factory_client))["deferred"] == []


async def test_a_merged_later_item_hides_the_deferral(factory_client, factory_store):
    first, second = factory_store.ids.scopes[:2]
    item_id = await add_item(factory_store.maker, first, 9005)
    await defer(factory_store.maker, first, item_id, "v4.5")
    assert len((await get_states(factory_client))["deferred"]) == 1
    await add_item(factory_store.maker, second, 9005, status="merged")
    assert (await get_states(factory_client))["deferred"] == []


async def test_a_cancelled_item_without_a_deferral_event_does_not_show(factory_client, factory_store):
    await add_item(factory_store.maker, factory_store.ids.scopes[0], 9006, status="cancelled")
    assert (await get_states(factory_client))["deferred"] == []


async def test_team_and_scope_filters(factory_client, factory_store):
    ids = factory_store.ids
    await add_wait(factory_store.maker, ids.scopes[0], 9007, "v4.5")
    await add_wait(factory_store.maker, ids.scopes[2], 9008, "v4.6")
    item_id = await add_item(factory_store.maker, ids.scopes[1], 9009)
    await defer(factory_store.maker, ids.scopes[1], item_id, "v4.7")
    everything = await get_states(factory_client)
    assert {r["issue_number"] for r in everything["waiting"]} == {9007, 9008}
    assert [r["issue_number"] for r in everything["deferred"]] == [9009]
    team_one = await get_states(factory_client, team_id=ids.teams[0])
    assert {r["issue_number"] for r in team_one["waiting"]} == {9007, 9008}
    assert team_one["deferred"] == []
    team_two = await get_states(factory_client, team_id=ids.teams[1])
    assert team_two["waiting"] == [] and [r["issue_number"] for r in team_two["deferred"]] == [9009]
    by_scope = await get_states(factory_client, scope_id=ids.scopes[2])
    assert [r["issue_number"] for r in by_scope["waiting"]] == [9008] and by_scope["deferred"] == []


async def test_unknown_and_mismatched_filters_use_the_factory_errors(factory_client, factory_store):
    missing = await factory_client.get("/api/v1/factory/milestone-states", params={"team_id": 999999})
    assert missing.status_code == 404
    mismatch = await factory_client.get("/api/v1/factory/milestone-states", params={
        "team_id": factory_store.ids.teams[1], "scope_id": factory_store.ids.scopes[0]})
    assert mismatch.status_code == 422


async def test_lists_are_bounded_and_say_when_they_are_cut(factory_client, factory_store, monkeypatch):
    monkeypatch.setattr(milestone_state_service, "ROW_LIMIT", 3)
    for number in range(9100, 9105):
        await add_wait(factory_store.maker, factory_store.ids.scopes[0], number, "v4.5")
    body = await get_states(factory_client)
    assert len(body["waiting"]) == 3 and body["waiting_truncated"] is True
    assert body["deferred_truncated"] is False


async def test_a_deferral_without_a_recorded_milestone_shows_a_null_milestone(factory_client, factory_store):
    from sqlalchemy import update
    from app.models.database import FactoryAuditEvent
    scope = factory_store.ids.scopes[0]
    item_id = await add_item(factory_store.maker, scope, 9010)
    await defer(factory_store.maker, scope, item_id, "v4.5")
    async with factory_store.maker() as db:
        await db.execute(update(FactoryAuditEvent).where(FactoryAuditEvent.item_id == item_id).values(
            after_values={"dispatch_status": "cancelled", "outcome": "deferred"}))
        await db.commit()
    rows = (await get_states(factory_client))["deferred"]
    assert len(rows) == 1 and rows[0]["target_milestone"] is None


# #1337, review 6074331482: a row keeps the repository it was recorded for, also after its scope moves.
DIRECTOR = {"x-synth-city-operator-token": "synthetic-factory-operator"}


async def move_scope(client, scope_id, **body):
    response = await client.patch(f"/api/v1/agent-teams/github-scopes/{scope_id}", json=body, headers=DIRECTOR)
    assert response.status_code == 200, response.text


async def idle_scope(maker, team_index, repo_name, ids):
    """A scope without items: the scope PATCH changes the repository of an idle scope only."""
    async with maker() as db:
        scope = TeamGithubScope(preset_id=ids.teams[team_index], repo_owner="fixture", repo_name=repo_name,
                                repo_path="/private/idle", dispatch_label="ready")
        db.add(scope)
        await db.commit()
        return scope.id


async def test_a_moved_scope_keeps_its_deferral_on_the_old_repository_and_drops_its_waits(factory_client, factory_store):
    scope = await idle_scope(factory_store.maker, 0, "orig", factory_store.ids)
    await add_wait(factory_store.maker, scope, 9111, "v4.5")
    item_id = await add_item(factory_store.maker, scope, 9112)
    await defer(factory_store.maker, scope, item_id, "v4.6")
    before = await get_states(factory_client)
    assert [r["repository"]["github"] for r in before["waiting"]] == [{"owner": "fixture", "name": "orig"}]
    assert [r["repository"]["github"] for r in before["deferred"]] == [{"owner": "fixture", "name": "orig"}]
    await move_scope(factory_client, scope, repo_name="renamed")
    after = await get_states(factory_client)
    # The wait belonged to the old repository. The deferral still names it, so its link still opens its issue.
    assert after["waiting"] == []
    assert [(r["repository"]["github"], r["issue_number"]) for r in after["deferred"]] == [
        ({"owner": "fixture", "name": "orig"}, 9112)]


async def test_waits_of_other_scopes_stay_when_one_scope_moves(factory_client, factory_store):
    first = await idle_scope(factory_store.maker, 0, "twin", factory_store.ids)
    second = await idle_scope(factory_store.maker, 1, "twin", factory_store.ids)
    await add_wait(factory_store.maker, first, 9113, "v4.5")
    await add_wait(factory_store.maker, second, 9113, "v4.5")
    await move_scope(factory_client, first, repo_name="elsewhere")
    rows = (await get_states(factory_client))["waiting"]
    assert [(r["repository"]["scope_id"], r["issue_number"]) for r in rows] == [(second, 9113)]


async def test_a_case_only_change_of_the_repository_keeps_the_waits(factory_client, factory_store):
    scope = await idle_scope(factory_store.maker, 0, "casey", factory_store.ids)
    await add_wait(factory_store.maker, scope, 9114, "v4.5")
    await move_scope(factory_client, scope, repo_owner="Fixture")
    assert [r["issue_number"] for r in (await get_states(factory_client))["waiting"]] == [9114]


async def test_an_item_of_the_new_repository_with_the_same_number_does_not_hide_the_deferral(factory_client, factory_store):
    old = await idle_scope(factory_store.maker, 0, "orig", factory_store.ids)
    other = await idle_scope(factory_store.maker, 1, "newer", factory_store.ids)
    item_id = await add_item(factory_store.maker, old, 9115)
    await defer(factory_store.maker, old, item_id, "v4.5")
    await move_scope(factory_client, old, repo_name="newer")
    await add_item(factory_store.maker, other, 9115)
    rows = (await get_states(factory_client))["deferred"]
    assert [(r["repository"]["github"]["name"], r["issue_number"]) for r in rows] == [("orig", 9115)]


async def test_an_item_with_the_same_issue_url_hides_the_deferral_even_in_another_scope(factory_client, factory_store):
    first, second = factory_store.ids.scopes[:2]
    item_id = await add_item(factory_store.maker, first, 9116)
    await defer(factory_store.maker, first, item_id, "v4.5")
    await add_item(factory_store.maker, second, 9116, url="https://github.com/Fixture/Shared/issues/9116")
    assert (await get_states(factory_client))["deferred"] == []


@pytest.mark.parametrize("url", [
    "u", "https://example.com/fixture/shared/issues/9117", "https://github.com/fixture/shared/pull/9117",
    "https://github.com/fixture/shared/issues/9118", "https://github.com/fixture/shared/issues/9117/extra",
])
async def test_a_deferral_whose_stored_url_does_not_name_its_issue_is_omitted(factory_client, factory_store, url):
    scope = factory_store.ids.scopes[0]
    item_id = await add_item(factory_store.maker, scope, 9117, url=url)
    await defer(factory_store.maker, scope, item_id, "v4.5")
    assert (await get_states(factory_client))["deferred"] == []


async def test_only_the_rows_of_the_list_are_built(factory_client, factory_store, monkeypatch):
    monkeypatch.setattr(milestone_state_service, "ROW_LIMIT", 3)
    for number in range(9200, 9212):
        await add_wait(factory_store.maker, factory_store.ids.scopes[0], number, "v4.5")
    built = []
    original = milestone_state_service.wire.WaitingIssue
    monkeypatch.setattr(milestone_state_service.wire, "WaitingIssue", lambda **row: built.append(row) or original(**row))
    body = await get_states(factory_client)
    assert len(body["waiting"]) == 3 and body["waiting_truncated"] is True
    # The list builds its rows and the one more that proves the cut, and no other row.
    assert len(built) == 4


# #1337, review 6074734411: a stored row that cannot be shown never takes the place of one that can.
async def test_an_invalid_newer_row_does_not_hide_a_valid_older_deferral_at_the_page_limit(factory_client, factory_store, monkeypatch):
    monkeypatch.setattr(milestone_state_service, "ROW_LIMIT", 1)
    scope = factory_store.ids.scopes[0]
    valid = await add_item(factory_store.maker, scope, 9301)
    await defer(factory_store.maker, scope, valid, "v4.5")
    bad = await add_item(factory_store.maker, scope, 9302, url="https://github.com/fixture/shared/extra/issues/9302")
    await defer(factory_store.maker, scope, bad, "v4.5")
    body = await get_states(factory_client)
    assert [r["issue_number"] for r in body["deferred"]] == [9301]
    assert body["deferred_truncated"] is False


async def test_invalid_rows_longer_than_one_batch_do_not_hide_a_valid_deferral(factory_client, factory_store, monkeypatch):
    monkeypatch.setattr(milestone_state_service, "ROW_LIMIT", 2)
    scope = factory_store.ids.scopes[0]
    valid = await add_item(factory_store.maker, scope, 9310)
    await defer(factory_store.maker, scope, valid, "v4.5")
    for number in range(9311, 9316):
        bad = await add_item(factory_store.maker, scope, number, url=f"https://github.com/fixture/shared/x/issues/{number}")
        await defer(factory_store.maker, scope, bad, "v4.5")
    body = await get_states(factory_client)
    assert [r["issue_number"] for r in body["deferred"]] == [9310]
    assert body["deferred_truncated"] is False


async def test_a_list_is_cut_only_by_rows_that_can_be_shown(factory_client, factory_store, monkeypatch):
    monkeypatch.setattr(milestone_state_service, "ROW_LIMIT", 2)
    scope = factory_store.ids.scopes[0]
    for number in (9320, 9321, 9322):
        item = await add_item(factory_store.maker, scope, number)
        await defer(factory_store.maker, scope, item, "v4.5")
    body = await get_states(factory_client)
    assert len(body["deferred"]) == 2 and body["deferred_truncated"] is True


async def test_a_later_item_with_another_number_does_not_hide_the_deferral(factory_client, factory_store):
    first, second = factory_store.ids.scopes[:2]
    item_id = await add_item(factory_store.maker, first, 9811)
    await defer(factory_store.maker, first, item_id, "v4.5")
    # The stored URL names issue 9811, but the item is issue 9812.
    await add_item(factory_store.maker, second, 9812, url="https://github.com/fixture/shared/issues/9811")
    assert [r["issue_number"] for r in (await get_states(factory_client))["deferred"]] == [9811]


async def test_a_wait_whose_scope_identity_is_refused_shows_without_a_repository(factory_client, factory_store):
    bad_identity = await idle_scope(factory_store.maker, 0, "bad name!", factory_store.ids)
    good = factory_store.ids.scopes[0]
    await add_wait(factory_store.maker, bad_identity, 9340, "v4.5")
    await add_wait(factory_store.maker, good, 9342, "v4.6")
    rows = (await get_states(factory_client))["waiting"]
    # The row stays in the list. It names no repository, because none can be proven.
    assert {r["issue_number"]: r["repository"] is None for r in rows} == {9340: True, 9342: False}


async def test_waits_that_are_not_waits_are_omitted_and_the_read_succeeds(factory_client, factory_store):
    good = factory_store.ids.scopes[0]
    await add_wait(factory_store.maker, good, 9341, "")
    await add_wait(factory_store.maker, good, 9343, "   ")
    await add_wait(factory_store.maker, good, 0, "v4.5")
    await add_wait(factory_store.maker, good, 9342, "v4.6")
    assert [r["issue_number"] for r in (await get_states(factory_client))["waiting"]] == [9342]


async def test_a_row_that_cannot_be_built_does_not_hide_a_valid_one_at_the_page_limit(factory_client, factory_store, monkeypatch):
    monkeypatch.setattr(milestone_state_service, "ROW_LIMIT", 1)
    good = factory_store.ids.scopes[0]
    await add_wait(factory_store.maker, good, 0, "v4.5", at=datetime(2026, 11, 26, 11, 0))
    await add_wait(factory_store.maker, good, 9351, "v4.5")
    body = await get_states(factory_client)
    assert [r["issue_number"] for r in body["waiting"]] == [9351] and body["waiting_truncated"] is False


async def test_an_unlinked_wait_counts_toward_the_cut(factory_client, factory_store, monkeypatch):
    monkeypatch.setattr(milestone_state_service, "ROW_LIMIT", 1)
    bad_identity = await idle_scope(factory_store.maker, 0, "bad name!", factory_store.ids)
    await add_wait(factory_store.maker, bad_identity, 9352, "v4.5", at=datetime(2026, 11, 26, 11, 0))
    await add_wait(factory_store.maker, factory_store.ids.scopes[0], 9353, "v4.5")
    body = await get_states(factory_client)
    assert [(r["issue_number"], r["repository"] is None) for r in body["waiting"]] == [(9352, True)]
    assert body["waiting_truncated"] is True


async def test_after_an_upgrade_the_waits_of_a_scope_that_changed_repository_show_without_a_repository(factory_client, factory_store):
    """The scope PATCH of the earlier version left waits behind. They carry no repository of their own.

    The GitHub update time of a wait does not say where the watcher saw the issue, so a wait of a scope with a
    repository change shows no link, whatever its time. The repository stamp that would prove it is #1372.
    """
    scope = await idle_scope(factory_store.maker, 0, "up-orig", factory_store.ids)
    await move_scope(factory_client, scope, repo_name="up-new")
    await add_wait(factory_store.maker, scope, 9501, "v4.5", at=MOMENT)
    # A GitHub clock ahead of the host gives a time after the change for a wait that is older than it.
    await add_wait(factory_store.maker, scope, 9502, "v4.5", at=datetime.utcnow() + timedelta(minutes=5))
    rows = {r["issue_number"]: r["repository"] for r in (await get_states(factory_client))["waiting"]}
    assert rows == {9501: None, 9502: None}


async def test_an_audited_move_with_equal_sanitized_names_is_still_a_move(factory_client, factory_store):
    """The ledger can store the same sanitized value for two different names. Its receipt still names the field."""
    prior, current = (sha256(label.encode()).hexdigest() for label in ("prior", "current"))
    scope = await idle_scope(factory_store.maker, 0, prior, factory_store.ids)
    await add_wait(factory_store.maker, scope, 9741, "v4.5", at=MOMENT)
    await move_scope(factory_client, scope, repo_name=current)
    async with factory_store.maker() as db:
        event = await db.scalar(select(FactoryAuditEvent).where(
            FactoryAuditEvent.scope_id == scope, FactoryAuditEvent.event_kind == "policy_change"))
        # The fixture holds only if the ledger cannot tell the two names apart.
        assert "repo_name" in event.context_snapshot["scope_changed_fields"]
        assert event.before_values["repo_name"] == event.after_values["repo_name"]
        await db.execute(GithubMilestoneWait.__table__.delete().where(GithubMilestoneWait.scope_id == scope))
        db.add(GithubMilestoneWait(scope_id=scope, issue_number=9741, milestone_title="v4.5", github_updated_at=MOMENT))
        await db.commit()
    rows = (await get_states(factory_client, scope_id=scope))["waiting"]
    assert [(r["issue_number"], r["repository"]) for r in rows] == [(9741, None)]


async def test_a_partly_sanitized_case_change_is_not_proof_of_a_case_only_edit(factory_client, factory_store):
    """The ledger can redact a part of each name. What is left may differ in case only while the names differ."""
    prior = "Build-" + sha256(b"old build").hexdigest()
    current = "build-" + sha256(b"new build").hexdigest()
    scope = await idle_scope(factory_store.maker, 0, prior, factory_store.ids)
    await add_wait(factory_store.maker, scope, 9744, "v4.5", at=MOMENT)
    await move_scope(factory_client, scope, repo_name=current)
    async with factory_store.maker() as db:
        event = await db.scalar(select(FactoryAuditEvent).where(
            FactoryAuditEvent.scope_id == scope, FactoryAuditEvent.event_kind == "policy_change"))
        before, after = event.before_values["repo_name"], event.after_values["repo_name"]
        # The fixture holds only if the stored values differ in case alone and are not the whole names.
        assert "repo_name" in event.context_snapshot["scope_changed_fields"]
        assert before != after and before.lower() == after.lower() and before != prior and after != current
        await db.execute(GithubMilestoneWait.__table__.delete().where(GithubMilestoneWait.scope_id == scope))
        db.add(GithubMilestoneWait(scope_id=scope, issue_number=9744, milestone_title="v4.5", github_updated_at=MOMENT))
        await db.commit()
    rows = (await get_states(factory_client, scope_id=scope))["waiting"]
    assert [(r["issue_number"], r["repository"]) for r in rows] == [(9744, None)]


async def test_an_unproven_wait_does_not_collapse_into_a_proven_wait_of_the_same_issue(factory_client, factory_store):
    old = await idle_scope(factory_store.maker, 0, "twin-old", factory_store.ids)
    current = await idle_scope(factory_store.maker, 1, "twin-new", factory_store.ids)
    await move_scope(factory_client, old, repo_name="twin-new")
    await add_wait(factory_store.maker, old, 9741, "old milestone", at=MOMENT)
    await add_wait(factory_store.maker, current, 9741, "current milestone", at=MOMENT + timedelta(seconds=1))
    # Both scopes now name the repository fixture/twin-new. Only the second wait has a proven repository.
    rows = (await get_states(factory_client))["waiting"]
    assert sorted((r["milestone"], r["repository"] and r["repository"]["display_name"]) for r in rows) == [
        ("current milestone", "fixture/twin-new"), ("old milestone", None)]
    assert [r["milestone"] for r in (await get_states(factory_client, scope_id=old))["waiting"]] == ["old milestone"]


async def test_waits_of_two_scopes_with_the_same_refused_identity_stay_separate(factory_client, factory_store):
    first = await idle_scope(factory_store.maker, 0, "bad name!", factory_store.ids)
    second = await idle_scope(factory_store.maker, 1, "bad name!", factory_store.ids)
    await add_wait(factory_store.maker, first, 9743, "first milestone", at=MOMENT)
    await add_wait(factory_store.maker, second, 9743, "second milestone", at=MOMENT + timedelta(seconds=1))
    rows = (await get_states(factory_client))["waiting"]
    assert sorted((r["milestone"], r["repository"]) for r in rows) == [("first milestone", None), ("second milestone", None)]


async def test_two_proven_waits_of_the_same_repository_and_issue_still_show_once(factory_client, factory_store):
    first = await idle_scope(factory_store.maker, 0, "pair", factory_store.ids)
    second = await idle_scope(factory_store.maker, 1, "pair", factory_store.ids)
    await add_wait(factory_store.maker, first, 9742, "v4.5", at=MOMENT)
    await add_wait(factory_store.maker, second, 9742, "v4.5", at=MOMENT + timedelta(seconds=1))
    assert [r["issue_number"] for r in (await get_states(factory_client))["waiting"]] == [9742]


@pytest.mark.parametrize("blank", ["  \t ", "\u00a0", "\u2003", "\u2028", " \u3000\n\u202f"])
async def test_a_newer_blank_observation_does_not_hide_a_valid_wait(factory_client, factory_store, blank):
    # The view and the ranking call the same titles blank, Unicode whitespace included.
    assert not blank.strip()
    first = await idle_scope(factory_store.maker, 0, "blank-twin", factory_store.ids)
    second = await idle_scope(factory_store.maker, 1, "blank-twin", factory_store.ids)
    await add_wait(factory_store.maker, first, 9760, "v4.5", at=MOMENT)
    await add_wait(factory_store.maker, second, 9760, blank, at=MOMENT + timedelta(seconds=1))
    assert [(r["issue_number"], r["milestone"]) for r in (await get_states(factory_client))["waiting"]] == [(9760, "v4.5")]


async def test_a_newer_row_with_no_issue_number_does_not_hide_a_valid_wait(factory_client, factory_store):
    first = await idle_scope(factory_store.maker, 0, "zero-twin", factory_store.ids)
    await add_wait(factory_store.maker, first, 9761, "v4.5", at=MOMENT)
    await add_wait(factory_store.maker, first, 0, "v4.5", at=MOMENT + timedelta(seconds=1))
    assert [r["issue_number"] for r in (await get_states(factory_client))["waiting"]] == [9761]


async def test_a_wait_stays_linked_when_the_scope_changed_something_other_than_its_repository(factory_client, factory_store):
    scope = await idle_scope(factory_store.maker, 0, "stays", factory_store.ids)
    await add_wait(factory_store.maker, scope, 9511, "v4.5", at=MOMENT)
    await move_scope(factory_client, scope, dispatch_label="ready-two")
    rows = (await get_states(factory_client))["waiting"]
    assert [(r["issue_number"], r["repository"]["github"]) for r in rows] == [(9511, {"owner": "fixture", "name": "stays"})]


async def test_a_case_only_change_of_the_repository_is_not_a_repository_change(factory_client, factory_store):
    scope = await idle_scope(factory_store.maker, 0, "casing", factory_store.ids)
    await add_wait(factory_store.maker, scope, 9512, "v4.5", at=MOMENT)
    await move_scope(factory_client, scope, repo_name="CASING")
    rows = (await get_states(factory_client))["waiting"]
    assert [(r["issue_number"], r["repository"] is not None) for r in rows] == [(9512, True)]


async def test_a_repository_change_of_another_scope_does_not_unlink_the_wait(factory_client, factory_store):
    other = await idle_scope(factory_store.maker, 0, "other-moves", factory_store.ids)
    kept = await idle_scope(factory_store.maker, 1, "kept-link", factory_store.ids)
    await add_wait(factory_store.maker, kept, 9513, "v4.5", at=MOMENT)
    await move_scope(factory_client, other, repo_name="other-moved")
    rows = (await get_states(factory_client))["waiting"]
    assert [(r["issue_number"], r["repository"] is not None) for r in rows] == [(9513, True)]


async def test_an_older_item_of_the_same_issue_does_not_hide_the_deferral(factory_client, factory_store):
    first, second = factory_store.ids.scopes[:2]
    await add_item(factory_store.maker, second, 9400, status="cancelled")
    item_id = await add_item(factory_store.maker, first, 9400)
    await defer(factory_store.maker, first, item_id, "v4.5")
    assert [r["issue_number"] for r in (await get_states(factory_client))["deferred"]] == [9400]

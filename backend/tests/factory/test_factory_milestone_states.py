"""#1337: items deferred by a milestone move and issues waiting for a team, read-only."""
from datetime import datetime

import pytest

from app.models.database import GithubMilestoneWait, GithubWorkItem, TeamGithubScope
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


async def add_item(maker, scope_id, number, status="pending", **values):
    async with maker() as db:
        item = GithubWorkItem(scope_id=scope_id, issue_number=number, issue_title=f"Issue {number}",
                              issue_url="u", github_updated_at=MOMENT, dispatch_status=status, **values)
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

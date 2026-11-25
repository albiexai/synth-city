"""#1238: the facts of each merged pull request are recorded once, after the merge that made them due."""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import text

from app.models.database import AgentTeamPreset, GithubWorkItem, TeamGithubScope
from app.services import factory_audit_service as audit
from app.services import factory_metrics_service as metrics
from app.services.github_rate_limit_service import GithubRateLimited
from app.services.github_verification_service import GithubVerificationService
from app.services.github_watcher_service import GithubWatcherService
from tests.factory.test_factory_audit_metrics import (  # noqa: F401  (fixtures and helpers of the audit tests)
    _install_marker, _seed_team, _seed_work, store,
)

pytestmark = pytest.mark.asyncio

REPO = "matrix-owner/matrix-repo"
MERGED_AT = "2026-10-07T09:00:00Z"
AUTOMATIC = "github_verification_service._process_review_item.auto_merge"
OBSERVED = "github_verification_service._verify_item"


def _pull(number, *, repo=REPO, head_repo=None, **values):
    return {
        "number": number, "state": "closed", "merged_at": MERGED_AT, "merged": True,
        "head": {"ref": "topic", "sha": "d" * 40, "repo": {"full_name": head_repo or repo}},
        "base": {"ref": "main", "repo": {"full_name": repo}},
        "additions": 120, "deletions": 30, "changed_files": 4, "commits": 3,
        "merged_by": {"login": "shared-account", "type": "User"}, **values,
    }


class _Client:
    """Answers get_pull from a table; an exception in the table is raised."""

    def __init__(self, pulls=None):
        self.pulls = pulls or {}
        self.reads = []

    async def get_pull(self, owner, repo, number, *, token=None):
        self.reads.append((f"{owner}/{repo}", number))
        answer = self.pulls[number] if number in self.pulls else _pull(number, repo=f"{owner}/{repo}")
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _start(store):
    await _install_marker(store)
    await _seed_team(store)


async def _merge(store, item_id, number, *, source=OBSERVED, seed=True, scope_id=None):
    if seed:
        await _seed_work(store, item_id=item_id, status="merged", pr_number=number)
    async with store() as db:
        item = await db.get(GithubWorkItem, item_id)
        if scope_id is not None:
            item.scope_id = scope_id
            await db.flush()
        await audit.record_merged_delivery(db, item, {"number": number, "merged_at": MERGED_AT}, source=source)
        await db.commit()


async def _second_scope(store):
    async with store() as db:
        first = await db.get(TeamGithubScope, 5)
        db.add(TeamGithubScope(
            id=6, preset_id=first.preset_id, repo_owner="other-owner", repo_name="other-repo",
            repo_path="/tmp/other-repo", base_ref="origin/main", github_auth_mode="ambient", merge_policy="human"))
        await db.commit()


async def _poll(store, client, *, service=None, scope_id=5):
    service = service or GithubWatcherService()
    async with store() as db:
        scope = await db.get(TeamGithubScope, scope_id)
        await service._record_due_pull_facts(db, scope, client)
    return service


async def _facts(store):
    async with store() as db:
        rows = (await db.execute(text(
            "SELECT * FROM factory_audit_events WHERE event_kind = 'pull_request_facts' ORDER BY id"))).mappings().all()
    return [dict(row, after=json.loads(row["after_values"]), snapshot=json.loads(row["context_snapshot"]))
            for row in rows]


async def _deliveries(store):
    async with store() as db:
        return [dict(row) for row in (await db.execute(text(
            "SELECT * FROM factory_audit_events WHERE event_kind = 'delivery_evidence' ORDER BY id"))).mappings().all()]


# 1
async def test_a_merge_marks_its_facts_due_in_the_merge_transaction(store):
    await _start(store)
    await _merge(store, 201, 71)
    async with store() as db:
        assert (await db.execute(text(
            "SELECT json_extract(context_snapshot, '$.pull_facts') FROM factory_audit_events"
            " WHERE event_kind = 'delivery_evidence'"))).scalar_one() == "due"


async def test_a_rolled_back_merge_leaves_nothing_due(store):
    await _start(store)
    await _seed_work(store, item_id=202, status="verifying", pr_number=72)
    async with store() as db:
        item = await db.get(GithubWorkItem, 202)
        await audit.record_merged_delivery(db, item, {"number": 72, "merged_at": MERGED_AT}, source=OBSERVED)
        await db.rollback()
    client = _Client()
    await _poll(store, client)
    assert client.reads == [] and await _facts(store) == []


# 2
async def test_a_due_pull_gets_one_event_with_every_value(store):
    await _start(store)
    await _merge(store, 203, 73)
    await _poll(store, _Client())
    (fact,) = await _facts(store)
    assert fact["after"] == {
        "additions": 120, "deletions": 30, "changed_files": 4, "commit_count": 3,
        "merged_by": "shared-account", "merged_by_type": "User", "merge_path": "unknown"}
    assert fact["snapshot"]["artifact"] == f"{REPO}/pull/73"
    assert fact["snapshot"]["attempt"] == "item:203:launch:None:revision:None"
    assert fact["fact_time"].startswith("2026-10-07 09:00:00")
    assert (fact["scope_id"], fact["team_preset_id"]) == (5, 7)
    assert fact["delivery_outcome"] is None and fact["completion_kind"] is None


# 3
async def test_repeats_add_nothing(store):
    await _start(store)
    await _merge(store, 204, 74)
    client = _Client()
    await _poll(store, client)
    await _poll(store, client)
    await _merge(store, 204, 74, source="factory_maintenance.finalize_merged_item", seed=False)
    await _merge(store, 205, 74)  # another work item delivered by the same pull
    await _poll(store, client)
    assert len(await _deliveries(store)) == 3
    assert len(await _facts(store)) == 1
    assert len(client.reads) == 1


async def test_two_due_deliveries_of_one_pull_are_read_and_recorded_once(store):
    await _start(store)
    await _merge(store, 206, 75)
    await _merge(store, 207, 75)
    client = _Client()
    await _poll(store, client)
    assert len(client.reads) == 1 and len(await _facts(store)) == 1


async def test_the_ledger_refuses_a_second_event_for_one_pull(store):
    await _start(store)
    await _merge(store, 208, 76)
    async with store() as db:
        (due,) = await audit.due_pull_facts(db, 5)
        first = await audit.record_pull_request_facts(db, scope_id=5, due=due, pull=_pull(76))
        again = await audit.record_pull_request_facts(db, scope_id=5, due=due, pull=_pull(76, additions=999))
        assert again.id == first.id
        await db.commit()
    (fact,) = await _facts(store)
    assert fact["after"]["additions"] == 120
    assert fact["replay_key"]


# 4
async def test_each_watched_repository_records_its_own_pull(store):
    await _start(store)
    await _second_scope(store)
    await _merge(store, 209, 77)
    await _merge(store, 210, 77, scope_id=6)
    client = _Client()
    await _poll(store, client)
    other = _Client({77: _pull(77, repo="other-owner/other-repo", additions=9,
                               merged_by={"login": "someone", "type": "User"})})
    await _poll(store, other, scope_id=6)
    facts = await _facts(store)
    assert [(f["scope_id"], f["snapshot"]["artifact"], f["after"]["additions"], f["after"]["merged_by"])
            for f in facts] == [
        (5, f"{REPO}/pull/77", 120, "shared-account"),
        (6, "other-owner/other-repo/pull/77", 9, "someone")]
    assert client.reads == [(REPO, 77)] and other.reads == [("other-owner/other-repo", 77)]


# 5
@pytest.mark.parametrize("bad", ["missing", None, -1, True, "12", 1.5])
async def test_an_unusable_number_is_unknown_and_never_zero(store, bad):
    await _start(store)
    await _merge(store, 211, 78)
    pull = _pull(78, deletions=0)
    for name in ("additions", "changed_files", "commits"):
        if bad == "missing":
            pull.pop(name)
        else:
            pull[name] = bad
    await _poll(store, _Client({78: pull}))
    (fact,) = await _facts(store)
    assert (fact["after"]["additions"], fact["after"]["changed_files"], fact["after"]["commit_count"]) == (None,) * 3
    assert fact["after"]["deletions"] == 0  # a real zero stays zero
    for name in ("additions", "changed_files", "commit_count"):
        assert name in fact["sanitized_reason"]
    assert "deletions" not in fact["sanitized_reason"]


@pytest.mark.parametrize("merged_by", [None, "missing", {}, {"login": ""}, {"login": 5, "type": 7}, "text"])
async def test_an_absent_merger_is_null(store, merged_by):
    await _start(store)
    await _merge(store, 212, 79)
    pull = _pull(79, merged_by=merged_by)
    if merged_by == "missing":
        pull.pop("merged_by")
    await _poll(store, _Client({79: pull}))
    (fact,) = await _facts(store)
    assert fact["after"]["merged_by"] is None and fact["after"]["merged_by_type"] is None
    assert "merged_by" in fact["sanitized_reason"]


# 6
async def test_a_confirmed_automatic_merge_is_automatic(store):
    await _start(store)
    await _merge(store, 213, 80, source=AUTOMATIC)
    await _poll(store, _Client())
    (fact,) = await _facts(store)
    assert fact["after"]["merge_path"] == "automatic"
    assert fact["after"]["merged_by"] == "shared-account"


async def test_the_confirmed_source_is_the_one_the_merge_path_writes():
    assert audit.AUTOMATIC_MERGE_SOURCE == AUTOMATIC
    assert f'source="{AUTOMATIC}"' in inspect.getsource(GithubVerificationService)


@pytest.mark.parametrize("values", [
    {"merged_by": {"login": "a-person", "type": "User"}},
    {"merged_by": {"login": "some[bot]", "type": "Bot"}},
    {"auto_merge": {"enabled_by": {"login": "a-person"}, "merge_method": "merge"}},
    {},
    {"merged_by": None},
])
async def test_nothing_is_inferred_about_an_unconfirmed_merge(store, values):
    await _start(store)
    await _merge(store, 214, 81)
    await _poll(store, _Client({81: _pull(81, **values)}))
    (fact,) = await _facts(store)
    assert fact["after"]["merge_path"] == "unknown"


async def test_one_login_shared_by_the_factory_and_a_person_classifies_only_the_confirmed_merge(store):
    await _start(store)
    await _merge(store, 238, 104, source=AUTOMATIC)
    await _merge(store, 239, 105)  # a person merged it with the factory's account
    await _poll(store, _Client())
    assert [(f["after"]["merged_by"], f["after"]["merged_by_type"], f["after"]["merge_path"])
            for f in await _facts(store)] == [
        ("shared-account", "User", "automatic"), ("shared-account", "User", "unknown")]


async def test_another_pulls_automatic_merge_confirms_nothing(store):
    await _start(store)
    await _second_scope(store)
    await _merge(store, 215, 82, source=AUTOMATIC)
    await _merge(store, 216, 83)
    await _merge(store, 217, 82, scope_id=6)
    service = await _poll(store, _Client())
    await _poll(store, _Client(), service=service, scope_id=6)
    paths = {f["snapshot"]["artifact"]: f["after"]["merge_path"] for f in await _facts(store)}
    assert paths == {f"{REPO}/pull/82": "automatic", f"{REPO}/pull/83": "unknown",
                     "other-owner/other-repo/pull/82": "unknown"}


async def test_two_teams_on_one_repository_keep_their_own_record_and_evidence(store):
    await _start(store)
    async with store() as db:
        team = AgentTeamPreset(id=8, name="Second team", created_by="fixture")
        db.add(team)
        await db.flush()
        db.add(TeamGithubScope(
            id=6, preset_id=8, repo_owner="matrix-owner", repo_name="matrix-repo", repo_path="/tmp/matrix-repo",
            base_ref="origin/main", github_auth_mode="ambient", merge_policy="human"))
        await db.commit()
    await _merge(store, 235, 102, source=AUTOMATIC)
    await _merge(store, 237, 103, scope_id=6)
    service = await _poll(store, _Client())
    client = _Client()
    await _poll(store, client, service=service, scope_id=6)
    assert client.reads == [(REPO, 103)]  # the other team's delivery of pull 102 is not this team's
    await _merge(store, 236, 102, scope_id=6)
    await _poll(store, _Client(), service=service, scope_id=6)
    assert [(f["scope_id"], f["team_preset_id"], f["after"]["merge_path"]) for f in await _facts(store)] == [
        (5, 7, "automatic"), (6, 8, "unknown"), (6, 8, "unknown")]


# 7
def _http_error(status):
    request = httpx.Request("GET", "https://api.github.com/x")
    return httpx.HTTPStatusError("refused", request=request, response=httpx.Response(status, request=request))


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), _http_error(502), ValueError("bad shape")])
async def test_a_failed_read_writes_nothing_and_the_next_poll_records(store, error):
    await _start(store)
    await _merge(store, 218, 84)
    before = await _deliveries(store)
    async with store() as db:
        item_before = (await db.execute(text("SELECT * FROM github_work_items"))).mappings().all()
    service = await _poll(store, _Client({84: error}))
    assert await _facts(store) == [] and await _deliveries(store) == before
    async with store() as db:
        assert (await db.execute(text("SELECT * FROM github_work_items"))).mappings().all() == item_before
    await _poll(store, _Client(), service=service)
    assert len(await _facts(store)) == 1


@pytest.mark.parametrize("stopped", ["before the read", "after the read"])
async def test_a_crash_leaves_the_facts_due_for_the_next_process(store, monkeypatch, stopped):
    await _start(store)
    await _merge(store, 240, 106)

    async def stop(*_args, **_kwargs):
        raise asyncio.CancelledError

    with monkeypatch.context() as patched:
        client = _Client()
        if stopped == "before the read":
            patched.setattr(client, "get_pull", stop)
        else:
            patched.setattr(audit, "record_pull_request_facts", stop)
        with pytest.raises(asyncio.CancelledError):
            await _poll(store, client)
    assert await _facts(store) == []
    await _poll(store, _Client())  # a new service: nothing in memory survives
    await _poll(store, _Client())
    assert len(await _facts(store)) == 1


async def test_a_failed_read_does_not_stop_the_other_pulls(store):
    await _start(store)
    await _merge(store, 219, 85)
    await _merge(store, 220, 86)
    await _poll(store, _Client({85: httpx.ReadTimeout("slow")}))
    assert [f["snapshot"]["artifact"] for f in await _facts(store)] == [f"{REPO}/pull/86"]


# 8
async def test_a_rate_limit_stops_the_batch_and_the_rest_stays_due(store):
    await _start(store)
    for offset in range(3):
        await _merge(store, 221 + offset, 87 + offset)
    limited = GithubRateLimited(datetime.now(timezone.utc) + timedelta(minutes=5),
                                httpx.Request("GET", "https://api.github.com/x"))
    client = _Client({88: limited})
    service = await _poll(store, client)
    assert [number for _, number in client.reads] == [87, 88]
    await _poll(store, _Client(), service=service)
    assert sorted(f["snapshot"]["artifact"] for f in await _facts(store)) == [
        f"{REPO}/pull/{number}" for number in (87, 88, 89)]


# 9
async def test_failing_pulls_do_not_starve_the_others(store, monkeypatch):
    import app.services.github_watcher_service as watcher
    assert watcher._PULL_FACTS_BATCH == 20
    monkeypatch.setattr(watcher, "_PULL_FACTS_BATCH", 2)
    await _start(store)
    for offset in range(3):
        await _merge(store, 224 + offset, 91 + offset)
    client = _Client({91: httpx.ReadTimeout("slow"), 92: httpx.ReadTimeout("slow")})
    service = await _poll(store, client)
    assert len(client.reads) == 2
    await _poll(store, client, service=service)
    assert [f["snapshot"]["artifact"] for f in await _facts(store)] == [f"{REPO}/pull/93"]


# 10
@pytest.mark.parametrize("pull", [
    _pull(999),
    _pull(94, repo="another/repository"),
    _pull(94, merged_at=None),
    _pull(94, state="open", merged_at=None),
    _pull(94, base=None),
    "text",
])
async def test_a_mismatched_answer_records_nothing(store, pull):
    await _start(store)
    await _merge(store, 227, 94)
    service = await _poll(store, _Client({94: pull}))
    assert await _facts(store) == []
    await _poll(store, _Client(), service=service)
    assert len(await _facts(store)) == 1


async def test_a_fork_head_is_accepted(store):
    await _start(store)
    await _merge(store, 228, 95)
    await _poll(store, _Client({95: _pull(95, head_repo="a-fork/matrix-repo")}))
    assert len(await _facts(store)) == 1


async def test_a_renamed_scope_repository_is_not_read_for_the_old_artifact(store):
    await _start(store)
    await _merge(store, 229, 96)
    async with store() as db:
        await db.execute(text("UPDATE team_github_scopes SET repo_name = 'renamed' WHERE id = 5"))
        await db.commit()
    client = _Client()
    await _poll(store, client)
    assert client.reads == [] and await _facts(store) == []


async def test_a_repository_changed_during_the_read_records_nothing(store):
    await _start(store)
    await _merge(store, 230, 97)

    class Renames(_Client):
        async def get_pull(self, owner, repo, number, *, token=None):
            async with store() as other:
                await other.execute(text("UPDATE team_github_scopes SET repo_name = 'renamed' WHERE id = 5"))
                await other.commit()
            return await super().get_pull(owner, repo, number, token=token)

    await _poll(store, Renames())
    assert await _facts(store) == []


# 11
async def test_an_older_delivery_without_the_marker_is_not_read(store):
    await _start(store)
    await _seed_work(store, item_id=231, status="merged", pr_number=98)
    async with store() as db:
        await audit.record_delivery_fact(
            db, item_id=231, scope_id=5, delivery_outcome="delivered", completion_kind="merged_code",
            fact_source="github_pull_request_merged", fact_time=None, artifact=f"{REPO}/pull/98",
            attempt="item:231:launch:None:revision:None")
        await db.commit()
    client = _Client()
    await _poll(store, client)
    assert client.reads == [] and await _facts(store) == []


# 12
async def test_a_deleted_work_item_still_gets_its_facts(store):
    await _start(store)
    await _merge(store, 232, 99)
    async with store() as db:
        await db.execute(text("DELETE FROM github_work_items WHERE id = 232"))
        await db.commit()
    await _poll(store, _Client())
    (fact,) = await _facts(store)
    assert fact["snapshot"]["artifact"] == f"{REPO}/pull/99"


# 13
class _PollClient(_Client):
    async def list_issues_with_label(self, owner, repo, label):
        return []

    async def list_repo_labels(self, owner, repo):
        return []

    async def get_issues_by_number(self, owner, repo, numbers):
        return {}

    async def get_open_issues_by_number(self, owner, repo, numbers):
        return {}


async def test_the_poll_records_due_facts(store):
    await _start(store)
    await _merge(store, 233, 100)
    async with store() as db:
        scope = await db.get(TeamGithubScope, 5)
        await GithubWatcherService().poll_scope(db, scope, _PollClient())
    assert len(await _facts(store)) == 1


async def test_a_failing_step_never_blocks_the_poll(store, monkeypatch):
    await _start(store)
    reached = []

    async def broken(*_args, **_kwargs):
        raise RuntimeError("broken step")

    async def refresh(db, scope, client):
        reached.append(scope.id)  # the scope is still readable after the recovery

    from app.services.agent_team_campaign_service import agent_team_campaign_service
    monkeypatch.setattr(GithubWatcherService, "_record_due_pull_facts", broken)
    monkeypatch.setattr(agent_team_campaign_service, "refresh_scope", refresh)
    async with store() as db:
        scope = await db.get(TeamGithubScope, 5)
        await GithubWatcherService().poll_scope(db, scope, _PollClient())
    assert reached == [5]


# 14
async def test_the_delivery_metrics_do_not_change(store):
    await _start(store)
    await _merge(store, 234, 101)
    window = {"window_start": datetime(2026, 10, 7), "window_end": datetime(2026, 10, 8)}

    async def values():
        async with store() as db:
            return [(m.name, m.value, m.sample_count, m.unknown_count)
                    for m in (await metrics.build_metrics_window(db, **window)).metrics]

    before = await values()
    assert ("delivered_in_window", 1.0) in [(name, value) for name, value, *_ in before]
    await _poll(store, _Client())
    assert len(await _facts(store)) == 1
    assert await values() == before

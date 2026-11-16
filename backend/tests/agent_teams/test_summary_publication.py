"""#940 phase 1: generated review and remaining-work blocks, published as a system-owned PR comment.

The system never edits a PR or issue body (GitHub has no conditional body write), so no operator record or
hand-written block can be overwritten. The comment is a pure function of recorded facts.
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.database import GithubApprovalRequest, GithubHeadReview, GithubWorkItem, TeamGithubScope
from app.services import github_summary_publication_service as module
from app.services.github_client import GithubClient
from app.services.github_summary_publication_service import (
    SummaryFacts, github_summary_publication_service as service, render_comment, render_digest,
)
from tests.agent_mail.test_api import _dispatch_approval_fixture
from tests.agent_teams.test_head_acceptance import slot_member

HEAD = "a" * 40
OTHER = "b" * 40
BOT = "synth-city-app[bot]"
EVIDENCE = "https://github.com/o/r/pull/12#pullrequestreview-77"


def facts(**overrides):
    base = dict(
        repo="o/r", pull_number=12, pull_url="https://github.com/o/r/pull/12", base="release/v4.2", head_sha=HEAD,
        state="open", draft=False, merge_sha=None, hold_label="synth-city-v42-review-hold", hold_present=False,
        checks=(("Full backend", "success"), ("e2e", "success")), review=("accepted", EVIDENCE), tracked=True,
        issue_number=940, plan=(339, 3), effort=(60, 0, 12), merge_policy="auto", item_status="ready_for_review")
    base.update(overrides)
    return SummaryFacts(**base)


# --- the renderer is a pure function of the facts ----------------------------------------------------------------


def test_the_rendered_comment_is_exactly_the_golden_text_for_fixed_facts():
    text = render_comment(facts())
    assert text == (
        "<!-- deck:summary-comment -->\n"
        "Generated and owned by Synth City from recorded facts. It is rewritten when the facts change, so an edit "
        "here is overwritten. It does not replace the owner's summary in the PR and issue bodies and grants no "
        "approval, review or merge authority.\n\n"
        "<!-- deck:human-review-summary:start -->\n"
        "## Human review summary (generated)\n\n"
        f"- **Pull request:** https://github.com/o/r/pull/12 into `release/v4.2`, head `{HEAD}`, ready for review.\n"
        "- **Required checks:** Full backend: success; e2e: success.\n"
        f"- **Head review:** accepted at this exact head ({EVIDENCE}).\n"
        "- **Review hold label:** absent.\n"
        "- **Approved plan:** request 339, round 3.\n"
        "- **Recorded effort limit (advisory ledger; not an estimate of remaining work):** 60 minutes; "
        "ledger 12 minutes.\n"
        "- **Requested action:** none from a human; the configured auto merge applies when every gate is met.\n"
        "<!-- deck:human-review-summary:end -->\n\n"
        "<!-- deck:work-remaining:start -->\n"
        "## Work remaining\n\n"
        "- **Remaining:** Complete the configured merge and closeout.\n"
        "- **Estimate:** Unknown.\n"
        "- **Next:** The configured merge applies; then the owner closes out.\n"
        "<!-- deck:work-remaining:end -->\n")


def test_the_same_facts_render_the_same_text_and_a_new_head_changes_the_digest():
    assert render_comment(facts()) == render_comment(facts())
    assert render_digest(facts()) == render_digest(facts())
    assert render_digest(facts(head_sha=OTHER)) != render_digest(facts())
    assert OTHER in render_comment(facts(head_sha=OTHER)) and HEAD not in render_comment(facts(head_sha=OTHER))


@pytest.mark.parametrize("change", [
    dict(checks=(("Full backend", "failure"), ("e2e", "success"))), dict(review=None), dict(draft=True),
    dict(hold_present=True), dict(state="merged", merge_sha=OTHER), dict(plan=None), dict(effort=None)])
def test_each_recorded_fact_changes_the_blocks(change):
    assert render_digest(facts(**change)) != render_digest(facts())


def test_the_estimate_is_unknown_and_the_limit_is_never_an_estimate_of_remaining_work():
    for effort in ((60, 0, 12), (240, 30, 300), None):
        text = render_comment(facts(effort=effort))
        block = text[text.index("<!-- deck:work-remaining:start -->"):]
        assert "- **Estimate:** Unknown.\n" in block
        assert not re.search(r"\d+ minutes", block), block
    text = render_comment(facts(effort=(60, 30, 12)))
    assert "advisory ledger; not an estimate of remaining work" in text and "60 minutes (+30 extension)" in text


def test_a_merged_pull_shows_the_merge_commit_and_nothing_left_to_decide():
    text = render_comment(facts(state="merged", merge_sha=OTHER, item_status="merged"))
    assert f"merged as `{OTHER}`" in text and "- **Requested action:** none." in text
    assert "- **Remaining:** Closeout." in text


def test_a_draft_pull_states_it_is_a_draft_and_a_hold_label_is_reported():
    text = render_comment(facts(draft=True, hold_present=True, review=None))
    assert "a draft" in text and "- **Review hold label:** present." in text
    assert "- **Head review:** none recorded for this head." in text


def test_a_standalone_pull_marks_what_does_not_exist_as_not_recorded_and_invents_nothing():
    text = render_comment(facts(tracked=False, issue_number=None, plan=None, effort=None, review=None,
                                item_status=None))
    assert "- **Approved plan:** not recorded." in text
    assert "- **Recorded effort limit (advisory ledger; not an estimate of remaining work):** not recorded." in text
    assert "- **Head review:** not recorded." in text


@pytest.mark.parametrize("secret", ["ghp_" + "a" * 36, "github_pat_" + "b" * 30, "lease_token=abc"])
def test_a_private_value_in_a_fact_is_refused_not_published(secret):
    with pytest.raises(module.SummaryRenderError):
        render_comment(facts(pull_url=f"https://example.test/{secret}"))


def test_the_output_never_contains_a_nonce_a_lease_or_a_token_shape():
    text = render_comment(facts())
    assert not re.search(r"[a-f0-9]{16,}", text.replace(HEAD, "").replace(OTHER, ""))
    assert not re.search(r"\b(nonce|lease|token)\b", text.lower())


# --- the client has no body write ---------------------------------------------------------------------------------


def test_the_github_client_has_no_call_that_edits_a_pull_request_or_issue_body():
    names = [name for name in dir(GithubClient) if not name.startswith("__")]
    assert not [name for name in names if "body" in name.lower()], names
    assert {"list_issue_comments", "get_issue_comment", "create_issue_comment", "update_issue_comment"} <= set(names)


# --- publication: a system-owned comment --------------------------------------------------------------------------


class FakeGithub:
    def __init__(self, **pull):
        self.pull = {"number": 12, "state": "open", "draft": False, "merged": False, "merge_commit_sha": None,
                     "html_url": "https://github.com/o/r/pull/12", "base": {"ref": "release/v4.2"},
                     "head": {"sha": HEAD}, "labels": [], "body": "OWNER BODY\n"}
        self.pull.update(pull)
        self.comments: dict[int, dict] = {}
        self.next_id = 1000
        self.calls: list[tuple] = []
        self.tokens: list = []
        self.fail_write = False
        self.checks = [{"name": "ci", "status": "completed", "conclusion": "success", "head_sha": HEAD,
                        "app": {"slug": "github-actions"}}]

    def _seen(self, token):
        self.tokens.append(token)

    @staticmethod
    def _issue_url(number):
        return f"https://api.github.com/repos/o/r/issues/{number}"

    async def get_pull(self, owner, repo, number, *, token=None):
        self._seen(token)
        self.calls.append(("get_pull", number))
        return dict(self.pull)

    async def list_check_runs_for_ref(self, owner, repo, ref, *, token=None):
        self._seen(token)
        self.calls.append(("checks", ref))
        return list(self.checks)

    async def list_issue_comments(self, owner, repo, number, *, token=None):
        self._seen(token)
        self.calls.append(("list_comments", number))
        return [dict(comment) for comment in self.comments.values()
                if comment.get("issue_url", self._issue_url(number)).endswith(f"/issues/{number}")]

    async def get_issue_comment(self, owner, repo, comment_id, *, token=None):
        self._seen(token)
        self.calls.append(("get_comment", comment_id))
        return dict(self.comments[comment_id]) if comment_id in self.comments else None

    async def create_issue_comment(self, owner, repo, number, body, *, token=None):
        self._seen(token)
        self.calls.append(("create_comment", number))
        if self.fail_write:
            raise RuntimeError("write refused")
        self.next_id += 1
        self.comments[self.next_id] = {"id": self.next_id, "body": body, "user": {"login": BOT},
                                       "issue_url": self._issue_url(number)}
        return dict(self.comments[self.next_id])

    async def update_issue_comment(self, owner, repo, comment_id, body, *, token=None):
        self._seen(token)
        self.calls.append(("update_comment", comment_id))
        if self.fail_write:
            raise RuntimeError("write refused")
        self.comments[comment_id]["body"] = body
        return dict(self.comments[comment_id])

    def writes(self):
        return [call for call in self.calls if call[0] in {"create_comment", "update_comment"}]


@pytest_asyncio.fixture
async def fx(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'summary.db'}", connect_args={"timeout": 5})
    async with engine.begin() as conn:
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    monkeypatch.setattr(settings, "github_publish_summaries_enabled", True, raising=False)
    monkeypatch.setattr(settings, "github_app_bot_login", BOT)
    async with maker() as db:
        item, members, _tokens = await _dispatch_approval_fixture(db)
        scope = await db.get(TeamGithubScope, item.scope_id)
        scope.repo_name = "r"  # the fake GitHub, the canonical links and the comments all name o/r
        scope.review_hold_label = "synth-city-v42-review-hold"
        scope.delivery_policy = {"required_checks": [{"name": "ci", "app_slug": "github-actions"}],
                                 "broad_checks": "hosted"}
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(
            pr_number=12, dispatch_status="ready_for_review", last_verified_sha=HEAD,
            delivery_policy={"required_checks": [{"name": "ci", "app_slug": "github-actions"}], "broad_checks": "hosted"},
            dispatch_head_ref="deck/slot-28/issue-940-x", dispatch_base_ref="origin/release/v4.2"))
        _slot, verifier = await slot_member(db, scope, role="verifier", name="V1")
        db.add(GithubHeadReview(work_item_id=item.id, dispatch_nonce=item.dispatch_nonce, pr_number=12,
                                head_sha=HEAD, scope_revision=0, reviewer_member_id=verifier.id,
                                verdict="accepted", summary="Reviewed", evidence_url=EVIDENCE))
        await db.commit()
        data = SimpleNamespace(maker=maker, item_id=item.id, scope_id=item.scope_id, nonce=item.dispatch_nonce)
    yield data
    await engine.dispose()


async def _publish(fx, client):
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        item = await db.get(GithubWorkItem, fx.item_id)
        return await service.publish_item(db, scope, item, client)


async def _item(fx):
    async with fx.maker() as db:
        return (await db.execute(select(GithubWorkItem).where(GithubWorkItem.id == fx.item_id)
                                 .execution_options(populate_existing=True))).scalar_one()


@pytest.mark.asyncio
async def test_the_facts_come_from_the_records_and_the_comment_matches_them(fx):
    client = FakeGithub()
    assert await _publish(fx, client) == "created"
    (comment,) = client.comments.values()
    assert comment["body"].startswith("<!-- deck:summary-comment -->")
    assert f"head `{HEAD}`" in comment["body"] and "Full backend" not in comment["body"]
    assert "- **Required checks:** ci: success." in comment["body"]
    assert f"- **Head review:** accepted at this exact head ({EVIDENCE})." in comment["body"]
    assert "- **Review hold label:** absent." in comment["body"]
    item = await _item(fx)
    assert item.summary_comment_id == comment["id"] and item.summary_published_head_sha == HEAD
    assert item.summary_published_digest and item.summary_comment_author == BOT


@pytest.mark.asyncio
async def test_the_same_head_and_facts_write_nothing_the_second_time(fx):
    client = FakeGithub()
    await _publish(fx, client)
    assert await _publish(fx, client) == "unchanged"
    assert len(client.writes()) == 1


@pytest.mark.asyncio
async def test_a_new_head_edits_the_same_comment_by_id_and_no_second_comment_appears(fx):
    client = FakeGithub()
    await _publish(fx, client)
    comment_id = next(iter(client.comments))
    client.pull["head"] = {"sha": OTHER}
    client.checks = [dict(row, head_sha=OTHER) for row in client.checks]
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(last_verified_sha=OTHER))
        await db.commit()
    assert await _publish(fx, client) == "updated"
    assert list(client.comments) == [comment_id] and OTHER in client.comments[comment_id]["body"]
    assert "- **Head review:** none recorded for this head." in client.comments[comment_id]["body"]


@pytest.mark.asyncio
async def test_the_pr_body_is_never_read_for_a_write_and_never_changed(fx):
    client = FakeGithub(body="<!-- b1:human-review-summary:start -->\nlegacy\n<!-- b1:human-review-summary:end -->\n")
    before = dict(client.pull)
    await _publish(fx, client)
    assert client.pull == before
    assert all(call[0] in {"get_pull", "checks", "list_comments", "get_comment", "create_comment", "update_comment"}
               for call in client.calls)


@pytest.mark.asyncio
async def test_an_unrelated_comment_and_a_marker_comment_by_another_author_are_never_edited(fx):
    client = FakeGithub()
    client.comments[1] = {"id": 1, "body": "a human note", "user": {"login": "someone"}}
    client.comments[2] = {"id": 2, "body": "<!-- deck:summary-comment -->\nforged", "user": {"login": "someone"}}
    await _publish(fx, client)
    assert client.comments[1]["body"] == "a human note" and client.comments[2]["body"].endswith("forged")
    assert len(client.comments) == 3 and [call for call in client.writes() if call[0] == "update_comment"] == []


@pytest.mark.asyncio
async def test_a_recorded_comment_whose_author_changed_is_not_edited_a_new_one_is_created(fx):
    client = FakeGithub()
    await _publish(fx, client)
    comment_id = next(iter(client.comments))
    client.comments[comment_id]["user"] = {"login": "someone-else"}
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(
            summary_published_digest="stale"))
        await db.commit()
    assert await _publish(fx, client) == "created"
    assert client.comments[comment_id]["user"]["login"] == "someone-else"
    assert "forged" not in client.comments[comment_id]["body"]


@pytest.mark.asyncio
async def test_a_lost_comment_id_creates_a_new_comment(fx):
    client = FakeGithub()
    await _publish(fx, client)
    client.comments.clear()
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(
            summary_published_digest="stale"))
        await db.commit()
    assert await _publish(fx, client) == "created"


@pytest.mark.asyncio
async def test_a_crash_between_create_and_record_adopts_the_existing_comment_instead_of_a_duplicate(fx):
    client = FakeGithub()
    await _publish(fx, client)
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(
            summary_comment_id=None, summary_comment_author=None, summary_published_digest=None,
            summary_published_head_sha=None))
        await db.commit()
    assert await _publish(fx, client) in {"updated", "unchanged"}
    assert len(client.comments) == 1


@pytest.mark.asyncio
async def test_two_concurrent_publishes_write_once(fx):
    client = FakeGithub()
    results = await asyncio.gather(_publish(fx, client), _publish(fx, client), return_exceptions=True)
    assert not [result for result in results if isinstance(result, Exception)], results
    assert len(client.comments) == 1 and len(client.writes()) == 1


@pytest.mark.asyncio
async def test_a_failed_write_changes_nothing_and_never_raises(fx):
    client = FakeGithub()
    client.fail_write = True
    before = await _item(fx)
    assert await _publish(fx, client) == "unavailable"
    after = await _item(fx)
    assert (after.dispatch_status, after.status_note, after.summary_published_digest) == (
        before.dispatch_status, before.status_note, None)
    client.fail_write = False
    assert await _publish(fx, client) == "created"


@pytest.mark.asyncio
async def test_the_disabled_setting_writes_nothing(fx, monkeypatch):
    monkeypatch.setattr(settings, "github_publish_summaries_enabled", False, raising=False)
    client = FakeGithub()
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        await service.publish_scope(db, scope, client)
    assert client.comments == {} and client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("dispatched", 0), ("verifying", 0), ("ready_for_review", 1),
                                              ("awaiting_human_review", 1), ("merged", 1)])
async def test_the_scope_pass_publishes_only_at_ready_and_at_merge(fx, status, expected):
    client = FakeGithub(**({"state": "closed", "merged": True, "merge_commit_sha": OTHER}
                           if status == "merged" else {}))
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(dispatch_status=status))
        await db.commit()
        scope = await db.get(TeamGithubScope, fx.scope_id)
        await service.publish_scope(db, scope, client)
    assert len(client.comments) == expected
    if status == "merged":
        assert f"merged as `{OTHER}`" in next(iter(client.comments.values()))["body"]


@pytest.mark.asyncio
async def test_a_pull_whose_head_moved_since_the_facts_is_not_published(fx):
    client = FakeGithub()
    original = client.get_pull
    state = {"n": 0}

    async def moving(owner, repo, number, *, token=None):
        state["n"] += 1
        pull = await original(owner, repo, number, token=token)
        if state["n"] > 1:
            pull["head"] = {"sha": OTHER}
        return pull

    client.get_pull = moving
    assert await _publish(fx, client) == "skipped"
    assert client.comments == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("pull", [{"state": "closed"}, {"base": {"ref": "master"}}])
async def test_a_closed_unmerged_pull_or_a_wrong_base_is_not_published(fx, pull):
    client = FakeGithub(**pull)
    assert await _publish(fx, client) == "skipped"
    assert client.comments == {}


# --- publication never touches a gate -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publication_changes_no_status_no_note_and_no_gate_input(fx):
    before = await _item(fx)
    await _publish(fx, FakeGithub())
    after = await _item(fx)
    for name in ("dispatch_status", "status_note", "last_verified_sha", "retry_count", "escalation_reason",
                 "active_scope_revision", "pr_number", "effort_elapsed_seconds"):
        assert getattr(after, name) == getattr(before, name), name


@pytest.mark.asyncio
async def test_acceptance_is_identical_with_and_without_any_summary_block_in_the_body(fx):
    from app.services import github_head_acceptance_service as acceptance

    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        item = await db.get(GithubWorkItem, fx.item_id)
        plain = await acceptance.gate_note(db, scope, item, HEAD)
    await _publish(fx, FakeGithub(body="no blocks at all\n"))
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        item = await db.get(GithubWorkItem, fx.item_id)
        with_comment = await acceptance.gate_note(db, scope, item, HEAD)
    assert plain == with_comment


@pytest.mark.asyncio
async def test_a_head_with_an_accepted_review_passes_acceptance_when_the_body_has_no_blocks(fx):
    from app.services import github_head_acceptance_service as acceptance

    client = FakeGithub(body="")
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        item = await db.get(GithubWorkItem, fx.item_id)
        assert await acceptance.gate_note(db, scope, item, HEAD) is None
    assert client.pull["body"] == ""


@pytest.mark.asyncio
async def test_the_status_commit_is_written_before_the_comment_in_the_scope_pass(fx):
    order = []
    client = FakeGithub()
    original = client.create_issue_comment

    async def spying(owner, repo, number, body, *, token=None):
        async with fx.maker() as db:
            order.append(("write", (await db.get(GithubWorkItem, fx.item_id)).dispatch_status))
        return await original(owner, repo, number, body, token=token)

    client.create_issue_comment = spying
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        await service.publish_scope(db, scope, client)
    assert order == [("write", "ready_for_review")]


# --- render and publish routes and standalone pulls ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_standalone_pull_is_rendered_from_github_facts_only(fx):
    client = FakeGithub(number=77, html_url="https://github.com/o/r/pull/77")
    client.pull["head"] = {"sha": OTHER}
    client.checks = [dict(row, head_sha=OTHER) for row in client.checks]
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        result = await service.render(db, scope, 77, client)
    assert result["tracked"] is False and OTHER in result["comment"]
    assert "- **Approved plan:** not recorded." in result["comment"]
    assert client.writes() == []


@pytest.mark.asyncio
async def test_rendering_a_pull_writes_nothing_even_when_every_write_would_raise(fx):
    client = FakeGithub()
    client.fail_write = True
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        result = await service.render(db, scope, 12, client)
    assert result["tracked"] is True and client.writes() == []


@pytest.mark.asyncio
async def test_a_standalone_pull_is_published_as_one_comment_found_again_by_marker_and_author(fx):
    client = FakeGithub(number=77)
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert await service.publish_pull(db, scope, 77, client) == "created"
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert await service.publish_pull(db, scope, 77, client) == "unchanged"
    assert len(client.comments) == 1


@pytest.mark.asyncio
async def test_a_standalone_publish_without_a_configured_bot_login_is_refused(fx, monkeypatch):
    monkeypatch.setattr(settings, "github_app_bot_login", "")
    client = FakeGithub(number=77)
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert await service.publish_pull(db, scope, 77, client) == "unavailable"
    assert client.comments == {}


@pytest.mark.asyncio
async def test_the_routes_split_reading_from_publishing_and_publishing_is_leader_only(fx, monkeypatch):
    from fastapi import HTTPException

    from app.api.v1 import github_work_progress as routes

    client = FakeGithub()
    monkeypatch.setattr(routes, "github_client", client)
    leader = SimpleNamespace(id=1)

    async def only_the_leader(db, scope_id, principal):
        if principal is not leader:
            raise routes.CoordinationError("not_leader", 403)

    monkeypatch.setattr(routes.github_coordination_service, "require_leader", only_the_leader)
    async with fx.maker() as db:
        rendered = await routes.render_pull_summary(fx.scope_id, 12, principal=leader, db=db)
        assert rendered["comment"].startswith("<!-- deck:summary-comment -->") and client.writes() == []
        with pytest.raises(HTTPException) as caught:
            await routes.publish_pull_summary(fx.scope_id, 12, principal=SimpleNamespace(id=2), db=db)
        assert caught.value.status_code == 403 and client.writes() == []
        result = await routes.publish_pull_summary(fx.scope_id, 12, principal=leader, db=db)
        assert result["result"] in {"created", "updated", "unchanged"} and len(client.comments) == 1
    methods = {route.path: route.methods for route in routes.router.routes}
    assert methods["/github-pull-summaries/{scope_id}/{pull_number}"] == {"GET"}
    assert methods["/github-pull-summaries/{scope_id}/{pull_number}/publish"] == {"POST"}


# --- migration and defaults ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_old_database_without_the_new_nullable_columns_is_migrated(tmp_path):
    from sqlalchemy import text

    from app.database import _run_sqlite_compat_migrations, _sqlite_columns

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
    new = ("summary_comment_id", "summary_comment_author", "summary_published_digest",
           "summary_published_head_sha", "summary_published_status", "summary_published_at",
           "summary_publish_claim_at")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for column in new:
            await conn.execute(text(f"ALTER TABLE github_work_items DROP COLUMN {column}"))
    async with engine.begin() as conn:
        await _run_sqlite_compat_migrations(conn)
    async with engine.begin() as conn:
        columns = await _sqlite_columns(conn, "github_work_items")
        info = (await conn.execute(text("PRAGMA table_info(github_work_items)"))).fetchall()
    await engine.dispose()
    assert set(new) <= set(columns)
    assert all(row[3] == 0 for row in info if row[1] in new), "the new columns must be nullable"


def test_publication_is_off_by_default():
    from app.config import Settings

    assert Settings().github_publish_summaries_enabled is False


@pytest.mark.asyncio
async def test_the_approved_plan_and_the_recorded_effort_come_from_the_records(fx):
    async with fx.maker() as db:
        item = await db.get(GithubWorkItem, fx.item_id)
        request = GithubApprovalRequest(
            work_item_id=item.id, request_kind="initial_plan", dispatch_nonce=item.dispatch_nonce, approval_round=2,
            owner_member_id=1, leader_member_id=2, request_fingerprint="f", status="approved")
        db.add(request)
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == item.id).values(
            effort_limit_minutes=90, effort_extension_minutes=15, effort_elapsed_seconds=61 * 60))
        await db.commit()
        request_id = request.id
    client = FakeGithub()
    await _publish(fx, client)
    body = next(iter(client.comments.values()))["body"]
    assert f"- **Approved plan:** request {request_id}, round 2." in body
    assert "90 minutes (+15 extension); ledger 61 minutes." in body
    assert "- **Estimate:** Unknown." in body


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_the_verification_poll_runs_the_summary_pass_last_and_a_failure_never_reaches_it(fx, monkeypatch, raises):
    from app.services.github_verification_service import github_verification_service

    calls = []

    async def spy(db, scope, client):
        calls.append(scope.id)
        if raises:
            raise RuntimeError("publication failed")

    monkeypatch.setattr(service, "publish_scope", spy)
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(dispatch_status="failed"))
        await db.commit()
        scope = await db.get(TeamGithubScope, fx.scope_id)
        await github_verification_service.process_scope(db, scope, client=FakeGithub())
    assert calls == [fx.scope_id]


@pytest.mark.asyncio
async def test_a_comment_by_the_bot_without_the_marker_is_never_adopted_or_edited(fx):
    client = FakeGithub()
    client.comments[1] = {"id": 1, "body": "a bot note without the marker", "user": {"login": BOT},
                          "issue_url": "https://api.github.com/repos/o/r/issues/12"}
    await _publish(fx, client)
    assert client.comments[1]["body"] == "a bot note without the marker" and len(client.comments) == 2


@pytest.mark.asyncio
async def test_a_fresh_publish_claim_blocks_a_second_writer_and_a_stale_claim_does_not(fx):
    from datetime import datetime, timedelta

    client = FakeGithub()
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(
            summary_publish_claim_at=datetime.utcnow()))
        await db.commit()
    assert await _publish(fx, client) == "skipped" and client.writes() == []
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(
            summary_publish_claim_at=datetime.utcnow() - timedelta(minutes=6)))
        await db.commit()
    assert await _publish(fx, client) == "created"
    assert (await _item(fx)).summary_publish_claim_at is None


@pytest.mark.asyncio
async def test_an_unchanged_head_makes_no_comment_call_at_all_on_the_second_publish(fx):
    client = FakeGithub()
    await _publish(fx, client)
    client.calls.clear()
    assert await _publish(fx, client) == "unchanged"
    assert not [call for call in client.calls if "comment" in call[0]], client.calls


@pytest.mark.asyncio
async def test_a_merged_item_already_published_as_merged_is_not_touched_again_by_the_scope_pass(fx):
    client = FakeGithub(state="closed", merged=True, merge_commit_sha=OTHER)
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(dispatch_status="merged"))
        await db.commit()
        scope = await db.get(TeamGithubScope, fx.scope_id)
        await service.publish_scope(db, scope, client)
        assert len(client.writes()) == 1
        client.calls.clear()
        await service.publish_scope(db, scope, client)
    assert client.calls == []


# --- V1 review 5917176293: the five findings --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence", [
    "https://github.com/o/r/pull/12#pullrequestreview-77?access_token=opaquevalue",
    "https://github.com/o/r/pull/12?access_token=opaquevalue#pullrequestreview-77",
    "https://user:password@github.com/o/r/pull/12#pullrequestreview-77",
    "https://github.com/o/r/pull/12%23pullrequestreview-77",
    "https://github.com/o/r/pull/12#pullrequestreview-77&x_api_key=opaquevalue",
    "https://evil.example/o/r/pull/12#pullrequestreview-77",
    "https://github.com/o/r/pull/13#pullrequestreview-77",
    "https://github.com/other/r/pull/12#pullrequestreview-77",
    "https://github.com/o/r/pull/12#pullrequestreview-77/extra",
    "http://github.com/o/r/pull/12#pullrequestreview-77",
])
async def test_a_noncanonical_or_credential_bearing_evidence_link_is_withheld_never_published(fx, evidence):
    async with fx.maker() as db:
        await db.execute(update(GithubHeadReview).where(GithubHeadReview.work_item_id == fx.item_id)
                         .values(evidence_url=evidence))
        await db.commit()
    client = FakeGithub()
    assert await _publish(fx, client) == "created"
    body = next(iter(client.comments.values()))["body"]
    assert "- **Head review:** accepted at this exact head (evidence link withheld)." in body
    for piece in ("opaquevalue", "password", "evil.example", "access_token", "api_key", "%23", "/extra"):
        assert piece not in body


def test_the_guard_recognises_a_credential_name_after_an_underscore_or_hyphen_but_not_inside_a_word():
    for text in ("access_token=abc", "x-api-token: abc", "client_secret=abc", "?key_password=abc"):
        with pytest.raises(module.SummaryRenderError):
            render_comment(facts(pull_url=f"https://github.com/o/r/pull/12?{text}"))
    render_comment(facts(base="release/v4.2"))  # 'release' contains 'lease': allowed


@pytest.mark.asyncio
async def test_the_pull_url_and_merge_sha_come_from_the_repository_and_number_not_from_a_response_field(fx):
    client = FakeGithub(html_url="https://evil.example/steal?access_token=zzz", merged=True, state="closed",
                        merge_commit_sha="not-a-sha")
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(dispatch_status="merged"))
        await db.commit()
    await _publish(fx, client)
    body = next(iter(client.comments.values()))["body"]
    assert "https://github.com/o/r/pull/12 into" in body and "evil.example" not in body and "not-a-sha" not in body


@pytest.mark.asyncio
async def test_an_app_mode_scope_passes_its_pull_request_token_to_every_read_and_write(fx, monkeypatch):
    from app.services.github_approval_service import github_approval_service

    seen = []

    async def mint(scope):
        seen.append(scope.id)
        return "app-token-value"

    monkeypatch.setattr(github_approval_service, "github_read_token", mint)
    client = FakeGithub()
    assert await _publish(fx, client) == "created"
    assert seen and set(client.tokens) == {"app-token-value"}, client.tokens
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        await service.render(db, scope, 12, client)
    assert set(client.tokens) == {"app-token-value"}


@pytest.mark.asyncio
async def test_a_failed_token_mint_is_unavailable_never_a_fallback_and_never_a_raise(fx, monkeypatch):
    from app.services.github_approval_service import github_approval_service

    async def broken(scope):
        raise RuntimeError("mint failed")

    monkeypatch.setattr(github_approval_service, "github_read_token", broken)
    client = FakeGithub()
    assert await _publish(fx, client) == "unavailable"
    assert client.calls == [] and client.comments == {}
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert await service.publish_pull(db, scope, 77, client) == "unavailable"
    assert client.calls == []


@pytest.mark.asyncio
async def test_the_real_client_sends_the_authorization_header_on_the_comment_get_and_post(fx, monkeypatch):
    import httpx

    from app.services.github_approval_service import github_approval_service

    async def mint(scope):
        return "app-token-value"

    monkeypatch.setattr(github_approval_service, "github_read_token", mint)
    seen = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        path = request.url.path
        if path == "/repos/o/r/pulls/12":
            return httpx.Response(200, json={"number": 12, "state": "open", "draft": False, "merged": False,
                                             "base": {"ref": "release/v4.2"}, "head": {"sha": HEAD}, "labels": []})
        if path.startswith("/repos/o/r/commits/") or path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if request.method == "GET" and path.endswith("/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={"id": 5, "body": "x", "user": {"login": BOT},
                                             "issue_url": "https://api.github.com/repos/o/r/issues/12"})
        return httpx.Response(200, json={})

    async def no_checks(owner, repo, ref, *, token=None):  # check observation has its own multi-call tests
        return []

    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(api)) as http:
        real = GithubClient(http=http, token=None)
        real.list_check_runs_for_ref = no_checks
        assert await _publish(fx, real) == "created"
    comment_calls = [row for row in seen if row[1].endswith("/comments")]
    assert comment_calls and all(row[2] == "Bearer app-token-value" for row in seen), seen


async def _age_the_publication(fx, minutes):
    from datetime import datetime, timedelta

    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(
            summary_published_at=datetime.utcnow() - timedelta(minutes=minutes)))
        await db.commit()


async def _scope_pass(fx, client):
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        await service.publish_scope(db, scope, client)


@pytest.mark.asyncio
async def test_a_fresh_publication_is_not_re_read_by_the_scope_pass_within_five_minutes(fx):
    client = FakeGithub()
    await _scope_pass(fx, client)
    client.calls.clear()
    await _scope_pass(fx, client)
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["review_withdrawn", "hold_added", "check_failed"])
async def test_the_scope_pass_refreshes_same_head_facts_after_the_throttle(fx, change):
    client = FakeGithub()
    await _scope_pass(fx, client)
    comment = next(iter(client.comments.values()))
    assert "accepted at this exact head" in comment["body"]
    async with fx.maker() as db:
        if change == "review_withdrawn":
            await db.execute(update(GithubHeadReview).where(GithubHeadReview.work_item_id == fx.item_id)
                             .values(status="ended"))
        await db.commit()
    if change == "hold_added":
        client.pull["labels"] = [{"name": "synth-city-v42-review-hold"}]
    if change == "check_failed":
        client.checks = [dict(row, conclusion="failure") for row in client.checks]
    await _age_the_publication(fx, 6)
    await _scope_pass(fx, client)
    body = next(iter(client.comments.values()))["body"]
    assert {"review_withdrawn": "none recorded for this head", "hold_added": "hold label:** present",
            "check_failed": "ci: failure"}[change] in body
    assert len(client.comments) == 1


@pytest.mark.asyncio
async def test_unchanged_facts_after_the_throttle_write_nothing(fx):
    client = FakeGithub()
    await _scope_pass(fx, client)
    await _age_the_publication(fx, 6)
    client.calls.clear()
    await _scope_pass(fx, client)
    assert client.writes() == []


@pytest.mark.asyncio
async def test_a_retained_comment_id_of_another_pull_is_never_edited_and_the_current_pull_gets_its_own(fx):
    client = FakeGithub()
    await _publish(fx, client)
    first_id = next(iter(client.comments))
    first_body = client.comments[first_id]["body"]
    async with fx.maker() as db:
        await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(pr_number=13))
        await db.commit()
    client.pull["number"] = 13
    client.pull["html_url"] = "https://github.com/o/r/pull/13"
    assert await _publish(fx, client) == "created"
    assert client.comments[first_id]["body"] == first_body
    other = [c for cid, c in client.comments.items() if cid != first_id]
    assert len(other) == 1 and other[0]["issue_url"].endswith("/issues/13")
    assert "pull/13" in other[0]["body"]


@pytest.mark.asyncio
async def test_a_comment_whose_issue_url_names_another_repository_is_not_adopted(fx):
    client = FakeGithub()
    client.comments[1] = {"id": 1, "body": MARKER_COMMENT, "user": {"login": BOT},
                          "issue_url": "https://api.github.com/repos/other/repo/issues/12"}
    await _publish(fx, client)
    assert client.comments[1]["body"] == MARKER_COMMENT and len(client.comments) == 2


MARKER_COMMENT = "<!-- deck:summary-comment -->\nanother repository's"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["head", "closed"])
async def test_a_standalone_pull_is_rechecked_right_before_the_write(fx, change):
    client = FakeGithub(number=77)
    original = client.list_issue_comments

    async def moving(owner, repo, number, *, token=None):
        rows = await original(owner, repo, number, token=token)
        if change == "head":
            client.pull["head"] = {"sha": OTHER}
        else:
            client.pull["state"] = "closed"
        return rows

    client.list_issue_comments = moving
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert await service.publish_pull(db, scope, 77, client) == "skipped"
    assert client.writes() == []


@pytest.mark.asyncio
async def test_an_unchanged_re_read_restarts_the_throttle_so_the_next_pass_makes_no_github_call(fx):
    client = FakeGithub()
    await _scope_pass(fx, client)
    await _age_the_publication(fx, 6)
    await _scope_pass(fx, client)  # re-read, nothing changed, nothing written
    client.calls.clear()
    await _scope_pass(fx, client)
    assert client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["tracked", "standalone"])
@pytest.mark.parametrize("change", ["open_to_merged", "draft_to_ready", "ready_to_draft"])
async def test_a_state_or_draft_change_during_the_lookup_writes_nothing_in_both_paths(fx, path, change):
    number = 12 if path == "tracked" else 77
    client = FakeGithub(number=number, draft=(change == "draft_to_ready"))
    original = client.list_issue_comments

    async def changing(owner, repo, n, *, token=None):
        rows = await original(owner, repo, n, token=token)
        if change == "open_to_merged":
            client.pull.update(state="closed", merged=True, merge_commit_sha=OTHER)
        elif change == "draft_to_ready":
            client.pull["draft"] = False
        else:
            client.pull["draft"] = True
        return rows

    client.list_issue_comments = changing
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        assert await service.publish_pull(db, scope, number, client) == "skipped"
    assert client.writes() == [] and client.comments == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["tracked", "standalone"])
async def test_a_pull_that_is_already_merged_when_rendered_is_still_published_as_merged(fx, path):
    number = 12 if path == "tracked" else 77
    client = FakeGithub(number=number, state="closed", merged=True, merge_commit_sha=OTHER)
    async with fx.maker() as db:
        scope = await db.get(TeamGithubScope, fx.scope_id)
        if path == "tracked":
            await db.execute(update(GithubWorkItem).where(GithubWorkItem.id == fx.item_id).values(
                dispatch_status="merged"))
            await db.commit()
        assert await service.publish_pull(db, scope, number, client) == "created"
    assert f"merged as `{OTHER}`" in next(iter(client.comments.values()))["body"]

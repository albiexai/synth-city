"""Exercise the Supervisor audit against a SQLite fixture and a fake GitHub CLI."""
from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "supervisor"))
import audit_merges as audit  # noqa: E402

HEAD = "a" * 40
TOKEN = "ghp_fixture_secret_never_print"
BASE = "release/v4.4"
MERGED = "2026-11-27T00:00:00Z"


def pull(number=1, **changes):
    return dict(number=number, head=HEAD, base=BASE, repo="o/r",
                state="closed", draft=False, merged_at=MERGED, labels=[]) | changes


class GitHub:
    def __init__(self, *rows, current=None):
        self.rows = list(rows)
        self.current = current
        self.writes = []
        self.pages = []
        self.reads = []

    def __call__(self, command, *, input=None, **options):
        assert command[:2] == ["gh", "api"]
        assert options["stderr"] == subprocess.DEVNULL and options["check"]
        assert options["env"]["GH_PROMPT_DISABLED"] == "1"
        assert command[command.index("--hostname") + 1] == "github.com"
        verb = command[command.index("--method") + 1]
        target = urlsplit(command[command.index("--method") + 2])
        self.reads.append((verb, target.path))
        if verb == "POST":
            self.writes.append((target.path, json.loads(input)))
            value = ["keep-me", "review-hold"]
        elif target.path == "repos/o/r":
            value = BASE
        elif target.path == "repos/o/r/pulls":
            query = parse_qs(target.query)
            assert query["base"] == [BASE] and query["state"] == ["all"]
            page, size = int(query["page"][0]), int(query["per_page"][0])
            self.pages.append(page)
            value = self.rows[(page - 1) * size:page * size]
        else:
            assert verb == "GET" and target.path == "repos/o/r/pulls/1"
            value = self.current() if callable(self.current) else self.current
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(value))


@pytest.fixture
def evidence(tmp_path):
    path = tmp_path / "evidence #1.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE team_github_scopes (id, preset_id, repo_owner, repo_name, base_ref, review_hold_label, enabled);
            CREATE TABLE agent_team_presets (id, leader_slot_id);
            CREATE TABLE github_work_items (id, scope_id, pr_number, owner_slot_id, dispatch_nonce);
            CREATE TABLE agent_team_slots (id, preset_id, enabled, role);
            CREATE TABLE mail_team_members (id, team_preset_id, team_slot_id, updated_at);
            CREATE TABLE github_head_reviews
                (id, work_item_id, dispatch_nonce, pr_number, head_sha, status, verdict, reviewer_member_id);
            INSERT INTO team_github_scopes VALUES (1, 2, 'o', 'r', 'origin/release/v4.4', 'review-hold', 1);
            INSERT INTO agent_team_presets VALUES (2, 22);
            INSERT INTO agent_team_presets VALUES (99, 32);
            INSERT INTO github_work_items VALUES (3, 1, 1, 21, 'private-nonce');
            INSERT INTO agent_team_slots VALUES (23, 2, 1, ' VeRiFiEr ');
            INSERT INTO mail_team_members VALUES (4, 2, 23, '2026-11-26');
        """)
        db.execute("INSERT INTO github_head_reviews VALUES (1, 3, 'private-nonce', 1, ?, 'active', 'accepted', 4)",
                   (HEAD,))
    return path


def change(path, sql):
    with sqlite3.connect(path) as db:
        db.execute(sql)


def run(path, github, *args):
    out, err = io.StringIO(), io.StringIO()
    result = audit.run(["--db", str(path), "--scope-id", "1", *args], gh_run=github,
                       env={"GH_TOKEN": TOKEN}, stdout=out, stderr=err)
    assert TOKEN not in out.getvalue() + err.getvalue()
    assert "private-nonce" not in out.getvalue() + err.getvalue()
    return result, out.getvalue(), err.getvalue()


def test_accepted_merge_is_a_get_only_read_of_readonly_db(evidence, monkeypatch):
    connect = sqlite3.connect

    def read_only(*args, **kwargs):
        db = connect(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("CREATE TABLE unexpected_write (id)")
        return db

    monkeypatch.setattr(audit.sqlite3, "connect", read_only)
    before = evidence.read_bytes()
    gh = GitHub(pull(), pull(2, merged_at=None))
    assert run(evidence, gh) == (0, "PR #1 merged with exact-head acceptance.\n", "")
    assert not gh.writes and evidence.read_bytes() == before


@pytest.mark.parametrize("sql", [
    "DELETE FROM github_head_reviews",
    "UPDATE github_head_reviews SET head_sha = 'some other head'",
    "UPDATE github_head_reviews SET status = 'superseded'",
    "UPDATE github_head_reviews SET verdict = 'changes_required'",
    "UPDATE github_head_reviews SET dispatch_nonce = 'previous-attempt'",
    "UPDATE github_head_reviews SET pr_number = 99",
    "UPDATE github_work_items SET scope_id = 99",
    "UPDATE agent_team_slots SET role = 'builder'",
    "UPDATE agent_team_slots SET enabled = 0",
    "UPDATE agent_team_slots SET preset_id = 99",
    "UPDATE mail_team_members SET team_preset_id = 99",
    "UPDATE github_work_items SET owner_slot_id = 23",
    "UPDATE agent_team_presets SET leader_slot_id = 23",
    "INSERT INTO mail_team_members VALUES (5, 2, 23, '2026-11-27')",
    "INSERT INTO github_head_reviews SELECT 2, work_item_id, dispatch_nonce, pr_number, head_sha, status, "
    "'changes_required', reviewer_member_id FROM github_head_reviews",
])
def test_each_missing_condition_means_no_acceptance(evidence, sql):
    change(evidence, sql)
    code, out, err = run(evidence, GitHub(pull()))
    assert code == 1 and "PR #1 merged without proven exact-head acceptance." in out and not err


def open_pull(**changes):
    return pull(state="open", merged_at=None, labels=["keep-me"]) | changes


def test_explicit_restore_only_adds_the_configured_label(evidence):
    change(evidence, "UPDATE github_head_reviews SET status = 'superseded'")
    row = open_pull()
    gh = GitHub(row, current=row)
    assert run(evidence, gh)[0] == 1 and gh.writes == []
    assert run(evidence, gh, "--restore-hold")[0] == 1
    assert gh.writes == [("repos/o/r/issues/1/labels", {"labels": ["review-hold"]})]
    assert ("GET", "repos/o/r/pulls/1") in gh.reads


@pytest.mark.parametrize("changes", [
    {"head": "b" * 40}, {"draft": True}, {"state": "closed"}, {"number": 2},
    {"base": "other"}, {"repo": "elsewhere/r"}, {"labels": ["review-hold"]},
])
def test_restore_checks_the_current_pull_again(evidence, changes):
    change(evidence, "UPDATE github_head_reviews SET status = 'superseded'")
    gh = GitHub(open_pull(), current=open_pull(**changes))
    assert run(evidence, gh, "--restore-hold")[0] != 0
    assert gh.writes == [] and ("GET", "repos/o/r/pulls/1") in gh.reads


@pytest.mark.parametrize("update", [
    "UPDATE github_head_reviews SET status = 'active'",
    "UPDATE team_github_scopes SET enabled = 0",
    "UPDATE team_github_scopes SET preset_id = 99",
    "DELETE FROM github_work_items",
])
def test_evidence_changing_during_the_github_read_prevents_restoration(evidence, update):
    change(evidence, "UPDATE github_head_reviews SET status = 'superseded'")

    def refresh():
        change(evidence, update)
        return open_pull()

    gh = GitHub(open_pull(), current=refresh)
    assert run(evidence, gh, "--restore-hold")[0] == 1 and gh.writes == []


@pytest.mark.parametrize("changes", [{"draft": True}, {"number": 2}, {"labels": ["review-hold"]}])
def test_draft_untracked_or_already_held_pull_is_left_alone(evidence, changes):
    change(evidence, "UPDATE github_head_reviews SET status = 'superseded'")
    gh = GitHub(open_pull(**changes))
    run(evidence, gh, "--restore-hold")
    assert gh.writes == [] and gh.reads == [("GET", "repos/o/r/pulls")]


@pytest.mark.parametrize("setting", ["review_hold_label = NULL", "enabled = 0"])
def test_restore_needs_an_enabled_scope_with_a_configured_hold(evidence, setting):
    change(evidence, f"UPDATE team_github_scopes SET {setting}")
    gh = GitHub(open_pull())
    assert run(evidence, gh, "--restore-hold")[0] == 2 and not gh.writes


def test_complete_pagination_and_default_base_resolution(evidence, monkeypatch):
    change(evidence, "UPDATE team_github_scopes SET base_ref = 'origin/HEAD'")
    monkeypatch.setattr(audit, "PAGE_SIZE", 1)
    gh = GitHub(pull(2), pull())
    code, out, _ = run(evidence, gh)
    assert code == 1 and gh.pages == [1, 2, 3] and "PR #1 merged with exact-head acceptance." in out
    assert ("GET", "repos/o/r") in gh.reads


def test_incomplete_listing_stops_before_any_label_write(evidence, monkeypatch):
    change(evidence, "UPDATE github_head_reviews SET status = 'superseded'")
    monkeypatch.setattr(audit, "PAGE_SIZE", 1)
    monkeypatch.setattr(audit, "MAX_PAGES", 1)
    gh = GitHub(open_pull(), current=open_pull())
    assert run(evidence, gh, "--restore-hold")[0] == 2 and gh.writes == []


@pytest.mark.parametrize("bad", [pull(head=TOKEN), {"body": TOKEN}, pull(merged_at=TOKEN)])
def test_malformed_github_evidence_fails_without_echoing_data(evidence, bad):
    gh = GitHub(bad)
    code, out, err = run(evidence, gh, "--restore-hold")
    assert code == 2 and not out and err.startswith("error: ") and gh.writes == []


def test_github_failure_does_not_echo_command_output(evidence):
    def denied(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, output=TOKEN, stderr=TOKEN)

    assert run(evidence, denied)[0] == 2



def test_invalid_arguments_are_reported_without_echoing_them(evidence, capsys):
    code, out, err = run(evidence, GitHub(), f"--{TOKEN}")
    captured = capsys.readouterr()
    assert code == 2 and not out and "invalid arguments" in err
    assert not captured.out and not captured.err

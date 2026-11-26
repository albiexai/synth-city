"""#1331: the split helper is a dry run by default, files each part once, and never ends or closes the original."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "supervisor"))
import split  # noqa: E402

TOKEN = "gh-secret-5d2f90ab"
READS = {("issue", "view"), ("issue", "list")}
PLAN = {"repo": "o/r", "original": 900, "milestone": "v4.4", "parts": [
    {"title": "Part one", "body": "First part.", "lane": "v44-ui", "priority": 21},
    {"title": "Part two", "body": "Second part.", "lane": "v44-infra", "priority": 22}],
    "end_stopped": {"preset": 9, "item": 349, "generation": "a" * 64, "revision": 0}}


class FakeGh:
    """The few gh commands the helper uses, over one issue tracker kept in memory."""

    def __init__(self, labels=("synth-city-v44-ready", "v44-ui"), state="OPEN", milestone="v4.4", fill=0, fail_at=None):
        self.view = {"number": 900, "state": state, "labels": [{"name": n} for n in labels],
                     "milestone": {"title": milestone} if milestone else None, "comments": []}
        self.created, self.calls, self.fill, self.fail_at = [], [], fill, fail_at
        self.bad_list, self.lag = None, False

    def __call__(self, args):
        self.calls.append(tuple(args))
        verb = tuple(args[:2])
        if verb == ("issue", "view"):
            return 0, json.dumps(self.view)
        if verb == ("issue", "list"):
            if self.bad_list is not None:
                return 0, json.dumps(self.bad_list)
            rows = [{"number": c["number"], "title": c["title"], "body": c["body"]} for c in self.created]
            if "--search" in args:  # the search finds the marker text; a lagging index does not find a new part yet
                return 0, json.dumps([{"number": n, "title": "x", "body": ""} for n in range(2000, 2000 + self.fill)] + ([] if self.lag else rows))
            return 0, json.dumps(rows)
        if verb == ("issue", "create"):
            if self.fail_at == len(self.created) + 1:
                return 1, f"boom: bad credentials {TOKEN}\nsecond line"
            get = lambda flag: args[args.index(flag) + 1]  # noqa: E731
            number = 1001 + len(self.created)
            self.created.append({"number": number, "title": get("--title"), "body": get("--body"), "milestone": get("--milestone"),
                                 "labels": [args[i + 1] for i, a in enumerate(args) if a == "--label"]})
            return 0, f"https://github.com/o/r/issues/{number}\n"
        if verb == ("issue", "edit"):
            self.view["labels"] = [x for x in self.view["labels"] if x["name"] != args[args.index("--remove-label") + 1]]
            return 0, ""
        if verb == ("issue", "comment"):
            self.view["comments"].append({"body": args[args.index("--body") + 1]})
            return 0, ""
        return 1, "unknown command"

    def writes(self):
        return [c for c in self.calls if tuple(c[:2]) not in READS]


def go(tmp_path, fake, *extra, plan=PLAN, env=None):
    path = tmp_path / "plan.json"
    path.write_text(plan if isinstance(plan, str) else json.dumps(plan))
    out, err = io.StringIO(), io.StringIO()
    code = split.run(["--plan", str(path), *extra], env=env or {}, stdout=out, stderr=err, runner=fake)
    return code, out.getvalue(), err.getvalue()


def test_the_default_is_a_dry_run_that_runs_only_read_commands_and_changes_nothing(tmp_path):
    fake = FakeGh()
    code, out, _ = go(tmp_path, fake)
    assert code == 0 and fake.writes() == [] and fake.created == [] and fake.view["comments"] == []
    assert "dry run" in out and "would create" in out and "Part one" in out and "Part two" in out
    assert "would remove label synth-city-v44-ready" in out and "would comment" in out
    assert {l["name"] for l in fake.view["labels"]} == {"synth-city-v44-ready", "v44-ui"}


def test_execute_files_each_part_once_removes_the_label_and_comments_the_list(tmp_path):
    fake = FakeGh()
    code, out, _ = go(tmp_path, fake, "--execute")
    assert code == 0 and [c["title"] for c in fake.created] == ["Part one", "Part two"]
    first = fake.created[0]
    assert first["labels"] == ["v44-ui", "priority:21"] and first["milestone"] == "v4.4"
    assert "Split from #900" in first["body"] and "split-from:#900 part:1" in first["body"]
    assert [l["name"] for l in fake.view["labels"]] == ["v44-ui"]
    assert len(fake.view["comments"]) == 1 and "#1001" in fake.view["comments"][0]["body"] and "#1002" in fake.view["comments"][0]["body"]
    assert "created #1001" in out


def test_a_second_run_files_nothing_new_and_writes_nothing(tmp_path):
    fake = FakeGh()
    go(tmp_path, fake, "--execute")
    before = len(fake.writes())
    code, out, _ = go(tmp_path, fake, "--execute")
    assert code == 0 and len(fake.writes()) == before and len(fake.created) == 2 and len(fake.view["comments"]) == 1
    assert "already filed as #1001" in out


def test_a_failed_create_stops_the_run_and_a_rerun_files_only_the_missing_part(tmp_path):
    fake = FakeGh(fail_at=2)
    code, out, err = go(tmp_path, fake, "--execute")
    assert code == 2 and len(fake.created) == 1 and fake.view["comments"] == []
    assert any(l["name"] == "synth-city-v44-ready" for l in fake.view["labels"]), "the label stays until every part is filed"
    fake.fail_at = None
    code, _, _ = go(tmp_path, fake, "--execute")
    assert code == 0 and [c["number"] for c in fake.created] == [1001, 1002] and len(fake.view["comments"]) == 1


def test_a_full_issue_list_page_refuses_execute_because_a_duplicate_cannot_be_ruled_out(tmp_path):
    fake = FakeGh(fill=200)
    code, out, err = go(tmp_path, fake, "--execute")
    assert code == 2 and fake.writes() == [] and "cannot prove" in out + err
    assert go(tmp_path, FakeGh(fill=200))[0] == 1  # the dry run warns and reports attention


@pytest.mark.parametrize("change", [
    {"repo": "no-slash"}, {"repo": "a/b/c"}, {"original": 0}, {"original": "9"}, {"parts": []},
    {"parts": [PLAN["parts"][0]] * 11}, {"milestone": 5},
    {"parts": [{**PLAN["parts"][0], "title": ""}]}, {"parts": [{**PLAN["parts"][0], "lane": "bad lane"}]},
    {"parts": [{**PLAN["parts"][0], "priority": "21"}]}, {"parts": [{**PLAN["parts"][0], "priority": 0}]},
    {"parts": [{k: v for k, v in PLAN["parts"][0].items() if k != "body"}]}, {"parts": ["x"]},
])
def test_an_invalid_plan_is_refused_before_any_command_runs(tmp_path, change):
    fake = FakeGh()
    code, _, _ = go(tmp_path, fake, "--execute", plan={**PLAN, **change})
    assert code == 2 and fake.calls == []


@pytest.mark.parametrize("plan", ["not json", "[]", "x" * 70000])
def test_an_unreadable_plan_file_is_a_setup_error(tmp_path, plan):
    fake = FakeGh()
    assert go(tmp_path, fake, "--execute", plan=plan)[0] == 2 and fake.calls == []
    assert split.run(["--plan", str(tmp_path / "absent.json")], env={}, stdout=io.StringIO(), stderr=io.StringIO(), runner=fake) == 2


@pytest.mark.parametrize("fake_args,plan", [
    ({"state": "CLOSED"}, PLAN), ({"milestone": None}, {k: v for k, v in PLAN.items() if k != "milestone"}),
])
def test_a_closed_original_or_a_missing_milestone_is_refused_with_no_write(tmp_path, fake_args, plan):
    fake = FakeGh(**fake_args)
    assert go(tmp_path, fake, "--execute", plan=plan)[0] == 2 and fake.writes() == []


@pytest.mark.parametrize("damage", [{"labels": "none"}, {"state": None}, {"comments": {}}, {"number": "x"}])
def test_a_gh_answer_without_the_fields_the_helper_reads_is_an_error_with_no_write(tmp_path, damage):
    fake = FakeGh()
    fake.view.update(damage)
    code, out, err = go(tmp_path, fake, "--execute")
    assert code == 2 and fake.writes() == [] and "no valid" in out + err


def test_the_original_milestone_is_used_when_the_plan_has_none(tmp_path):
    fake = FakeGh(milestone="v4.5")
    go(tmp_path, fake, "--execute", plan={k: v for k, v in PLAN.items() if k != "milestone"})
    assert {c["milestone"] for c in fake.created} == {"v4.5"}


def test_no_token_value_reaches_the_output_even_from_gh_text_or_a_title(tmp_path):
    plan = {**PLAN, "parts": [{**PLAN["parts"][0], "title": f"Part {TOKEN}"}, PLAN["parts"][1]]}
    out_all = ""
    for args in (("--execute",), ()):
        fake = FakeGh(fail_at=2)
        code, out, err = go(tmp_path, fake, *args, plan=plan, env={"GH_TOKEN": TOKEN, "GITHUB_TOKEN": TOKEN})
        out_all += out + err
    assert TOKEN not in out_all and "boom" in out_all


def test_control_characters_in_a_title_never_reach_the_terminal(tmp_path):
    plan = {**PLAN, "parts": [{**PLAN["parts"][0], "title": "A\x1b[31mred\x07\r\nB"}, PLAN["parts"][1]]}
    _, out, err = go(tmp_path, FakeGh(), plan=plan)
    assert all(c == "\n" or c.isprintable() for c in out + err) and "red" in out


def test_the_end_stopped_command_has_the_ids_and_no_token_value(tmp_path):
    _, out, _ = go(tmp_path, FakeGh(), env={"SYNTH_CITY_OPERATOR_TOKEN": TOKEN})
    assert "/presets/9/work-items/349/end-stopped" in out and '"outcome":"superseded"' in out
    assert "$SYNTH_CITY_OPERATOR_TOKEN" in out and "<SUPERSEDING_PR_NUMBER>" in out and "a" * 64 in out and TOKEN not in out
    _, bare, _ = go(tmp_path, FakeGh(), plan={k: v for k, v in PLAN.items() if k != "end_stopped"})
    assert "<PRESET_ID>" in bare and "<ITEM_ID>" in bare and "<EXPECTED_DISPATCH_GENERATION>" in bare


def test_the_helper_never_closes_or_ends_the_original(tmp_path):
    fake = FakeGh()
    go(tmp_path, fake, "--execute")
    assert {c[:2] for c in fake.calls} <= READS | {("issue", "create"), ("issue", "edit"), ("issue", "comment")}
    assert all("--remove-label" in c for c in fake.calls if c[:2] == ("issue", "edit"))
    assert not any("close" in c for c in fake.calls)


def test_the_last_step_removes_a_token_and_a_control_character_that_no_earlier_step_saw(tmp_path):
    # The lane and the milestone are printed as they are: only the last redaction and the printable step reach them.
    plan = {**PLAN, "milestone": "v4\x1b[31m\x07.4", "parts": [{**PLAN["parts"][0], "lane": TOKEN}, PLAN["parts"][1]]}
    _, out, err = go(tmp_path, FakeGh(), plan=plan, env={"GH_TOKEN": TOKEN})
    assert TOKEN not in out + err and "would create" in out
    assert all(c == "\n" or c.isprintable() for c in out + err)


@pytest.mark.parametrize("answer", [{"x": 1}, [{"number": 5, "title": "x"}], [{"number": "5", "title": "x", "body": ""}], ["x"]])
def test_an_issue_list_answer_without_the_fields_the_helper_reads_is_an_error_with_no_write(tmp_path, answer):
    fake = FakeGh()
    fake.bad_list = answer
    code, out, err = go(tmp_path, fake, "--execute")
    assert code == 2 and fake.writes() == [] and "no valid" in out + err


def test_the_search_is_for_the_marker_text_and_a_part_the_index_has_not_seen_yet_is_found_in_the_newest_issues(tmp_path):
    fake = FakeGh()
    go(tmp_path, fake, "--execute")
    fake.lag = True
    code, out, _ = go(tmp_path, fake, "--execute")
    assert code == 0 and len(fake.created) == 2 and "already filed as #1001" in out
    searches = [c[c.index("--search") + 1] for c in fake.calls if c[:2] == ("issue", "list") and "--search" in c]
    assert searches and all("Split from #900" in s for s in searches)

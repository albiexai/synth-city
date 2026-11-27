"""#1332 (#1116 part 3): the Supervisor skill moved, and the guide documents every supported script option.

The old skill path stays as a pointer for one release. The guide is the only place that tells a Supervisor how to run the
scripts, so a new script option fails here until the guide names it.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
OLD = ROOT / "skills/deck-factory-supervisor/SKILL.md"
NEW = ROOT / "skills/synth-city-supervisor/SKILL.md"
GUIDE = ROOT / "docs/guide/supervisor-guide.md"
SCRIPTS = ("checkin", "audit_merges", "split")


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    block = text.split("---", 2)[1]
    return dict(line.split(": ", 1) for line in block.strip().splitlines())


def test_the_old_skill_path_still_resolves_and_points_to_the_new_one():
    assert OLD.is_file()
    text = OLD.read_text()
    assert "skills/synth-city-supervisor/SKILL.md" in text
    assert "one release" in text
    assert frontmatter(OLD)["name"] == "deck-factory-supervisor"
    assert len(text.splitlines()) < 30  # a pointer, not a second copy of the skill
    assert not (OLD.parent / "agents").exists()


def test_the_new_skill_is_named_after_its_directory_and_names_itself_in_its_prompt():
    assert frontmatter(NEW)["name"] == NEW.parent.name == "synth-city-supervisor"
    prompt = (NEW.parent / "agents/openai.yaml").read_text()
    assert "$synth-city-supervisor" in prompt and "deck-factory" not in prompt


def test_no_current_file_still_loads_the_skill_by_its_old_name():
    for path in [*ROOT.glob("docs/guide/*.md"), *ROOT.glob("skills/synth-city-supervisor/**/*"), ROOT / "docs/.vitepress/config.ts"]:
        if path.is_file():
            assert "deck-factory-supervisor" not in path.read_text() or path.name == "supervised-factory-playbook.md", path


def test_the_playbook_names_the_old_path_only_to_say_that_it_moved():
    text = (ROOT / "docs/guide/supervised-factory-playbook.md").read_text()
    assert "skills/synth-city-supervisor/SKILL.md" in text
    assert [line for line in text.splitlines() if "deck-factory-supervisor" in line] == [
        line for line in text.splitlines() if "deck-factory-supervisor" in line and "pointer stays at that old path" in line]


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_guide_documents_every_option_of_each_script(script):
    answer = subprocess.run([sys.executable, "-B", str(ROOT / f"scripts/supervisor/{script}.py"), "--help"],
                            capture_output=True, text=True, timeout=60, check=True)
    options = set(re.findall(r"--[a-z][a-z-]*", answer.stdout)) - {"--help"}
    assert options  # the help text lists the options
    guide = GUIDE.read_text()
    assert [option for option in sorted(options) if f"| `{option}` |" not in guide] == []  # each has a row in the options table
    assert f"scripts/supervisor/{script}.py" in guide


def test_the_guide_is_in_the_sidebar_the_index_the_playbook_and_the_skill():
    assert "'/guide/supervisor-guide'" in (ROOT / "docs/.vitepress/config.ts").read_text()
    assert "supervisor-guide.md" in (ROOT / "docs/guide/index.md").read_text()
    assert "/guide/supervisor-guide" in (ROOT / "docs/guide/supervised-factory-playbook.md").read_text()
    assert "docs/guide/supervisor-guide.md" in NEW.read_text()


def test_the_guide_keeps_the_safety_rules_and_the_exit_codes():
    guide = GUIDE.read_text()
    for phrase in ("pkill", "pgrep -f", "Never print a token", "read-only", "SYNTH_CITY_OPERATOR_TOKEN", "mode 600",
                   "--match-head-commit", "dry run", "never closes the original"):
        assert phrase in guide, phrase
    assert guide.count("The exit code is `0`") >= 2


def test_the_guide_keeps_the_corrections_of_review_6093796586():
    guide = GUIDE.read_text()
    for phrase in (
        "Do not stop a native owner session",  # 1: sessions are preserved through maintenance
        "not a confirmed cursor", "highest inbox id:", "Keep your saved id",  # 2: the inbox cursor moves only after a full read
        "/api/v1/external/agent-mail/messages", "recipient_member_id", "SYNTH_CITY_EXTERNAL_ACTOR_TOKEN",  # 3: the Supervisor's identity
        "Zero work items or more than one work item", "keep the finding",  # 4: an ambiguous PR stays a finding
        "auto_merge_cap_reached", "item_not_eligible", "Leader's approval",  # 5: the cap is the sole blocker
        "does not send as the Supervisor",  # 3: the member tool is not the Supervisor's identity
        "Before the call, inspect the exact lease",  # 6: inspect before force-release
    ):
        assert phrase in guide, phrase
    for gone in ("Kill tmux sessions", "or restart the member", "Use `synth_city_send_message`"):
        assert gone not in guide, gone

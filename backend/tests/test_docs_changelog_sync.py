"""The docs changelog page is generated from the root CHANGELOG.md (#935)."""
import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "sync-docs-changelog.py"
spec = importlib.util.spec_from_file_location("sync_docs_changelog", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

ROOT_CHANGELOG = REPO / "CHANGELOG.md"
DOCS_CHANGELOG = REPO / "docs" / "changelog.md"
ENTRY = re.compile(r"^## \[([^\]]+)\]", re.M)
OLD_NAME = "Claude Deck"
LAST_HISTORICAL = (4, 0, 0)  # entries up to 4.0.0 may name the product's former name


def versions(text):
    return [name for name in ENTRY.findall(text) if name != "Unreleased"]


def as_tuple(version):
    return tuple(int(part) for part in version.split("."))


def as_tuple_or_none(name):
    return as_tuple(name) if re.fullmatch(r"\d+\.\d+\.\d+", name) else None


def entries(text):
    """Map each entry heading to its body; the key '' holds the page header."""
    parts = re.split(r"^(?=## \[)", text, flags=re.M)
    result = {"": parts[0]}
    for part in parts[1:]:
        result[ENTRY.match(part).group(1)] = part
    return result


def test_docs_page_is_the_generated_copy_of_the_root_changelog():
    wanted = sync.render(ROOT_CHANGELOG.read_text(encoding="utf-8"))
    assert DOCS_CHANGELOG.read_text(encoding="utf-8") == wanted, (
        "docs/changelog.md is out of date: run python3 scripts/sync-docs-changelog.py"
    )


def test_every_released_version_is_listed_newest_first():
    root = versions(ROOT_CHANGELOG.read_text(encoding="utf-8"))
    page = versions(DOCS_CHANGELOG.read_text(encoding="utf-8"))
    assert page == root
    assert "4.1.0" in page and "4.0.0" in page
    assert page == sorted(page, key=as_tuple, reverse=True)


def test_old_product_name_stays_in_historical_entries():
    page = entries(DOCS_CHANGELOG.read_text(encoding="utf-8"))
    assert OLD_NAME not in page[""], "the page header must use the current name"
    current = [
        name for name in page
        if name and (as_tuple_or_none(name) is None or as_tuple(name) > LAST_HISTORICAL)
    ]
    assert current, "expected at least the Unreleased and 4.1.0 entries"
    for name in current:
        assert OLD_NAME not in page[name], f"entry {name} uses the old product name"
    # The guard is not vacuous: the historical entries do carry the old name.
    assert any(OLD_NAME in page[name] for name in page if name and as_tuple_or_none(name))


def test_page_has_one_title_and_the_generated_notice():
    lines = DOCS_CHANGELOG.read_text(encoding="utf-8").split("\n")
    assert [line for line in lines if line.startswith("# ")] == ["# Changelog"]
    assert "Generated from the root CHANGELOG.md" in lines[2]


def test_render_demotes_inner_titles_but_not_code_fences():
    source = "# Changelog\n\nIntro\n\n## [1.0.0]\n\n# Inner title\n\n```\n# shell comment\n```\n"
    page = sync.render(source)
    assert "### Inner title" in page
    assert "\n# shell comment\n" in page
    assert page.count("\n# ") == 1  # only the shell comment; the title opens the page
    assert page.startswith("# Changelog\n\n<!-- Generated")


def test_render_needs_a_title():
    with pytest.raises(ValueError):
        sync.render("## [1.0.0]\n")


def test_check_fails_when_the_copy_drifts_and_passes_when_synced(tmp_path, capsys):
    source = tmp_path / "CHANGELOG.md"
    target = tmp_path / "changelog.md"
    source.write_text("# Changelog\n\n## [1.0.0]\n\n- first\n", encoding="utf-8")
    args = ["--source", str(source), "--target", str(target)]

    assert sync.main(["--check", *args]) == 1  # no copy yet
    assert "Run: python3 scripts/sync-docs-changelog.py" in capsys.readouterr().err

    assert sync.main(args) == 0
    assert sync.main(["--check", *args]) == 0

    source.write_text("# Changelog\n\n## [1.1.0]\n\n- second\n\n## [1.0.0]\n\n- first\n", encoding="utf-8")
    assert sync.main(["--check", *args]) == 1  # a new release in the root file, not yet in the copy

    target.write_text(target.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8")
    assert sync.main(args) == 0  # writing replaces a hand edit
    assert "hand edit" not in target.read_text(encoding="utf-8")
    assert sync.main(["--check", *args]) == 0


def test_the_docs_build_regenerates_the_page_first():
    scripts = json.loads((REPO / "docs" / "package.json").read_text(encoding="utf-8"))["scripts"]
    assert scripts["prebuild"] == "python3 ../scripts/sync-docs-changelog.py"
    assert scripts["check:changelog"] == "python3 ../scripts/sync-docs-changelog.py --check"


def test_the_always_running_job_checks_the_copy():
    # A pull request that only edits CHANGELOG.md is docs-only and skips the backend suite,
    # so the drift check also runs in the job that always runs (its name is a required check).
    workflow = (REPO / ".github" / "workflows" / "factory-checks.yml").read_text(encoding="utf-8")
    job = workflow.split("  python:\n")[0]
    assert "name: Rename old-name guard" in job
    assert "python scripts/sync-docs-changelog.py --check" in job

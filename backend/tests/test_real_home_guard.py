"""#1122: the test run never writes Synth City's real-home paths, and a deliberate write is caught.

Every case builds a stand-in home and working directory under tmp_path. The real home is never written here.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

from tests import home_guard
from tests.home_guard import Guard

PROBE = ".albiex-synth-city-probe"


@pytest.fixture
def world(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    (home / "seed.txt").write_text("seed")
    return home, cwd


def guard_for(world, environ=None):
    home, cwd = world
    return Guard(str(home), None, str(cwd), environ or {})


# ---- a deliberate write into the stand-in's Synth City paths is caught -----------------------------------------------------


WRITE_ATTEMPTS = {
    "open-w": lambda target, source: open(target, "w").close(),
    "mkdir": lambda target, source: os.mkdir(target),
    "symlink-into": lambda target, source: os.symlink(source, target),
    "rename-into": lambda target, source: os.rename(source, target),
    "sqlite-connect": lambda target, source: sqlite3.connect(target).close(),
}


@pytest.mark.parametrize("attempt", WRITE_ATTEMPTS.values(), ids=WRITE_ATTEMPTS.keys())
def test_a_deliberate_write_under_a_watched_name_is_caught(world, tmp_path, attempt):
    home, _ = world
    source = tmp_path / "source.txt"
    source.write_text("kept")
    guard = guard_for(world)
    before = guard.snapshot()

    attempt(home / PROBE, source)

    assert str(home / PROBE) in home_guard.changed(before, guard.snapshot())


def test_an_in_place_edit_and_a_removal_are_caught(world):
    home, _ = world
    entry, root = home / ".claude-registry-note", home / ".albiex-synth-city"
    entry.write_text("a")
    root.mkdir()
    guard = guard_for(world)
    before = guard.snapshot()

    entry.write_text("bbbb")
    root.rmdir()

    assert {str(entry), str(root)} <= set(home_guard.changed(before, guard.snapshot()))


@pytest.mark.parametrize("root_name", [".albiex-synth-city", ".claude-registry"])
def test_a_write_under_a_watched_root_is_caught_at_any_depth(world, root_name):
    home, _ = world
    deep = home / root_name / "backups"
    deep.mkdir(parents=True)
    guard = guard_for(world)
    before = guard.snapshot()

    (deep / "snapshot.db").write_bytes(b"data")

    assert str(deep / "snapshot.db") in home_guard.changed(before, guard.snapshot())


def test_a_root_that_appears_from_nothing_is_caught(world):
    home, _ = world
    guard = guard_for(world)
    before = guard.snapshot()

    (home / ".albiex-synth-city").mkdir()
    (home / ".albiex-synth-city" / "upgrade.lock").write_text("")
    (home / ".albiex-synth-city-db-upgrade.lock").write_text("")

    changed = home_guard.changed(before, guard.snapshot())
    assert {str(home / ".albiex-synth-city"), str(home / ".albiex-synth-city" / "upgrade.lock"),
            str(home / ".albiex-synth-city-db-upgrade.lock")} <= set(changed)


def test_the_original_home_and_the_account_home_are_both_watched(tmp_path):
    original, account, cwd = tmp_path / "original", tmp_path / "account", tmp_path / "cwd"
    for folder in (original, account, cwd):
        folder.mkdir()
    guard = Guard(str(original), str(account), str(cwd), {})
    before = guard.snapshot()

    (account / ".albiex-synth-city").mkdir()
    (original / ".claude-registry").mkdir()

    assert {str(account / ".albiex-synth-city"), str(original / ".claude-registry")} <= set(home_guard.changed(before, guard.snapshot()))


# ---- what must NOT be reported ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", [".claude", ".codex"])
def test_cli_config_folders_and_their_children_are_never_looked_at(world, name):
    home, _ = world
    guard = guard_for(world)
    before = guard.snapshot()

    (home / name).mkdir()
    (home / name / "created.json").write_text("{}")

    assert home_guard.changed(before, guard.snapshot()) == []


def test_a_protected_folder_inside_a_watched_root_is_not_traversed(world):
    home, _ = world
    root = home / ".albiex-synth-city"
    (root / ".claude").mkdir(parents=True)
    guard = guard_for(world)
    before = guard.snapshot()

    (root / ".claude" / "x.json").write_text("{}")

    assert home_guard.changed(before, guard.snapshot()) == []


def test_unrelated_names_in_the_home_and_next_to_the_database_are_not_reported(world):
    home, cwd = world
    (cwd / "synth_city.db").write_text("db")
    guard = guard_for(world)
    before = guard.snapshot()

    (home / "unrelated.txt").write_text("x")
    (home / ".cache").mkdir()
    (cwd / "other.txt").write_text("x")

    assert home_guard.changed(before, guard.snapshot()) == []


def test_symlinks_under_a_watched_root_are_not_followed(world, tmp_path):
    home, _ = world
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_text("x")
    root = home / ".albiex-synth-city"
    root.mkdir()
    os.symlink(outside, root / "link")

    after = guard_for(world).snapshot()

    assert str(root / "link") in after
    assert not any(key.startswith(str(root / "link") + os.sep) for key in after)


# ---- bounds and unreadable roots are reported, never silent -----------------------------------------------------------------


def test_exceeding_the_entry_bound_is_reported(world, monkeypatch):
    home, _ = world
    root = home / ".albiex-synth-city"
    root.mkdir()
    for index in range(4):
        (root / f"f{index}").write_text("x")
    monkeypatch.setattr(home_guard, "MAX_ENTRIES", 3)

    assert guard_for(world).snapshot()[str(root)] == home_guard.EXCEEDED


def test_exceeding_the_depth_bound_is_reported(world):
    home, _ = world
    root = home / ".albiex-synth-city"
    deep = root.joinpath(*(f"d{index}" for index in range(home_guard.MAX_DEPTH)))
    deep.mkdir(parents=True)
    assert guard_for(world).snapshot()[str(root)] != home_guard.EXCEEDED

    (deep / "too-deep.txt").write_text("x")

    assert guard_for(world).snapshot()[str(root)] == home_guard.EXCEEDED


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any folder")
def test_an_unreadable_root_is_reported(world):
    home, _ = world
    root = home / ".albiex-synth-city"
    root.mkdir()
    root.chmod(0)
    try:
        assert guard_for(world).snapshot()[str(root)] == home_guard.UNREADABLE
    finally:
        root.chmod(0o700)


def test_a_failure_at_the_end_of_the_run_is_reported_even_when_the_baseline_had_it(world, monkeypatch):
    home, _ = world
    root = home / ".albiex-synth-city"
    root.mkdir()
    guard = guard_for(world)
    monkeypatch.setattr(home_guard, "_guard", guard)
    monkeypatch.setattr(home_guard, "MAX_ENTRIES", 1)
    (root / "a").write_text("")
    (root / "b").write_text("")
    monkeypatch.setattr(home_guard, "_baseline", guard.snapshot())      # the same bound failure at the start

    assert str(root) in home_guard.session_changes()


# ---- the four review findings (#1138): bounds while scanning, no-follow traversal, .env comments, an unreadable home ---------


class CountingScandir:
    """os.scandir that counts how many entries are actually pulled from the listings."""

    def __init__(self, real):
        self.real, self.pulled = real, 0

    def __call__(self, target):
        owner = self
        listing = self.real(target)

        class Wrapped:
            def __enter__(self):
                listing.__enter__()
                return self

            def __exit__(self, *exc):
                return listing.__exit__(*exc)

            def __iter__(self):
                for entry in listing:
                    owner.pulled += 1
                    yield entry
        return Wrapped()


def test_the_entry_bound_is_tested_while_scanning_not_after_the_whole_folder_is_read(world, monkeypatch):
    home, _ = world
    root = home / ".albiex-synth-city"
    root.mkdir()
    for index in range(9):
        (root / f"f{index}").write_text("x")
    monkeypatch.setattr(home_guard, "MAX_ENTRIES", 3)
    counting = CountingScandir(os.scandir)
    monkeypatch.setattr(home_guard.os, "scandir", counting)

    result = guard_for(world).snapshot()

    assert result[str(root)] == home_guard.EXCEEDED
    # the home listing reads 2 entries (the watched root and an unrelated file); the walk reads 3 allowed plus the
    # one that trips the bound; the other five entries of the folder are never read
    assert counting.pulled <= 2 + 4


def test_a_watched_root_swapped_for_a_symlink_after_the_metadata_read_is_not_traversed(world, monkeypatch):
    home, _ = world
    (home / ".claude").mkdir()
    (home / ".claude" / "secret.json").write_text("{}")
    root = home / ".albiex-synth-city"
    root.mkdir()
    (root / "inner.txt").write_text("x")
    real_stat, swapped = os.stat, []

    def stat_then_swap(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path) == str(root) and not swapped:
            swapped.append(True)
            os.rename(root, home / "moved-away")
            os.symlink(home / ".claude", root)
        return result

    monkeypatch.setattr(home_guard.os, "stat", stat_then_swap)

    result = guard_for(world).snapshot()

    assert result[str(root)] == home_guard.UNREADABLE
    assert not any("secret.json" in key or ".claude/" in key for key in result)


def test_a_subfolder_swapped_for_a_symlink_after_the_metadata_read_is_not_traversed(world, monkeypatch):
    home, _ = world
    (home / ".claude").mkdir()
    (home / ".claude" / "secret.json").write_text("{}")
    root = home / ".albiex-synth-city"
    (root / "sub").mkdir(parents=True)
    real_stat, swapped = os.stat, []

    def stat_then_swap(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == "sub" and kwargs.get("dir_fd") is not None and not swapped:
            swapped.append(True)
            os.rename(root / "sub", home / "sub-moved")
            os.symlink(home / ".claude", root / "sub")
        return result

    monkeypatch.setattr(home_guard.os, "stat", stat_then_swap)

    result = guard_for(world).snapshot()

    assert result[str(root)] == home_guard.UNREADABLE
    assert not any("secret.json" in key for key in result)


@pytest.mark.parametrize("swapped_levels_up", [1, 2])
def test_an_ancestor_of_a_configured_root_swapped_for_a_symlink_is_not_traversed(world, tmp_path, monkeypatch, swapped_levels_up):
    home, _ = world
    for behind in (home / ".claude" / "files", home / ".claude" / "mid" / "files"):    # what each swap would expose
        behind.mkdir(parents=True)
        (behind / "private-fixture.json").write_text("{}")
    top = tmp_path / "srv"
    root = top / "mid" / "files"
    root.mkdir(parents=True)
    (root / "inner.txt").write_text("x")
    swap_target = root.parent if swapped_levels_up == 1 else top
    guard = guard_for(world, {"BRIDGE_ATTACHMENT_DIR": str(root)})
    real_stat, swapped = os.stat, []

    def stat_then_swap(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path) == str(root) and not swapped:
            swapped.append(True)
            os.rename(swap_target, str(swap_target) + "-moved")
            os.symlink(home / ".claude", swap_target)
        return result

    monkeypatch.setattr(home_guard.os, "stat", stat_then_swap)

    result = guard.snapshot()

    assert result["configured attachment dir"] == home_guard.UNREADABLE
    assert not any("private-fixture" in key for key in result)


def test_the_home_itself_swapped_for_a_symlink_after_the_metadata_read_is_not_traversed(world, tmp_path, monkeypatch):
    home, _ = world
    root = home / ".albiex-synth-city"
    root.mkdir()
    decoy = tmp_path / "decoy"
    (decoy / ".albiex-synth-city").mkdir(parents=True)
    (decoy / ".albiex-synth-city" / "private-fixture.json").write_text("{}")
    guard = guard_for(world)
    real_stat, swapped = os.stat, []

    def stat_then_swap(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path) == str(root) and not swapped:
            swapped.append(True)
            os.rename(home, tmp_path / "home-moved")
            os.symlink(decoy, home)
        return result

    monkeypatch.setattr(home_guard.os, "stat", stat_then_swap)

    result = guard.snapshot()

    assert result[str(root)] == home_guard.UNREADABLE
    assert not any("private-fixture" in key for key in result)


@pytest.mark.parametrize("line,expected", [
    ("DATABASE_URL=sqlite+aiosqlite:///x.db # note", "sqlite+aiosqlite:///x.db"),
    ("DATABASE_URL=sqlite+aiosqlite:///x.db\t# note", "sqlite+aiosqlite:///x.db"),
    ('DATABASE_URL="quoted # kept" # trailing', "quoted # kept"),
    ("DATABASE_URL='single # kept' # trailing", "single # kept"),
    ("DATABASE_URL=  padded  ", "padded"),
    ("DATABASE_URL=no#space#comment", "no#space#comment"),
    ('DATABASE_URL="has \\" escaped quote" # c', 'has " escaped quote'),
    ("DATABASE_URL='has \\' escaped quote'", "has ' escaped quote"),
    ('DATABASE_URL="back\\\\slash"', "back\\slash"),
    ("export DATABASE_URL=exported", "exported"),
    ("database_url = spaced", "spaced"),
])
def test_a_dotenv_value_is_read_the_way_the_app_reads_it(world, line, expected):
    _, cwd = world
    (cwd / ".env").write_text(line + "\n")

    assert home_guard.configured_values({}, str(cwd)) == {"DATABASE_URL": expected}


def test_a_dotenv_escaped_quote_in_the_database_path_still_watches_the_real_file(world, tmp_path):
    _, cwd = world
    real_db = tmp_path / 'we"ird.db'
    (cwd / ".env").write_text('DATABASE_URL="sqlite+aiosqlite:///' + str(real_db).replace('"', '\\"') + '" # c\n')
    guard = guard_for(world)
    before = guard.snapshot()

    real_db.write_text("db")

    assert home_guard.changed(before, guard.snapshot()) == ["configured database"]


def test_a_dotenv_reference_to_another_key_resolves_like_the_app(world, tmp_path):
    _, cwd = world
    parent = tmp_path / "parent"
    (parent / "attachments").mkdir(parents=True)
    (cwd / ".env").write_text(f"DATA_PARENT={parent}\nBRIDGE_ATTACHMENT_DIR=${{DATA_PARENT}}/attachments\n")
    guard = guard_for(world)
    before = guard.snapshot()

    (parent / "attachments" / "upload.bin").write_bytes(b"x")

    assert guard.roots == {"configured attachment dir": os.path.realpath(parent / "attachments")}
    assert "configured attachment dir/upload.bin" in home_guard.changed(before, guard.snapshot())


def test_a_dotenv_reference_to_the_process_environment_resolves_like_the_app(world, tmp_path, monkeypatch):
    _, cwd = world
    monkeypatch.setenv("SYNTH_TEST_PARENT_1122", str(tmp_path / "from-process"))
    (cwd / ".env").write_text("BRIDGE_ATTACHMENT_DIR=${SYNTH_TEST_PARENT_1122}/files\n")

    assert guard_for(world).roots == {"configured attachment dir": os.path.realpath(tmp_path / "from-process" / "files")}


def test_a_dotenv_that_cannot_be_decoded_adds_nothing(world):
    _, cwd = world
    (cwd / ".env").write_bytes(b"DATABASE_URL=\xff\xfe\n")

    assert home_guard.configured_values({}, str(cwd)) == {}


def test_the_dotenv_values_that_are_not_the_three_keys_are_not_kept(world):
    _, cwd = world
    (cwd / ".env").write_text("GITHUB_TOKEN=do-not-keep\nDATABASE_URL=sqlite+aiosqlite:///x.db\n")

    assert set(home_guard.configured_values({}, str(cwd))) == {"DATABASE_URL"}


def test_a_dotenv_database_line_with_a_trailing_comment_still_watches_the_real_file(world, tmp_path):
    _, cwd = world
    real_db = tmp_path / "elsewhere.db"
    (cwd / ".env").write_text(f"DATABASE_URL=sqlite+aiosqlite:///{real_db}   # the real one\n")
    guard = guard_for(world)
    before = guard.snapshot()

    real_db.write_text("db")

    assert home_guard.changed(before, guard.snapshot()) == ["configured database"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any folder")
def test_an_unreadable_home_is_reported_not_skipped(world):
    home, _ = world
    guard = guard_for(world)
    home.chmod(0)
    try:
        assert guard.snapshot()[str(home.resolve())] == home_guard.UNREADABLE
    finally:
        home.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any folder")
def test_an_unreadable_home_fails_the_run_even_when_it_was_unreadable_at_both_ends(world, monkeypatch):
    home, _ = world
    guard = guard_for(world)
    home.chmod(0)
    try:
        monkeypatch.setattr(home_guard, "_guard", guard)
        monkeypatch.setattr(home_guard, "_baseline", guard.snapshot())          # unreadable at the start ...

        assert home_guard.session_changes() == [str(home.resolve())]            # ... and at the end
    finally:
        home.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any folder")
def test_a_named_file_in_an_unreadable_folder_is_reported_and_a_missing_one_is_not(world):
    _, cwd = world
    guard = guard_for(world)
    assert not any(value == home_guard.UNREADABLE for value in guard.snapshot().values())     # absent: nothing to report
    cwd.chmod(0)
    try:
        result = guard.snapshot()
    finally:
        cwd.chmod(0o700)
    assert result["working directory/synth_city.db"] == home_guard.UNREADABLE


# ---- the database file outside the home, and its sidecars --------------------------------------------------------------------


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("name", ["synth_city.db", "claude_registry.db"])
def test_the_default_database_files_in_the_working_directory_are_caught(world, name, suffix):
    _, cwd = world
    guard = guard_for(world)
    before = guard.snapshot()

    (cwd / (name + suffix)).write_text("db")

    assert [key for key in home_guard.changed(before, guard.snapshot())] == [f"working directory/{name}{suffix}"]


def test_a_configured_database_file_is_watched_and_named_by_role_not_by_value(world, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "data.db"
    elsewhere.parent.mkdir()
    guard = guard_for(world, {"database_url": f"sqlite+aiosqlite:///{elsewhere}"})
    before = guard.snapshot()

    elsewhere.write_text("db")
    Path(str(elsewhere) + "-wal").write_text("w")

    changed = home_guard.changed(before, guard.snapshot())
    assert changed == ["configured database", "configured database-wal"]
    assert not any(str(elsewhere) in key for key in changed)


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:///:memory:", "postgresql://user:secret@db.example/x", "not a url"])
def test_an_in_memory_remote_or_malformed_database_url_adds_no_watch(world, url):
    default = guard_for(world).files
    assert guard_for(world, {"DATABASE_URL": url}).files == default


def test_a_relative_database_url_is_taken_against_the_working_directory(world):
    _, cwd = world
    guard = guard_for(world, {"DATABASE_URL": "sqlite+aiosqlite:///./sub/x.db"})
    (cwd / "sub").mkdir()
    before = guard.snapshot()

    (cwd / "sub" / "x.db").write_text("db")

    assert home_guard.changed(before, guard.snapshot()) == ["configured database"]


# ---- configured attachment directories ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("key,label", [("BRIDGE_ATTACHMENT_DIR", "configured attachment dir"),
                                       ("bridge_attachment_agent_root", "configured attachment agent root")])
def test_a_configured_directory_outside_the_home_is_watched_by_role(world, tmp_path, key, label):
    target = tmp_path / "srv" / "files"
    target.mkdir(parents=True)
    guard = guard_for(world, {key: str(target)})
    before = guard.snapshot()

    (target / "upload.bin").write_bytes(b"x")

    changed = home_guard.changed(before, guard.snapshot())
    assert changed == sorted([label, f"{label}/upload.bin"])        # the folder's own time moves with its entries


def test_a_tilde_value_is_taken_against_the_original_home_and_a_relative_one_against_the_working_directory(world):
    home, cwd = world
    (home / "stuff").mkdir()
    (cwd / "rel").mkdir()
    guard = guard_for(world, {"BRIDGE_ATTACHMENT_DIR": "~/stuff", "BRIDGE_ATTACHMENT_AGENT_ROOT": "rel"})
    before = guard.snapshot()

    (home / "stuff" / "a").write_text("x")
    (cwd / "rel" / "b").write_text("x")

    assert home_guard.changed(before, guard.snapshot()) == sorted([
        "configured attachment agent root", "configured attachment agent root/b",
        "configured attachment dir", "configured attachment dir/a"])


# ---- precedence between the environment and a .env, without exposing values --------------------------------------------------


def test_the_environment_wins_over_a_dotenv_and_only_the_three_keys_are_read(world, tmp_path):
    _, cwd = world
    from_env, from_file = tmp_path / "from-env.db", tmp_path / "from-file.db"
    (cwd / ".env").write_text(f"DATABASE_URL=sqlite+aiosqlite:///{from_file}\nexport GITHUB_TOKEN=do-not-keep\nOTHER=1\n")

    assert guard_for(world).files["configured database"] == os.path.realpath(from_file)
    winner = guard_for(world, {"DATABASE_URL": f"sqlite+aiosqlite:///{from_env}"})
    assert winner.files["configured database"] == os.path.realpath(from_env)
    assert os.path.realpath(from_file) not in winner.files.values()
    kept = home_guard.configured_values({}, str(cwd))
    assert set(kept) == {"DATABASE_URL"}


def test_dotenv_keys_are_case_insensitive_and_quotes_are_stripped(world, tmp_path):
    _, cwd = world
    target = tmp_path / "quoted"
    target.mkdir()
    (cwd / ".env").write_text(f"bridge_attachment_dir = \"{target}\"\n# DATABASE_URL=ignored\n")

    assert guard_for(world).roots == {"configured attachment dir": os.path.realpath(target)}


# ---- configured directories that cannot be watched safely are refused and reported ----------------------------------------


@pytest.mark.parametrize("where", ["the-home", "inside-claude", "inside-codex", "claude-itself", "above-the-home"])
def test_a_configured_directory_that_overlaps_the_home_or_the_cli_folders_is_refused_and_never_listed(world, tmp_path, where):
    home, _ = world
    (home / ".claude" / "sub").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".claude" / "sub" / "private.json").write_text("{}")
    value = {"the-home": home, "inside-claude": home / ".claude" / "sub", "inside-codex": home / ".codex",
             "claude-itself": home / ".claude", "above-the-home": tmp_path}[where]
    guard = guard_for(world, {"BRIDGE_ATTACHMENT_DIR": str(value)})

    after = guard.snapshot()

    assert guard.roots == {}
    assert after["configured attachment dir"] == home_guard.REFUSED
    assert not any(".claude" in key or ".codex" in key for key in after)


def test_a_symlink_that_leads_into_a_cli_folder_is_refused(world, tmp_path):
    home, _ = world
    (home / ".claude").mkdir()
    link = tmp_path / "innocent-looking"
    os.symlink(home / ".claude", link)

    guard = guard_for(world, {"BRIDGE_ATTACHMENT_AGENT_ROOT": str(link)})

    assert guard.roots == {} and guard.refused == ["configured attachment agent root"]


def test_a_refused_root_fails_the_run_even_though_it_was_there_at_the_start(world, monkeypatch):
    home, _ = world
    guard = guard_for(world, {"BRIDGE_ATTACHMENT_DIR": str(home)})
    monkeypatch.setattr(home_guard, "_guard", guard)
    monkeypatch.setattr(home_guard, "_baseline", guard.snapshot())

    assert home_guard.session_changes() == ["configured attachment dir"]


# ---- the environment check that runs around every test -----------------------------------------------------------------------


@pytest.fixture
def temp_home():
    with tempfile.TemporaryDirectory(prefix="synth-city-test-elsewhere-") as folder:
        yield folder


def test_a_session_home_and_temporary_settings_are_clean(world, temp_home):
    guard = guard_for(world)
    clean = {"HOME": temp_home, "XDG_DATA_HOME": os.path.join(temp_home, ".local/share"),
             "DATABASE_URL": f"sqlite+aiosqlite:///{temp_home}/x.db", "BRIDGE_ATTACHMENT_DIR": os.path.join(temp_home, "a")}

    assert guard.environment_problems(clean, temp_home) == []


@pytest.mark.parametrize("name", ["HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"])
def test_home_or_an_xdg_variable_pointing_into_a_real_home_is_named(world, temp_home, name):
    home, _ = world
    guard = guard_for(world)

    problems = guard.environment_problems({name: str(home / "sub")}, temp_home)

    assert problems == [f"{name} points into a real home"]


def test_a_deleted_home_that_falls_back_to_the_account_home_is_named(world, temp_home):
    home, _ = world
    guard = guard_for(world)

    assert guard.environment_problems({}, str(home)) == ["~ expands into a real home"]


def test_configuration_pointing_back_at_a_watched_real_path_is_named_without_its_value(world, tmp_path, temp_home):
    home, cwd = world
    secret_place = tmp_path / "secret-place"
    secret_place.mkdir()
    guard = guard_for(world, {"BRIDGE_ATTACHMENT_DIR": str(secret_place), "DATABASE_URL": "sqlite+aiosqlite:///./real.db"})
    leaked = {"HOME": temp_home, "BRIDGE_ATTACHMENT_DIR": str(secret_place / "deeper"),
              "DATABASE_URL": "sqlite+aiosqlite:///./real.db", "bridge_attachment_agent_root": str(home / ".albiex-synth-city" / "x")}

    problems = guard.environment_problems(leaked, temp_home)

    assert sorted(problems) == ["BRIDGE_ATTACHMENT_AGENT_ROOT points at a watched real path",
                                "BRIDGE_ATTACHMENT_DIR points at a watched real path",
                                "DATABASE_URL points at a watched real path"]
    assert not any(str(secret_place) in problem or "real.db" in problem for problem in problems)


# ---- the live session is isolated -------------------------------------------------------------------------------------------


def test_this_run_uses_a_temporary_home_xdg_database_and_attachment_settings():
    home = os.environ["HOME"]
    assert "synth-city-test-home-" in home
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert home in os.environ[name], name
    assert os.path.expanduser("~") == home
    # The settings fixture hides the three configuration names while a test runs; the session values are what
    # every child process and every import sees outside a test body.
    session = sys.modules["tests.conftest"]._session_env
    for name in ("DATABASE_URL", "BRIDGE_ATTACHMENT_DIR", "BRIDGE_ATTACHMENT_AGENT_ROOT"):
        assert home in session[name], name


def test_the_live_guard_watches_the_real_home_and_finds_this_run_clean():
    live = home_guard.guard()

    assert live.homes and not any(home == os.environ["HOME"] for home in live.homes)
    assert live.environment_problems() == []

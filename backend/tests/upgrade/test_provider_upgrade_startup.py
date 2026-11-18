"""T-I1–T-I4: ordinary starts must finish evidence-gated provider upgrades."""
import json
import sqlite3
from pathlib import Path
import zipfile

import pytest

from .provider_upgrade_harness import OLD, NEW, entries, provision, run_startup, customize


@pytest.mark.parametrize("provider", ["claude-code", "muse-cli", "codex-cli", "copilot-cli", "opencode-cli"])
def test_t_i1_pinned_v31_ordinary_start_rewrites_registration(tmp_path, provider):
    install = provision(tmp_path / "install")
    custom_url = customize(install, provider)
    before = entries(install, provider)[OLD]
    result = run_startup(install)
    assert result.returncode == 0, result.stderr[-4000:]
    after = entries(install, provider)
    assert OLD not in after, "ordinary startup left the positively owned old registration"
    assert NEW in after, "ordinary startup did not install the new registration"
    env_key = "environment" if provider == "opencode-cli" else "env"
    assert after[NEW][env_key]["SYNTH_CITY_URL"] == before[env_key]["CLAUDE_DECK_URL"]
    assert after[NEW][env_key]["SYNTH_CITY_PROVIDER"] == before[env_key]["CLAUDE_DECK_PROVIDER"]
    assert not any(key.startswith("CLAUDE_DECK_") for key in after[NEW][env_key])
    assert after[NEW][env_key]["SYNTH_CITY_URL"] == custom_url
    assert after[NEW][env_key]["PRIVATE_SETTING"] == "synthetic-unpublished-value"
    assert after[NEW]["upgrade_custom"] == {"keep": False}
    status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
    assert status["providers"][provider]["state"] == "verified", status["providers"][provider]
    assert custom_url not in json.dumps(status) and "synthetic-unpublished-value" not in json.dumps(status)
    assert not (install.root / "network-attempts").exists(), "tools/list attempted session registration or other network access"
    with sqlite3.connect(install.app_dir / "synth_city.db") as db:
        assert db.execute("SELECT count(*) FROM mail_agent_sessions").fetchone()[0] == len(install.before.records["mail_agent_sessions"])
        backups = db.execute("SELECT file_path FROM backups WHERE name LIKE 'provider-upgrade-%'").fetchall()
    assert backups, "provider rewrite has no durably indexed safety backup"
    for (raw,) in backups:
        path = Path(raw)
        assert path.is_absolute() and path.parent == install.home / ".albiex-synth-city/backups"
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            assert archive.namelist()


def test_t_i2_unowned_registration_remains_byte_identical_and_needs_attention(tmp_path):
    install = provision(tmp_path / "install")
    path = install.home / ".claude.json"
    doc = json.loads(path.read_text())
    doc["mcpServers"][OLD]["args"] = ["/unrelated/agent_mail_server.py"]
    path.write_text(json.dumps(doc, indent=3) + "\n")
    before = path.read_bytes()
    result = run_startup(install)
    assert result.returncode == 0, result.stderr[-4000:]
    assert path.read_bytes() == before
    status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
    assert status["providers"]["claude-code"]["state"] == "needs_attention"


@pytest.mark.parametrize("conflicting_transport", [
    {"type": "remote", "url": "https://unrelated.invalid/mcp"},
    {"type": "remote"},
    {"url": "https://unrelated.invalid/mcp"},
    {"transport": "http"},
])
def test_t_i2_remote_transport_with_residual_shim_is_preserved_at_startup(tmp_path, conflicting_transport):
    install = provision(tmp_path / "install")
    config = install.home / ".config/opencode/opencode.json"
    doc = json.loads(config.read_text())
    doc["mcp"][OLD].update(conflicting_transport)
    config.write_text(json.dumps(doc, indent=3) + "\n")
    before = config.read_bytes()
    old_plugin = config.parent / "plugins/claude-deck-agent-mail.js"
    new_plugin = old_plugin.with_name("albiex-synth-city-agent-mail.js")

    result = run_startup(install)

    assert result.returncode == 0, result.stderr[-4000:]
    assert config.read_bytes() == before, "remote or contradictory transport is not owned local launch evidence"
    status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
    assert status["providers"]["opencode-cli"]["state"] == "needs_attention"
    # The independently owned plugin still upgrades; the MCP entry is never
    # captured as a migration record or falsely reported as verified.
    assert not old_plugin.exists() and new_plugin.is_file()
    assert "Albiex Synth City Agent Mail OpenCode plugin" in new_plugin.read_text()
    records = [json.loads(path.read_text()) for path in
               (install.home / ".albiex-synth-city/provider-upgrades").glob("*.json")]
    assert not any(record["provider"] == "opencode-cli" and record["artifact"] == "mcp" for record in records)
    assert not (install.root / "network-attempts").exists()


@pytest.mark.parametrize("operation", ["remove", "add"])
def test_t_i3_cli_failure_is_incomplete_and_normal_restart_finishes(tmp_path, operation):
    install = provision(tmp_path / "install")
    before = entries(install, "codex-cli")[OLD]
    first = run_startup(install, environment={"PROVIDER_TEST_CLI_FAIL": operation})
    assert first.returncode == 0, first.stderr[-4000:]
    status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
    assert status["providers"]["codex-cli"]["state"] == "incomplete"
    if operation == "remove":
        assert entries(install, "codex-cli")[OLD] == before
    second = run_startup(install)
    assert second.returncode == 0, second.stderr[-4000:]
    after = entries(install, "codex-cli")
    assert OLD not in after and NEW in after
    assert after[NEW]["env"]["SYNTH_CITY_URL"] == before["env"]["CLAUDE_DECK_URL"]


def test_test_instance_skips_provider_discovery_and_mutation(tmp_path):
    install = provision(tmp_path / "install")
    paths = [install.home / name for name in (".claude.json", ".codex/config.toml", ".copilot/mcp-config.json")]
    before = [path.read_bytes() for path in paths]
    result = run_startup(install, test_instance=True,
        code="from app.database import init_db\nasyncio.run(init_db())")
    assert result.returncode == 0, result.stderr[-4000:]
    assert [path.read_bytes() for path in paths] == before
    assert not (install.root / "cli-operations.jsonl").exists()


@pytest.mark.parametrize("owned_mcp,owned_plugin", [(True, False), (False, True), (True, True)])
def test_t_i2_t_i4_opencode_artifacts_have_independent_ownership(tmp_path, owned_mcp, owned_plugin):
    install = provision(tmp_path / "install")
    config = install.home / ".config/opencode/opencode.json"
    old_plugin = config.parent / "plugins/claude-deck-agent-mail.js"
    new_plugin = old_plugin.with_name("albiex-synth-city-agent-mail.js")
    if not owned_mcp:
        doc = json.loads(config.read_text())
        doc["mcp"][OLD]["command"] = ["python", "/unrelated/agent_mail_server.py"]
        config.write_text(json.dumps(doc, indent=3) + "\n")
    if not owned_plugin:
        old_plugin.write_text(old_plugin.read_text().replace("Claude Deck Agent Mail OpenCode plugin", "User-managed plugin", 1))
    before_config, before_plugin = config.read_bytes(), old_plugin.read_bytes()
    result = run_startup(install)
    assert result.returncode == 0, result.stderr[-4000:]
    if owned_mcp:
        assert OLD not in entries(install, "opencode-cli") and NEW in entries(install, "opencode-cli")
    else:
        assert config.read_bytes() == before_config
    if owned_plugin:
        assert not old_plugin.exists()
        assert "Albiex Synth City Agent Mail OpenCode plugin" in new_plugin.read_text()
        assert "export const AlbiexSynthCityAgentMailPlugin" in new_plugin.read_text()
        assert "CLAUDE_DECK_" not in new_plugin.read_text()
    else:
        assert old_plugin.read_bytes() == before_plugin
        assert not new_plugin.exists()


@pytest.mark.parametrize("equal", [True, False])
def test_t_i3_both_names_preserve_matching_values_or_stop_before_mutation(tmp_path, equal):
    install = provision(tmp_path / "install")
    path = install.home / ".claude.json"
    doc = json.loads(path.read_text())
    old = doc["mcpServers"][OLD]
    new = old | {"env": {"SYNTH_CITY_URL": old["env"]["CLAUDE_DECK_URL"], "SYNTH_CITY_PROVIDER": "claude-code"}}
    if not equal:
        new["env"]["SYNTH_CITY_URL"] = "http://conflict.invalid"
    doc["mcpServers"][NEW] = new
    path.write_text(json.dumps(doc, indent=3))
    before = path.read_bytes()
    result = run_startup(install)
    assert result.returncode == 0, result.stderr[-4000:]
    after = entries(install, "claude-code")
    if equal:
        assert OLD not in after and after[NEW] == new
    else:
        assert path.read_bytes() == before
        status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
        assert status["providers"]["claude-code"]["state"] == "needs_attention"


def test_t_i4_copilot_hook_filename_follows_new_registration(tmp_path):
    install = provision(tmp_path / "install")
    old = install.home / ".copilot/hooks/claude-deck-mail.json"
    new = old.with_name(NEW + ".json")
    assert old.is_file() and not new.exists()
    result = run_startup(install)
    assert result.returncode == 0, result.stderr[-4000:]
    assert not old.exists() and new.is_file()
    assert "--deck-url" not in new.read_text()
    assert "--synth-city-url" in new.read_text()


def test_t_i2_plugin_url_literal_is_preserved_when_it_contains_a_generated_identifier(tmp_path):
    install = provision(tmp_path / "install")
    old = install.home / ".config/opencode/plugins/claude-deck-agent-mail.js"
    url = "http://127.0.0.1:43220/process.env.CLAUDE_DECK_URL"
    historical = 'const historical = "process.env.CLAUDE_DECK_PROVIDER";\n'
    old.write_text(old.read_text().replace('"http://127.0.0.1:8000"', json.dumps(url), 1) + historical)
    result = run_startup(install)
    assert result.returncode == 0, result.stderr[-4000:]
    new = old.with_name("albiex-synth-city-agent-mail.js")
    assert json.dumps(url) in new.read_text()
    assert historical in new.read_text()
    assert ' || process.env.SYNTH_CITY_URL || ' in new.read_text()

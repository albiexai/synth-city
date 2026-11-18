"""T-I1–T-I3: interrupted or failed provider upgrades finish at the next ordinary start."""
import json
import sqlite3

import pytest

from .provider_upgrade_harness import OLD, NEW, entries, provision, run_startup


@pytest.mark.parametrize("boundary", ["captured", "backup_durable", "backup_indexed", "old_removed",
                                     "new_applied", "verified", "status_durable"])
def test_t_i3_abrupt_cut_resumes_at_ordinary_start_without_old_registration(tmp_path, boundary):
    install = provision(tmp_path / "install")
    code = '''
import os
from app.services import provider_upgrade as upgrade
def cut(stage, record):
    if stage == BOUNDARY and record.get('provider') == 'codex-cli':
        os._exit(77)
upgrade._checkpoint = cut
'''.replace("BOUNDARY", repr(boundary))
    first = run_startup(install, code=code)
    assert first.returncode == 77, first.stderr[-4000:]
    second = run_startup(install)
    assert second.returncode == 0, second.stderr[-4000:]
    after = entries(install, "codex-cli")
    assert OLD not in after and NEW in after
    status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
    assert status["providers"]["codex-cli"]["state"] == "verified"


@pytest.mark.parametrize("failure", ["capture", "backup", "index", "write", "status"])
def test_t_i3_transient_storage_failure_is_not_completion_and_restarts_finish(tmp_path, failure):
    install = provision(tmp_path / "install")
    code = '''
from app.services import provider_upgrade as u, provider_upgrade_artifacts as a
from pathlib import Path
FAILURE = CHOICE
if FAILURE == 'capture':
    original = a.snapshot
    def failed(path):
        if str(path).endswith('.codex/hooks.json'):
            raise OSError('synthetic unreadable capture')
        return original(path)
    a.snapshot = failed
elif FAILURE in ('backup', 'index'):
    name = '_backup' if FAILURE == 'backup' else '_index_backup'
    original = getattr(u, name)
    if FAILURE == 'index':
        async def failed(record):
            if record['provider'] == 'codex-cli':
                raise OSError('synthetic index failure')
            return await original(record)
    else:
        def failed(record):
            if record['provider'] == 'codex-cli':
                raise OSError('synthetic backup failure')
            return original(record)
    setattr(u, name, failed)
elif FAILURE == 'write':
    original = u._write
    def failed(file, target):
        if str(file['path']).endswith('.codex/config.toml') and target == file['after']:
            raise OSError('synthetic file write failure')
        return original(file, target)
    u._write = failed
else:
    original = u._atomic
    def failed(path, data, mode=0o600):
        if Path(path).name == 'provider-upgrade-status.json' and b'"codex-cli"' in data:
            raise OSError('synthetic status publication failure')
        return original(path, data, mode)
    u._atomic = failed
'''.replace("CHOICE", repr(failure))
    first = run_startup(install, code=code)
    assert first.returncode == 0, first.stderr[-4000:]
    if failure in {"capture", "backup", "index"}:
        assert OLD in entries(install, "codex-cli")
        assert NEW not in entries(install, "codex-cli")
    second = run_startup(install)
    assert second.returncode == 0, second.stderr[-4000:]
    assert OLD not in entries(install, "codex-cli") and NEW in entries(install, "codex-cli")
    status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
    assert status["providers"]["codex-cli"]["state"] == "verified"


def test_t_i3_cut_between_removal_and_stage_publication_recovers_and_backup_precedes_mutation(tmp_path):
    install = provision(tmp_path / "install")
    code = '''
import os, sqlite3, zipfile
from pathlib import Path
from app.services import provider_upgrade as u
original = u._write
def cut(file, target):
    if str(file['path']).endswith('.codex/config.toml') and target == file['removed']:
        with sqlite3.connect('synth_city.db') as db:
            indexed = db.execute("SELECT file_path FROM backups WHERE name LIKE 'provider-upgrade-%'").fetchall()
        assert indexed, 'mutation preceded the backup index barrier'
        assert all(Path(row[0]).is_absolute() and Path(row[0]).is_file() for row in indexed)
        for row in indexed:
            with zipfile.ZipFile(row[0]) as archive:
                assert archive.testzip() is None
        original(file, target)
        os._exit(77)
    return original(file, target)
u._write = cut
'''
    first = run_startup(install, code=code)
    assert first.returncode == 77, first.stderr[-4000:]
    assert OLD not in entries(install, "codex-cli") and NEW not in entries(install, "codex-cli")
    second = run_startup(install)
    assert second.returncode == 0, second.stderr[-4000:]
    assert NEW in entries(install, "codex-cli") and OLD not in entries(install, "codex-cli")
    with sqlite3.connect(install.app_dir / "synth_city.db") as db:
        paths = [row[0] for row in db.execute("SELECT file_path FROM backups WHERE name LIKE 'provider-upgrade-%'")]
    assert len(paths) == len(set(paths)) == len(list((install.home / '.albiex-synth-city/backups').glob('provider-upgrade-*.zip')))


@pytest.mark.parametrize("defect", ["server_name", "missing_tool", "extra_tool", "duplicate_tool", "changed_schema"])
def test_t_i1_protocol_failure_is_incomplete_until_the_actual_shipped_shim_verifies(tmp_path, defect):
    install = provision(tmp_path / "install")
    shim = install.app_dir / "mcp_shim/agent_mail_server.py"
    original = shim.read_text()
    if defect == "server_name":
        broken = original.replace('FastMCP("albiex-synth-city-mail")', 'FastMCP("synthetic-wrong-server")')
    elif defect == "missing_tool":
        broken = original.replace('@mcp.tool()\ndef synth_city_ack_message', 'def synth_city_ack_message')
    elif defect == "extra_tool":
        broken = original.replace('if __name__ == "__main__":',
            '@mcp.tool()\ndef synth_city_unexpected_probe_tool():\n    return {"ok": False}\n\n'
            'if __name__ == "__main__":')
    elif defect == "changed_schema":
        # #1146: the same names and count, but one parameter's type differs from the deployed snapshot.
        broken = original.replace(
            'def synth_city_extend_effort_limit(\n    work_item_id: int,\n    dispatch_nonce: str,\n    minutes: int,',
            'def synth_city_extend_effort_limit(\n    work_item_id: int,\n    dispatch_nonce: str,\n    minutes: str,')
    else:
        # Preserve the exact set while duplicating a wire entry. The count
        # guard must independently refuse this actual tools/list response.
        broken = original.replace('class _UpgradeSafeFastMCP(FastMCP):',
            'class _UpgradeSafeFastMCP(FastMCP):\n'
            '    async def list_tools(self):\n'
            '        tools = await super().list_tools()\n'
            '        return [*tools, tools[0]]\n')
    assert broken != original
    shim.write_text(broken)
    first = run_startup(install)
    assert first.returncode == 0, first.stderr[-4000:]
    status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
    assert status["providers"]["codex-cli"]["state"] == "incomplete"
    assert status["providers"]["codex-cli"]["stage"] != "verified"
    shim.write_text(original)
    second = run_startup(install)
    assert second.returncode == 0, second.stderr[-4000:]
    status = json.loads((install.home / ".albiex-synth-city/provider-upgrade-status.json").read_text())
    assert status["providers"]["codex-cli"]["state"] == "verified"

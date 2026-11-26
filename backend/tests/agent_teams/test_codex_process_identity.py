import pytest

from app.services.codex_process_identity import explicit_resume, plain_start

SESSION = '12345678-1234-1234-1234-123456789abc'


@pytest.mark.parametrize('arguments,expected',[
    ([b'resume',SESSION.encode()],True),
    ([b'--cd',b'/tmp/repo',b'--config',b'effort="high"',b'--model',b'fixture',
      b'--dangerously-bypass-approvals-and-sandbox',b'resume',SESSION.encode(),b'Continue'],True),
    ([b'--',b'resume',SESSION.encode()],False),
    ([b'--model',SESSION.encode(),b'resume',b'other'],False),
    ([b'--config',b'resume',SESSION.encode()],False),
    ([b'resume',SESSION.encode(),b'--last'],False),
    ([b'resume',SESSION.encode(),b'--',b'resume',SESSION.encode()],False),
    ([b'resume',b'--last'],False),
    ([b'exec',b'resume',SESSION.encode()],False),
    ([b'--unsupported',b'resume',SESSION.encode()],False),
])
def test_exact_resume_selector(arguments,expected):
    assert explicit_resume([b'/opt/tools/codex',*arguments,b''],SESSION) is expected


def test_resume_refuses_a_different_executable_or_noncanonical_identity():
    assert not explicit_resume([b'other',b'resume',SESSION.encode()],SESSION)
    assert not explicit_resume([b'codex',b'resume',SESSION.upper().encode()],SESSION.upper())


@pytest.mark.parametrize(("arguments", "expected"), [
    ([], True),
    ([b"--cd", b"/tmp/repo", b"--no-alt-screen", b"Start work"], True),
    ([b"--no-alt-screen", b"Start work", b"Extra argument"], False),
    ([b"resume", SESSION.encode()], False),
    ([b"resume", b"--last"], False),
    ([b"fork"], False),
    ([b"exec", b"Start work"], False),
    ([b"--model"], False),
    ([b"--unsupported"], False),
])
def test_plain_start_requires_codex_options_and_no_resume_selector(arguments, expected):
    assert plain_start([b"/opt/tools/codex", *arguments, b""]) is expected
    assert not plain_start([b"/usr/bin/other", *arguments, b""])


@pytest.mark.parametrize('arguments,accepted',[
    ([b'resume',SESSION.encode()],True),
    ([b'--',b'resume',SESSION.encode()],False),
    ([b'--model',SESSION.encode(),b'resume',b'other'],False),
    ([b'resume',SESSION.encode(),b'--last'],False),
])
def test_real_owner_identity_helper_uses_exact_resume_parser(monkeypatch,arguments,accepted):
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    from app.services import owner_observation_pause as pause
    monkeypatch.setattr(pause.activity,'_process',lambda _pid: ('S','fixture-start'))
    monkeypatch.setattr(pause.activity,'_process_started_at',lambda _start: datetime.now(timezone.utc)-timedelta(minutes=2))
    monkeypatch.setattr(pause,'Path',lambda _path: SimpleNamespace(read_bytes=lambda:b'\0'.join([b'codex',*arguments,b''])))
    slot=SimpleNamespace(provider='codex-cli',launch_options={'session_id':SESSION})
    session=SimpleNamespace(pid=12,created_at=datetime.utcnow(),cwd='/tmp/fixture')
    workspace=SimpleNamespace(leased_owner_pid=11,leased_owner_proc_start='fixture-start')
    if accepted: assert pause._process_identity(slot,session,workspace)=='fixture-start'
    else:
        with pytest.raises(ValueError,match='owner_native_identity_changed'): pause._process_identity(slot,session,workspace)


MUSE_SESSION='01a13c39-4863-7870-8ca7-e5b33ccc8225'


@pytest.mark.parametrize('argv,accepted',[
    ([b'/home/u/.local/bin/muse-bin-1.4.8-R6256.1',b'--trust-workspace',b'--yolo',b'resume',MUSE_SESSION.encode()],True),
    ([b'muse',b'resume',MUSE_SESSION.encode()],True),
    ([b'muse-bin-1.4.8',b'resume',b'01a13c39-0000-7000-8000-000000000000'],False),
    ([b'muse-bin-1.4.8',b'--trust-workspace'],False),
    ([b'/usr/bin/yes',b'resume',MUSE_SESSION.encode()],False),
])
def test_muse_owner_identity_needs_its_executable_and_exact_resume(monkeypatch,argv,accepted):
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    from app.services import owner_observation_pause as pause
    monkeypatch.setattr(pause.activity,'_process',lambda _pid: ('S','fixture-start'))
    monkeypatch.setattr(pause.activity,'_process_started_at',lambda _start: datetime.now(timezone.utc)-timedelta(minutes=2))
    monkeypatch.setattr(pause,'Path',lambda _path: SimpleNamespace(read_bytes=lambda:b'\0'.join([*argv,b''])))
    slot=SimpleNamespace(provider='muse-cli',launch_options={'session_id':MUSE_SESSION})
    session=SimpleNamespace(pid=12,created_at=datetime.utcnow(),cwd='/tmp/fixture')
    workspace=SimpleNamespace(leased_owner_pid=11,leased_owner_proc_start='fixture-start')
    if accepted: assert pause._process_identity(slot,session,workspace)=='fixture-start'
    else:
        with pytest.raises(ValueError,match='owner_native_identity_changed'): pause._process_identity(slot,session,workspace)


AGY_CONVERSATION='6f1c2b8e-4d3a-4b7f-9c2e-1a2b3c4d5e6f'


@pytest.mark.parametrize('argv,accepted',[
    ([b'/home/u/.local/bin/agy',b'--model',b'm-1',b'--conversation',AGY_CONVERSATION.encode()],True),
    ([b'agy',b'--conversation',AGY_CONVERSATION.encode()],True),
    ([b'agy',b'--effort',b'max',b'--dangerously-skip-permissions',b'--conversation',AGY_CONVERSATION.encode()],True),
    ([b'agy',b'--conversation',AGY_CONVERSATION.upper().encode()],True),
    ([b'agy',b'--conversation',b'6f1c2b8e-0000-4000-8000-000000000000'],False),
    ([b'agy',b'-i',AGY_CONVERSATION.encode()],False),
    ([b'agy'],False),
    ([b'agy',b'--conversation'],False),
    ([b'/usr/bin/yes',b'--conversation',AGY_CONVERSATION.encode()],False),
    # Only the builder's own resume form identifies the member (V1 review 5913411113).
    ([b'agy',b'--conversation',AGY_CONVERSATION.encode(),b'--conversation',b'6f1c2b8e-0000-4000-8000-000000000000'],False),
    ([b'agy',b'--conversation',AGY_CONVERSATION.encode(),b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'agy',b'--',b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'agy',b'-i',b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'agy',b'--model',b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'/tmp/agy arbitrary-file',b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'agy',b'--conversation='+AGY_CONVERSATION.encode()],False),
    ([b'agy',b'--conversation',AGY_CONVERSATION.encode(),b'extra'],False),
    ([b'agy',b'--dangerously-skip-permissions',b'--model',b'm-1',b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'agy',b'--model',b'm-1',b'--model',b'm-2',b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'agy',b'--effort',b'ultra',b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'agy-old',b'--conversation',AGY_CONVERSATION.encode()],False),
    ([b'antigravity-cli',b'--conversation',AGY_CONVERSATION.encode()],False),
])
def test_antigravity_owner_identity_needs_its_executable_and_exact_conversation(monkeypatch,argv,accepted):
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    from app.services import owner_observation_pause as pause
    monkeypatch.setattr(pause.activity,'_process',lambda _pid: ('S','fixture-start'))
    monkeypatch.setattr(pause.activity,'_process_started_at',lambda _start: datetime.now(timezone.utc)-timedelta(minutes=2))
    monkeypatch.setattr(pause,'Path',lambda _path: SimpleNamespace(read_bytes=lambda:b'\0'.join([*argv,b''])))
    slot=SimpleNamespace(provider='antigravity-cli',launch_options={'session_id':AGY_CONVERSATION})
    session=SimpleNamespace(pid=12,created_at=datetime.utcnow(),cwd='/tmp/fixture')
    workspace=SimpleNamespace(leased_owner_pid=11,leased_owner_proc_start='fixture-start')
    if accepted: assert pause._process_identity(slot,session,workspace)=='fixture-start'
    else:
        with pytest.raises(ValueError,match='owner_native_identity_changed'): pause._process_identity(slot,session,workspace)


def test_antigravity_owner_identity_needs_a_saved_conversation(monkeypatch):
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    from app.services import owner_observation_pause as pause
    monkeypatch.setattr(pause.activity,'_process',lambda _pid: ('S','fixture-start'))
    monkeypatch.setattr(pause.activity,'_process_started_at',lambda _start: datetime.now(timezone.utc)-timedelta(minutes=2))
    monkeypatch.setattr(pause,'Path',lambda _path: SimpleNamespace(read_bytes=lambda:b'agy\0'))
    slot=SimpleNamespace(provider='antigravity-cli',launch_options={})
    session=SimpleNamespace(pid=12,created_at=datetime.utcnow(),cwd='/tmp/fixture')
    workspace=SimpleNamespace(leased_owner_pid=11,leased_owner_proc_start='fixture-start')
    with pytest.raises(ValueError,match='owner_native_identity_changed'): pause._process_identity(slot,session,workspace)


@pytest.mark.parametrize('cmdline',[
    b'\0agy\0--conversation\0'+AGY_CONVERSATION.encode()+b'\0',
    b'agy\0\0--conversation\0'+AGY_CONVERSATION.encode()+b'\0',
    b'agy\0--conversation\0\0'+AGY_CONVERSATION.encode()+b'\0',
    b'agy\0--conversation\0'+AGY_CONVERSATION.encode()+b'\0\0',
])
def test_antigravity_owner_identity_keeps_empty_argv_fields(monkeypatch,cmdline):
    # Only the final NUL terminator is removed, so an empty argument reaches the exact parser (V1 review 5913494242).
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    from app.services import owner_observation_pause as pause
    monkeypatch.setattr(pause.activity,'_process',lambda _pid: ('S','fixture-start'))
    monkeypatch.setattr(pause.activity,'_process_started_at',lambda _start: datetime.now(timezone.utc)-timedelta(minutes=2))
    monkeypatch.setattr(pause,'Path',lambda _path: SimpleNamespace(read_bytes=lambda:cmdline))
    slot=SimpleNamespace(provider='antigravity-cli',launch_options={'session_id':AGY_CONVERSATION})
    session=SimpleNamespace(pid=12,created_at=datetime.utcnow(),cwd='/tmp/fixture')
    workspace=SimpleNamespace(leased_owner_pid=11,leased_owner_proc_start='fixture-start')
    with pytest.raises(ValueError,match='owner_native_identity_changed'): pause._process_identity(slot,session,workspace)


def test_antigravity_owner_identity_accepts_its_terminated_cmdline(monkeypatch):
    from datetime import datetime,timedelta,timezone
    from types import SimpleNamespace
    from app.services import owner_observation_pause as pause
    monkeypatch.setattr(pause.activity,'_process',lambda _pid: ('S','fixture-start'))
    monkeypatch.setattr(pause.activity,'_process_started_at',lambda _start: datetime.now(timezone.utc)-timedelta(minutes=2))
    cmdline=b'agy\0--conversation\0'+AGY_CONVERSATION.encode()+b'\0'
    monkeypatch.setattr(pause,'Path',lambda _path: SimpleNamespace(read_bytes=lambda:cmdline))
    slot=SimpleNamespace(provider='antigravity-cli',launch_options={'session_id':AGY_CONVERSATION})
    session=SimpleNamespace(pid=12,created_at=datetime.utcnow(),cwd='/tmp/fixture')
    workspace=SimpleNamespace(leased_owner_pid=11,leased_owner_proc_start='fixture-start')
    assert pause._process_identity(slot,session,workspace)=='fixture-start'

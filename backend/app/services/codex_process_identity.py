"""Recognize the Codex launch forms emitted by Albiex Synth City."""
from pathlib import Path
import os
import uuid


def _arguments_after_options(argv: list[bytes]) -> list[bytes] | None:
    if not argv or Path(os.fsdecode(argv[0])).name != "codex":
        return None
    if argv[-1] == b"":
        argv = argv[:-1]
    values = {b"--cd", b"--config", b"--model", b"--profile", b"--profile-v2",
              b"--sandbox", b"--ask-for-approval"}
    flags = {b"--search", b"--no-alt-screen", b"--dangerously-bypass-approvals-and-sandbox"}
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in values:
            if index + 1 >= len(argv) or not argv[index + 1] or argv[index + 1].startswith(b"--"):
                return None
            index += 2
        elif argument in flags:
            index += 1
        else:
            break
    return argv[index:]


def plain_start(argv: list[bytes]) -> bool:
    tail = _arguments_after_options(argv)
    if tail is None or len(tail) > 1:
        return False
    return not tail or (bool(tail[0]) and not tail[0].startswith(b"-")
                        and tail[0] not in {b"resume", b"fork", b"exec", b"e"})


def explicit_resume(argv: list[bytes], session_id: str) -> bool:
    try:
        if str(uuid.UUID(session_id)) != session_id:
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    args = _arguments_after_options(argv)
    if args is None or args[:2] != [b"resume", session_id.encode()]:
        return False
    # Albiex Synth City emits at most one prompt after its explicit selector. Option text
    # after a delimiter cannot select a conversation. Unsupported forms refuse.
    tail = args[2:]
    return len(tail) <= 1 and (not tail or (tail[0] and not tail[0].startswith(b"-")))

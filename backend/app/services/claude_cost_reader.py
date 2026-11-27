"""#1249: per-member Claude Code token use and API-equivalent cost over a campaign window (design §4).

Follows the #1248 spike's reader rules. Each assistant message counts once, at its first row's time, and only inside
the window. A session counts for a member only when it is the session the product recorded for that member's launch;
every other session in the team's folders is unattributed usage, shown and never dropped. The read is bounded and
fails closed: a file that can't be read safely, an unknown format, a missing session or a model without a dated price
reads unknown with a reason, never zero. Codex members are #1250's.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from app.services.pricing_service import PricingService
from app.utils.path_utils import convert_path_to_folder_name

MAX_BYTES = 256 * 1_048_576  # a bound, not an expectation: one long session is tens of megabytes
_FIELDS = ("input", "cache_creation", "cache_read", "output")
_USAGE = {"input": "input_tokens", "cache_creation": "cache_creation_input_tokens",
          "cache_read": "cache_read_input_tokens", "output": "output_tokens"}
_REQUIRED = ("input", "output")  # the cache fields are absent when a request used no cache


@dataclass(frozen=True)
class ClaudeMember:
    member: str
    workspace: str
    session_id: str | None  # the session the product recorded for this member's launch
    provider: str = "claude-code"


@dataclass
class ModelUsage:
    model: str
    messages: int = 0
    input: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output: int = 0
    cost_usd: float | None = 0.0  # None when the model has no dated price


@dataclass
class MemberCost:
    member: str
    messages: int = 0
    models: list[ModelUsage] = field(default_factory=list)
    cost_usd: float | None = None
    unknown_reasons: list[str] = field(default_factory=list)


@dataclass
class UnattributedUsage:
    workspace: str
    session_id: str
    messages: int = 0
    models: list[ModelUsage] = field(default_factory=list)
    unknown_reasons: list[str] = field(default_factory=list)


@dataclass
class CostReport:
    prices_as_of: date
    members: list[MemberCost]
    unattributed: list[UnattributedUsage]
    members_priced: int
    members_total: int


class _Unknown(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def read_claude_usage(start: datetime, end: datetime, members: list[ClaudeMember], *, home: Path, uid: int,
                      config_dir: Path | None = None, max_bytes: int = MAX_BYTES) -> CostReport:
    """Read the team's Claude Code sessions over ``[start, end)``. ``home`` and ``uid`` are the agents' user's."""
    start, end = _utc(start), _utc(end)
    root = (config_dir or home / ".claude") / "projects"
    pricing = PricingService()
    # Each transcript folder once, however its workspace is spelled. A Codex member's workspace is listed too: an
    # earlier Claude Code session there is unattributed usage.
    folders: dict[Path, Path] = {}
    for member in members:
        workspace = Path(member.workspace).resolve()
        folders.setdefault(root / convert_path_to_folder_name(str(workspace)), workspace)
    claims: dict[tuple[Path, str], list[str]] = {}
    for member in members:
        if member.provider == "claude-code" and _canonical(member.session_id):
            claims.setdefault((_folder(root, member), member.session_id), []).append(member.member)
    results = []
    for member in members:
        if member.provider != "claude-code":
            reason = "harness_not_read"
        elif not _canonical(member.session_id):
            reason = "binding_unverified"
        elif len(claims[(_folder(root, member), member.session_id)]) > 1:
            reason = "competing_binding"  # the session is shown once, unattributed
        else:
            try:
                models = _read(_folder(root, member) / f"{member.session_id}.jsonl", member.session_id,
                               Path(member.workspace).resolve(), uid, max_bytes, start, end, pricing)
            except _Unknown as unknown:
                reason = unknown.reason
            else:
                results.append(_member(member.member, models))
                continue
        results.append(MemberCost(member.member, unknown_reasons=[reason]))
    unattributed = []
    for folder, workspace in sorted(folders.items()):
        for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else ():
            claimants = claims.get((folder, path.stem), [])
            if len(claimants) == 1:
                continue
            reasons = ["competing_binding"] if claimants else []
            try:
                models = _read(path, path.stem, workspace, uid, max_bytes, start, end, pricing)
            except _Unknown as unknown:
                unattributed.append(UnattributedUsage(str(workspace), path.stem, unknown_reasons=[*reasons,
                                                                                                   unknown.reason]))
                continue
            if models or claimants:
                unattributed.append(UnattributedUsage(str(workspace), path.stem, sum(m.messages for m in models),
                                                      models, reasons + _price_reasons(models)))
    return CostReport(prices_as_of=PricingService.PRICES_AS_OF, members=results, unattributed=unattributed,
                      members_priced=sum(1 for m in results if m.cost_usd is not None), members_total=len(results))


def _canonical(session_id: str | None) -> bool:
    """A recorded session is a canonical UUID, so it names one file in the member's own folder."""
    try:
        return isinstance(session_id, str) and str(UUID(session_id)) == session_id
    except ValueError:
        return False


def _folder(root: Path, member: ClaudeMember) -> Path:
    return root / convert_path_to_folder_name(str(Path(member.workspace).resolve()))


def _price_reasons(models: list[ModelUsage]) -> list[str]:
    return sorted(f"price_unknown:{m.model}" for m in models if m.cost_usd is None)


def _member(name: str, models: list[ModelUsage]) -> MemberCost:
    unpriced = _price_reasons(models)
    cost = None if unpriced else sum(m.cost_usd for m in models)
    return MemberCost(name, sum(m.messages for m in models), models, cost, unpriced)


def _read(path: Path, session_id: str, workspace: Path, uid: int, max_bytes: int, start: datetime, end: datetime,
          pricing: PricingService) -> list[ModelUsage]:
    """One session's window usage by model, from a bounded read of its own file. Every row must be this session's."""
    try:
        descriptor: int | None = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise _Unknown("no_native_session") from None
    except OSError:
        raise _Unknown("native_log_unreadable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _Unknown("native_log_unreadable")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None  # the stream closes it now
        with stream:
            data = stream.read(max_bytes + 1)  # one byte past the bound shows the file is over it
    except OSError:
        raise _Unknown("native_log_unreadable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(data) > max_bytes:
        raise _Unknown("native_log_unreadable")
    seen: set[str] = set()
    by_model: dict[str, ModelUsage] = {}
    for line in data.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise _Unknown("format_unknown") from None
        if not isinstance(row, dict) or not isinstance(row.get("type"), str):
            raise _Unknown("format_unknown")
        if row["type"] != "assistant":
            continue  # a user turn, a system notice or a summary carries no usage
        message_id, model, at, tokens = _message(row)
        cwd = row.get("cwd")
        if (row.get("sessionId") != session_id or not isinstance(cwd, str) or not Path(cwd).is_absolute()
                or not Path(cwd).resolve().is_relative_to(workspace)):
            raise _Unknown("session_mismatch")
        if message_id in seen:
            continue  # one message is written as one row per content block: count it once, at its first row
        seen.add(message_id)
        if not start <= at < end:
            continue
        usage = by_model.setdefault(model, ModelUsage(model))
        usage.messages += 1
        usage.input += tokens["input"]
        usage.cache_creation += tokens["cache_creation"]
        usage.cache_read += tokens["cache_read"]
        usage.output += tokens["output"]
        price = pricing.dated_price(model)
        if price is None or usage.cost_usd is None:
            usage.cost_usd = None
        else:  # the request's whole prompt picks its tier, and that tier prices every class, output included
            prompt = tokens["input"] + tokens["cache_creation"] + tokens["cache_read"]
            tier = "_above_200k" if prompt > PricingService.TIERED_THRESHOLD else ""
            usage.cost_usd += sum(tokens[name] * price.get(f"{name}{tier}", price[name]) for name in _FIELDS)
    return sorted(by_model.values(), key=lambda usage: usage.model)


def _message(row: dict) -> tuple[str, str, datetime, dict[str, int]]:
    message = row.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    message_id = message.get("id") if isinstance(message, dict) else None
    model = message.get("model") if isinstance(message, dict) else None
    stamp = row.get("timestamp")
    if not (isinstance(usage, dict) and isinstance(message_id, str) and message_id and isinstance(model, str)
            and model and isinstance(stamp, str)):
        raise _Unknown("format_unknown")
    tokens = {}
    for name, key in _USAGE.items():
        value = usage.get(key, None if name in _REQUIRED else 0)
        if type(value) is not int or value < 0:
            raise _Unknown("format_unknown")
        tokens[name] = value
    try:
        at = _utc(datetime.fromisoformat(stamp))
    except ValueError:
        raise _Unknown("format_unknown") from None
    return message_id, model, at, tokens


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("a window edge needs a time zone")
    return value.astimezone(UTC)

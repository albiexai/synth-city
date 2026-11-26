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
    folders = {member.workspace: root / convert_path_to_folder_name(str(Path(member.workspace).resolve()))
               for member in members if member.provider == "claude-code"}
    bound = {(member.workspace, member.session_id) for member in members
             if member.provider == "claude-code" and member.session_id}
    results = []
    for member in members:
        if member.provider != "claude-code":
            results.append(MemberCost(member.member, unknown_reasons=["harness_not_read"]))
        elif not member.session_id:
            results.append(MemberCost(member.member, unknown_reasons=["binding_unverified"]))
        else:
            path = folders[member.workspace] / f"{member.session_id}.jsonl"
            if not path.exists() and not path.is_symlink():
                results.append(MemberCost(member.member, unknown_reasons=["no_native_session"]))
                continue
            try:
                models = _read(path, uid, max_bytes, start, end, pricing)
            except _Unknown as unknown:
                results.append(MemberCost(member.member, unknown_reasons=[unknown.reason]))
                continue
            results.append(_member(member.member, models))
    unattributed = []
    for workspace, folder in sorted(folders.items()):
        for path in sorted(folder.glob("*.jsonl")) if folder.is_dir() else ():
            if (workspace, path.stem) in bound:
                continue
            try:
                models = _read(path, uid, max_bytes, start, end, pricing)
            except _Unknown as unknown:
                unattributed.append(UnattributedUsage(workspace, path.stem, unknown_reasons=[unknown.reason]))
                continue
            if models:
                unattributed.append(UnattributedUsage(workspace, path.stem, sum(m.messages for m in models), models))
    return CostReport(prices_as_of=PricingService.PRICES_AS_OF, members=results, unattributed=unattributed,
                      members_priced=sum(1 for m in results if m.cost_usd is not None), members_total=len(results))


def _member(name: str, models: list[ModelUsage]) -> MemberCost:
    unpriced = sorted(f"price_unknown:{m.model}" for m in models if m.cost_usd is None)
    cost = None if unpriced else sum(m.cost_usd for m in models)
    return MemberCost(name, sum(m.messages for m in models), models, cost, unpriced)


def _read(path: Path, uid: int, max_bytes: int, start: datetime, end: datetime,
          pricing: PricingService) -> list[ModelUsage]:
    """One session's window usage by model, from a bounded read of its own file."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise _Unknown("native_log_unreadable") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise _Unknown("native_log_unreadable")
        data = stream.read(max_bytes + 1)  # one byte past the bound shows the file is over it
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
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        message_id, model, at, tokens = _message(row)
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
        else:  # per message, as a request's tokens decide its tier
            usage.cost_usd += sum(pricing.calculate_tiered_cost(
                tokens[name], price.get(name), price.get(f"{name}_above_200k")) for name in _FIELDS)
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

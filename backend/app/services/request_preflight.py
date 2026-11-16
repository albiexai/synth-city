"""Mechanical checks for plan, scope-amendment and continuation requests (#937).

The same functions serve two callers. The preflight route reports every problem of a request
before it is submitted, writing nothing. The submit methods in ``github_approval_service`` call
them in place of their former inline checks and raise the first problem with its own code and
status, so a request that preflight passes is never refused for these rules and a refused one
fails exactly as before. State that needs the database (budgets, pending requests, leases, the
repository head) stays with the submit methods.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.config import settings
from app.services import companion_paths

_EXECUTION_TARGETS = {"workspace", "hosted_ci", "workspace_and_hosted_ci"}
# A request estimated this close to the window gets a warning, not a problem.
WINDOW_WARNING_MARGIN_MINUTES = 10

_FIELDS = {
    "continuation_phase_invalid": "phase",
    "amendment_diagnostic_restoration_required": "phase",
    "execution_target_invalid": "execution_target",
    "continuation_summary_required": "summary",
    "continuation_summary_reserved": "summary",
    "approval_summary_required": "summary",
    "allowed_paths_invalid": "allowed_paths",
    "allowed_paths_required": "allowed_paths",
    "amendment_closed_file_paths_required": "allowed_paths",
    "continuation_path_limit_exceeded": "allowed_paths",
    "allowed_actions_invalid": "allowed_actions",
    "allowed_actions_required": "allowed_actions",
    "implementation_completion_actions_required": "allowed_actions",
    "diagnostic_revert_required": "allowed_actions",
    "allowed_commands_invalid": "allowed_commands",
    "continuation_command_limit_exceeded": "allowed_commands",
    "hosted_diagnostic_local_build_forbidden": "allowed_commands",
    "prohibited_actions_invalid": "prohibited_actions",
    "tool_fallbacks_invalid": "tool_fallbacks",
    "hosted_tool_target_required": "tool_fallbacks",
    "hosted_tool_fallback_required": "tool_fallbacks",
    "continuation_failed_head_limit_exceeded": "max_failed_heads",
    "max_failed_heads_invalid": "max_failed_heads",
    "plan_allowed_paths_missing": "plan_metadata.allowed_paths",
    "plan_metadata_reserved_key": "plan_metadata",
    "estimate_invalid": "estimate_active_minutes",
    "estimate_exceeds_window": "estimate_active_minutes",
    "estimate_near_window": "estimate_active_minutes",
    "unread_authority_mail": "mail",
    "stale_nonce": "dispatch_nonce",
    "lease_token_mismatch": "lease_token",
    "companion_paths_added": "allowed_paths",
    "request_schema_invalid": "request",
}

_MESSAGES = {
    "continuation_phase_invalid": "phase must be implementation or diagnostic.",
    "amendment_diagnostic_restoration_required": "A scope amendment is an implementation request, and the attempt must be in implementation.",
    "execution_target_invalid": "execution_target must be workspace, hosted_ci or workspace_and_hosted_ci.",
    "continuation_summary_required": "The summary is empty.",
    "continuation_summary_reserved": "The summary contains the reserved companion-path trailer line.",
    "approval_summary_required": "The summary is empty.",
    "allowed_paths_invalid": "Each path must be a repo-relative file or directory with no '..', no absolute or backslash form and no glob character.",
    "allowed_paths_required": "At least one allowed path is required.",
    "amendment_closed_file_paths_required": "An amendment names closed files, not directories (no trailing slash).",
    "continuation_path_limit_exceeded": "The request names more paths or commands than the scope allows.",
    "allowed_actions_invalid": "allowed_actions holds an empty or unknown action.",
    "allowed_actions_required": "At least one allowed action is required.",
    "implementation_completion_actions_required": "An implementation request needs push_pr_head and request_verification, and only known actions.",
    "diagnostic_revert_required": "A diagnostic request needs revert_diagnostic_changes.",
    "allowed_commands_invalid": "allowed_commands holds an empty or non-string command.",
    "continuation_command_limit_exceeded": "The request names more commands than the scope allows.",
    "hosted_diagnostic_local_build_forbidden": "A hosted-only diagnostic must not name a local build command.",
    "prohibited_actions_invalid": "prohibited_actions holds an empty or non-string entry.",
    "tool_fallbacks_invalid": "A tool fallback needs exactly target, if_missing, package and revert_required with the fixed hosted values.",
    "hosted_tool_target_required": "A tool fallback needs a hosted execution target.",
    "hosted_tool_fallback_required": "install_hosted_ci_tool and tool_fallbacks must be given together.",
    "continuation_failed_head_limit_exceeded": "max_failed_heads is above the scope's limit per revision, or below one for an amendment.",
    "max_failed_heads_invalid": "max_failed_heads must be a whole number.",
    "plan_allowed_paths_missing": "plan_metadata.allowed_paths must be a non-empty list of closed file paths; later amendments read it.",
    "plan_metadata_reserved_key": "plan_metadata uses a key the server reserves for companion paths.",
    "estimate_invalid": "The estimate must be a positive finite number of minutes, or an object with high (and optionally low) minutes.",
    "estimate_exceeds_window": "The estimate is longer than the current authority window.",
    "estimate_near_window": "The estimate is close to the end of the current authority window.",
    "unread_authority_mail": "Unread stop, hold or decision Mail from your Leader, Supervisor or Synth City is waiting; read your inbox first.",
    "stale_nonce": "The dispatch nonce is not the current one.",
    "lease_token_mismatch": "The lease token does not match the workspace lease.",
    "companion_paths_added": "The checked-in companion map adds paths the request does not name.",
    "request_schema_invalid": "The request breaks a limit or type the submit route enforces.",
}


@dataclass(frozen=True)
class Problem:
    code: str
    field: str
    message: str
    status_code: int = 409

    def as_dict(self) -> dict:
        return {"code": self.code, "field": self.field, "message": self.message}


def problem(code: str, status_code: int = 409, *, message: str | None = None, field_name: str | None = None) -> Problem:
    return Problem(
        code,
        field_name or _FIELDS.get(code, "request"),
        message or _MESSAGES.get(code, code.replace("_", " ")),
        status_code,
    )


@dataclass
class Normalized:
    """The canonical values of a request. A field that failed is ``None`` and the others stay usable."""

    paths: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    prohibitions: list = field(default_factory=list)
    fallbacks: dict = field(default_factory=dict)


@dataclass
class PreflightResult:
    problems: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict:
        return {
            "passed": self.ok,
            "problems": [item.as_dict() for item in self.problems],
            "warnings": [item.as_dict() for item in self.warnings],
        }


def _scalar(value):
    """A string stays itself; anything else (a list, an object, a number) becomes ``None``.

    A request field of the wrong type must be reported, never used in a set membership test, where an
    unhashable value would raise. The raw value is still handed to the schema check for the report.
    """
    return value if isinstance(value, str) else None


def _service():
    # Imported on use: the approval service imports this module, so a top-level import would be a cycle.
    from app.services import github_approval_service as module

    return module


def _attempt(problems: list, function, *args, **kwargs):
    """Run a canonicalising helper; a refusal becomes a problem and the value ``None``."""
    module = _service()
    try:
        return function(*args, **kwargs)
    except module.GithubApprovalError as error:
        problems.append(problem(error.detail, error.status_code))
        return None


# --- Continuation ---------------------------------------------------------------------------


def continuation_header(phase, execution_target, summary) -> list:
    """The checks a continuation request meets before its owner and lease are looked at."""
    phase, execution_target = _scalar(phase), _scalar(execution_target)
    problems = []
    if phase not in {"implementation", "diagnostic"}:
        problems.append(problem("continuation_phase_invalid", 400))
    if execution_target not in _EXECUTION_TARGETS:
        problems.append(problem("execution_target_invalid", 400))
    if not isinstance(summary, str) or not summary.strip():
        problems.append(problem("continuation_summary_required", 400))
    return problems


def continuation_body(
    *, phase, execution_target, allowed_paths, allowed_actions, allowed_commands, prohibited_actions,
    max_failed_heads, tool_fallbacks, scope,
):
    """Every problem in a continuation request's paths, actions, commands, fallbacks and limits.

    Returns ``(problems, normalized)``. Each field is judged on its own: a field that failed is ``None`` in
    ``normalized`` and the rest stay usable, so one bad field never hides another field's problem.
    """
    module = _service()
    service = module.github_approval_service
    phase, execution_target = _scalar(phase), _scalar(execution_target)
    problems: list = []
    paths = _attempt(problems, service._canonical_paths, allowed_paths)
    actions = _attempt(problems, service._canonical_strings, allowed_actions,
                       label="allowed_actions", allow_empty=False)
    if actions is not None:
        if set(actions) - module._CONTINUATION_ACTIONS:
            problems.append(problem("allowed_actions_invalid", 400))
        elif phase == "implementation" and not module.IMPLEMENTATION_COMPLETION_ACTIONS <= set(actions):
            problems.append(problem("implementation_completion_actions_required", 400))
    commands = _attempt(problems, service._canonical_strings, allowed_commands, label="allowed_commands")
    prohibitions = _attempt(problems, service._canonical_strings, prohibited_actions, label="prohibited_actions")
    fallbacks = _attempt(problems, service._canonical_tool_fallbacks, tool_fallbacks,
                         execution_target=execution_target)
    if actions is not None and fallbacks is not None and (phase in {"implementation", "diagnostic"}):
        if phase == "diagnostic":
            if "revert_diagnostic_changes" not in actions:
                problems.append(problem("diagnostic_revert_required", 400))
            if commands is not None and execution_target == "hosted_ci" and any(
                module._HOSTED_ONLY_LOCAL_BUILD.search(command) for command in commands
            ):
                problems.append(problem("hosted_diagnostic_local_build_forbidden", 400))
            if ("install_hosted_ci_tool" in actions) != bool(fallbacks):
                problems.append(problem("hosted_tool_fallback_required", 400))
        elif "install_hosted_ci_tool" in actions and not fallbacks:
            problems.append(problem("hosted_tool_fallback_required", 400))
    if paths is not None and len(paths) > scope.max_scope_paths:
        problems.append(problem("continuation_path_limit_exceeded"))
    if commands is not None and len(commands) > scope.max_scope_commands:
        problems.append(problem("continuation_command_limit_exceeded"))
    if type(max_failed_heads) is not int:
        problems.append(problem("max_failed_heads_invalid", 400))
    elif max_failed_heads > scope.max_failed_heads_per_revision:
        problems.append(problem("continuation_failed_head_limit_exceeded"))
    return problems, Normalized(paths, actions, commands, prohibitions, fallbacks)


# --- Scope amendment ------------------------------------------------------------------------


def amendment_body(
    *, phase, attempt_phase, execution_target, summary, allowed_paths, allowed_actions, allowed_commands,
    prohibited_actions, max_failed_heads, tool_fallbacks, scope,
):
    """Every problem in an amendment request that needs no database state.

    Returns ``(problems, normalized)`` with each field judged on its own, as in ``continuation_body``.
    """
    module = _service()
    service = module.github_approval_service
    phase, execution_target = _scalar(phase), _scalar(execution_target)
    problems: list = []
    if phase != "implementation" or attempt_phase != "implementation":
        problems.append(problem("amendment_diagnostic_restoration_required"))
    if execution_target not in _EXECUTION_TARGETS:
        problems.append(problem("execution_target_invalid", 400))
    paths = _attempt(problems, service._canonical_paths, allowed_paths)
    if paths is not None and any(path.endswith("/") for path in paths):
        problems.append(problem("amendment_closed_file_paths_required"))
    actions = _attempt(problems, service._canonical_strings, allowed_actions,
                       label="allowed_actions", allow_empty=False)
    if actions is not None and (
        set(actions) - module._CONTINUATION_ACTIONS or not module.IMPLEMENTATION_COMPLETION_ACTIONS <= set(actions)
    ):
        problems.append(problem("implementation_completion_actions_required", 400))
    commands = _attempt(problems, service._canonical_strings, allowed_commands, label="allowed_commands")
    prohibitions = _attempt(problems, service._canonical_strings, prohibited_actions, label="prohibited_actions")
    fallbacks = _attempt(problems, service._canonical_tool_fallbacks, tool_fallbacks,
                         execution_target=execution_target)
    if actions is not None and fallbacks is not None and bool(fallbacks) != ("install_hosted_ci_tool" in actions):
        problems.append(problem("hosted_tool_fallback_required", 400))
    # The amendment path has always reported a command overflow with the path code.
    if paths is not None and len(paths) > scope.max_scope_paths:
        problems.append(problem("continuation_path_limit_exceeded"))
    if commands is not None and len(commands) > scope.max_scope_commands:
        problems.append(problem("continuation_path_limit_exceeded"))
    if not isinstance(summary, str) or not summary.strip():
        problems.append(problem("continuation_summary_required", 400))
    if (type(max_failed_heads) is not int or max_failed_heads < 1
            or max_failed_heads > scope.max_failed_heads_per_revision):
        problems.append(problem("continuation_failed_head_limit_exceeded"))
    return problems, Normalized(paths, actions, commands, prohibitions, fallbacks)


# --- Initial plan, companions, estimate ------------------------------------------------------


def initial_plan_problems(summary, plan_metadata) -> list:
    problems = []
    if not isinstance(summary, str) or not summary.strip():
        problems.append(problem("approval_summary_required", 400))
    if not isinstance(plan_metadata, dict):
        plan_metadata = {}
    if companion_paths.RESERVED_KEYS & set(plan_metadata):
        problems.append(problem("plan_metadata_reserved_key", 400))
    service = _service().github_approval_service
    listed = service.initial_plan_paths(plan_metadata)
    if listed is None:
        problems.append(problem("plan_allowed_paths_missing"))
    else:
        canonical = _attempt(problems, service._canonical_paths, listed)
        if canonical is not None and any(path.endswith("/") for path in canonical):
            problems.append(problem("amendment_closed_file_paths_required",
                                    message="Name closed files, not directories: a later amendment needs a closed list."))
    return problems


def companion_warning(paths) -> list:
    """A warning naming the companions the server will add to a path list, or nothing."""
    if not paths:
        return []
    try:
        extras = companion_paths.companions_for(paths)
    except Exception:
        return []
    if not extras:
        return []
    shown = ", ".join(extras[:8]) + (", ..." if len(extras) > 8 else "")
    return [problem("companion_paths_added", message=f"The companion map adds: {shown}.")]


def companion_limit_problems(paths, scope) -> list:
    """The path limit again, counted with the companions the server adds to the request."""
    if not paths or len(paths) > scope.max_scope_paths:
        return []
    try:
        extras = companion_paths.companions_for(paths)
    except Exception:
        return []
    if len(set(paths) | set(extras)) > scope.max_scope_paths:
        return [problem("continuation_path_limit_exceeded",
                        message="With the companion paths the server adds, the request is over the scope's path limit.")]
    return []


def _default(value):
    return [] if value is None else value


def schema_problems(kind, *, summary, plan_metadata=None, phase=None, execution_target=None, allowed_paths=None,
                    allowed_actions=None, allowed_commands=None, prohibited_actions=None, max_failed_heads=None,
                    tool_fallbacks=None) -> list:
    """What the real submit routes would refuse with a 422, judged by the very same request models.

    Only the library's own constraint text is reported, never the submitted value. The nonce and lease are
    checked by the route, so placeholders stand in for them here.
    """
    from pydantic import ValidationError

    from app.models import schemas

    if kind == "initial_plan":
        model = schemas.MailApprovalRequestCreate
        values = {"work_item_id": 0, "dispatch_nonce": "x", "summary": summary,
                  "plan_metadata": plan_metadata if isinstance(plan_metadata, dict) else {}}
    else:
        model = (schemas.GithubScopeAmendmentCreate if kind == "scope_amendment"
                 else schemas.GithubContinuationProposalCreate)
        values = {"dispatch_nonce": "x", "lease_token": "x", "summary": summary, "phase": phase,
                  "execution_target": execution_target, "allowed_paths": _default(allowed_paths),
                  "allowed_actions": _default(allowed_actions), "allowed_commands": _default(allowed_commands),
                  "prohibited_actions": _default(prohibited_actions), "max_failed_heads": max_failed_heads,
                  "tool_fallbacks": tool_fallbacks if tool_fallbacks is not None else {}}
        if kind == "scope_amendment" and phase is None:
            values.pop("phase")
    try:
        model(**values)
    except ValidationError as error:
        return [
            problem("request_schema_invalid", 422, field_name=str(item["loc"][0]) if item["loc"] else "request",
                    message=f"{'.'.join(str(part) for part in item['loc'][1:]) or str(item['loc'][0])}: {item['msg']}"
                    if len(item["loc"]) > 1 else f"{item['msg']}")
            for item in error.errors(include_input=False, include_url=False, include_context=False)
        ]
    return []


def authority_window_minutes(scope=None) -> float:
    """The authority window a request is judged against.

    Today this is the one fixed window every revision gets. #938 derives it from an approved estimate and a
    scope maximum, and replaces only this function.
    """
    return settings.github_continuation_proposal_expiry_seconds / 60


def estimate_upper_bound(estimate):
    """The largest minutes the estimate allows, or ``None`` when it is not a usable estimate."""
    def number(value):
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and math.isfinite(value) and value > 0)

    if isinstance(estimate, dict):
        if set(estimate) - {"low", "high"} or "high" not in estimate or not number(estimate["high"]):
            return None
        if "low" in estimate and (not number(estimate["low"]) or estimate["low"] > estimate["high"]):
            return None
        return float(estimate["high"])
    return float(estimate) if number(estimate) else None


def estimate_problems(estimate, scope=None):
    """``(problems, warnings)`` for an optional estimate against the current window; silent without one."""
    if estimate is None:
        return [], []
    upper = estimate_upper_bound(estimate)
    if upper is None:
        return [problem("estimate_invalid", 400)], []
    window = authority_window_minutes(scope)
    if upper > window:
        return [problem("estimate_exceeds_window",
                        message=f"The estimate's upper bound is {upper:g} minutes; the current authority window is {window:g}.")], []
    if upper > window - WINDOW_WARNING_MARGIN_MINUTES:
        return [], [problem("estimate_near_window",
                            message=f"The estimate's upper bound is {upper:g} minutes of a {window:g}-minute window.")]
    return [], []


def preflight(
    kind, *, summary, scope, plan_metadata=None, phase=None, attempt_phase="implementation",
    execution_target=None, allowed_paths=None, allowed_actions=None, allowed_commands=None,
    prohibited_actions=None, max_failed_heads=None, tool_fallbacks=None, estimate=None,
    unread_authority_message_id=None,
) -> PreflightResult:
    """Every mechanical problem of a request, without touching the database.

    ``unread_authority_message_id`` is looked up by the caller, which owns the session.
    """
    result = PreflightResult()
    paths_named = None
    raw = dict(
        summary=summary, plan_metadata=plan_metadata, phase=phase, execution_target=execution_target,
        allowed_paths=allowed_paths, allowed_actions=allowed_actions, allowed_commands=allowed_commands,
        prohibited_actions=prohibited_actions, max_failed_heads=max_failed_heads, tool_fallbacks=tool_fallbacks)
    # A value of the wrong container type is reported by the schema check; the rules see an empty one.
    if not isinstance(plan_metadata, dict):
        plan_metadata = {}
    allowed_paths, allowed_actions, allowed_commands, prohibited_actions = (
        value if isinstance(value, list) else [] for value in
        (allowed_paths, allowed_actions, allowed_commands, prohibited_actions))
    if not isinstance(tool_fallbacks, dict):
        tool_fallbacks = {}
    if kind == "initial_plan":
        result.problems += initial_plan_problems(summary, plan_metadata)
        listed = _service().github_approval_service.initial_plan_paths(plan_metadata)
        paths_named = listed
    elif kind == "continuation":
        result.problems += continuation_header(phase, execution_target, summary)
        problems, normalized = continuation_body(
            phase=phase, execution_target=execution_target, allowed_paths=allowed_paths or [],
            allowed_actions=allowed_actions or [], allowed_commands=allowed_commands or [],
            prohibited_actions=prohibited_actions or [], max_failed_heads=max_failed_heads,
            tool_fallbacks=tool_fallbacks if tool_fallbacks is not None else {}, scope=scope)
        result.problems += problems
        paths_named = normalized.paths
    elif kind == "scope_amendment":
        problems, normalized = amendment_body(
            phase=phase or "implementation", attempt_phase=attempt_phase, execution_target=execution_target,
            summary=summary, allowed_paths=allowed_paths or [], allowed_actions=allowed_actions or [],
            allowed_commands=allowed_commands or [], prohibited_actions=prohibited_actions or [],
            max_failed_heads=max_failed_heads,
            tool_fallbacks=tool_fallbacks if tool_fallbacks is not None else {}, scope=scope)
        result.problems += problems
        paths_named = normalized.paths
    else:
        raise ValueError("unknown request kind")
    if isinstance(summary, str) and companion_paths.TRAILER_HEADER in summary and kind != "initial_plan":
        result.problems.append(problem("continuation_summary_reserved", 400))
    if paths_named:
        if kind != "initial_plan":
            result.problems += companion_limit_problems(paths_named, scope)
        result.warnings += companion_warning(paths_named)
    if kind != "initial_plan":
        problems, warnings = estimate_problems(estimate, scope)
        result.problems += problems
        result.warnings += warnings
    # A field of the wrong container type is one schema problem; the rules saw an empty one and must not add
    # a misleading "required" next to it.
    wrong_type = {name for name in ("allowed_paths", "allowed_actions", "allowed_commands", "prohibited_actions")
                  if raw[name] is not None and not isinstance(raw[name], list)}
    result.problems = [item for item in result.problems if item.field not in wrong_type]
    seen = {item.field for item in result.problems}
    result.problems += [item for item in schema_problems(kind, **raw) if item.field not in seen]
    if unread_authority_message_id is not None:
        result.problems.append(problem(
            "unread_authority_mail",
            message=f"Unread stop, hold or decision message {unread_authority_message_id}: read your inbox first."))
    return result


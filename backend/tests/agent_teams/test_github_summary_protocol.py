"""Current summaries replace their own block and preserve issue requirements."""
from datetime import datetime, timezone
import hashlib

import pytest
from pydantic import ValidationError

from mcp_shim.github_summary_protocol import CurrentSummary, START, END, render_summary


def summary(**changes):
    return CurrentSummary(**{**dict(goal="Record delivery results.", status="Working",
        completed="The first checkpoint is published.", remaining="Complete the correction batch and review.",
        next_action="The owner publishes the complete candidate.", human_action="No action is required.",
        reported_at=datetime(2026, 10, 7, 14, tzinfo=timezone.utc)), **changes})


def test_inserts_one_summary_and_preserves_original_requirements():
    body = "## Requirements\n\nOriginal human scope.\n<!-- deck:operator-actions:start -->\nRecords\n<!-- deck:operator-actions:end -->\n"
    result = render_summary(body, summary())
    assert result["body_markdown"] == summary().markdown() + "\n\n" + body
    assert result["source_body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert result["body_markdown"].count(START) == 1


def test_replaces_only_owned_block_and_repeated_render_is_unchanged():
    prefix, suffix = "Human requirements\n\n", "\n\nForeign history stays.\n"
    body = prefix + summary().markdown() + suffix
    newer = summary(status="Awaiting review", completed="The complete candidate is published.")
    result = render_summary(body, newer, expected_body_sha256=hashlib.sha256(body.encode()).hexdigest())
    assert result["body_markdown"] == prefix + newer.markdown() + suffix
    assert not render_summary(result["body_markdown"], newer)["changed"]


@pytest.mark.parametrize("body", [START, END, END + START, START + END + START + END,
    "```\n" + START + END, "<!--\n" + START + END, "<details>\n" + START + END,
    START + "<!-- deck:operator-actions:start -->" + END])
def test_ambiguous_or_hidden_markers_are_refused(body):
    with pytest.raises(ValueError):
        render_summary(body, summary())


def test_changed_body_and_oversized_body_are_refused():
    with pytest.raises(ValueError, match="body_changed"):
        render_summary("Human changed requirements", summary(), expected_body_sha256="0" * 64)
    with pytest.raises(ValueError, match="body_invalid"):
        render_summary("x" * 65537, summary())
    with pytest.raises(ValueError, match="rendered_body_too_large"):
        render_summary("x" * 65535, summary())


@pytest.mark.parametrize("changes", [{"goal": "password=private"}, {"completed": "ghp_private"},
    {"next_action": "hidden\u202evalue"}, {"human_action": "[Link](private)"},
    {"effort_low_minutes": True}, {"effort_low_minutes": 60}, {"confidence": "high"},
    {"effort_low_minutes": 120, "effort_high_minutes": 60, "confidence": "low", "effort_scope": "repairs"}])
def test_private_text_and_unqualified_effort_are_refused(changes):
    with pytest.raises(ValidationError):
        summary(**changes)


def test_effort_and_waiting_are_separate_and_unknown_is_explicit():
    assert summary().estimate() == "Unknown."
    measured = summary(effort_low_minutes=180, effort_high_minutes=300, confidence="low",
                       effort_scope="corrections", waiting="Review and CI waits are not estimated.")
    text = measured.markdown()
    assert "3–5 hours of active work" in text
    assert "**Waiting:** Review and CI waits are not estimated." in text
    assert "**Human action:** No action is required." in text


# --- #987: every refusal says why, and never repeats the body or the summary text ------------------------------------

def _refusal(body, report=None, **kwargs):
    from mcp_shim.github_summary_protocol import explain
    try:
        render_summary(body, report or summary(), **kwargs)
    except (ValueError, TypeError) as error:
        return explain(error)
    pytest.fail("the render was not refused")


REASONS = [
    ("body_changed", dict(body="Human requirements.\n", expected_body_sha256="0" * 64)),
    ("body_invalid", dict(body="x" * 65537)),
    ("rendered_body_too_large", dict(body="x" * 65535)),
    ("summary_markers_ambiguous", dict(body=START + END + START + END)),
    ("summary_markers_invalid", dict(body="<details>\n" + START + "old" + END)),
    ("summary_markers_invalid", dict(body="x" * 4100 + "\n" + START + "old" + END)),
    ("summary_contains_foreign_section", dict(body=START + "<!-- deck:operator-actions:start -->" + END)),
]


@pytest.mark.parametrize("reason,case", REASONS, ids=[f"{r}-{i}" for i, (r, _c) in enumerate(REASONS)])
def test_each_body_refusal_names_its_reason_and_keeps_the_stable_code(reason, case):
    refusal = _refusal(case.pop("body"), **case)
    assert refusal["code"] == "invalid_github_summary" and refusal["reason"] == reason
    assert refusal["suggestion"] and "fields" not in refusal


def test_the_same_body_renders_without_a_hash_and_is_refused_with_a_wrong_one():
    """The reproduced failure: a body that renders on a test call is refused only because the hash did not match."""
    body = "## Requirements\n\n" + "Existing issue text. " * 200 + "\n<!-- deck:operator-actions:start -->\nR\n<!-- deck:operator-actions:end -->\n"
    assert render_summary(body, summary())["changed"]
    right = hashlib.sha256(body.encode()).hexdigest()
    assert render_summary(body, summary(), expected_body_sha256=right)["changed"]
    wrong = hashlib.sha256((body + " ").encode()).hexdigest()
    refusal = _refusal(body, expected_body_sha256=wrong)
    assert refusal["reason"] == "body_changed"
    assert "placeholder" in refusal["suggestion"] or "hash" in refusal["suggestion"]


@pytest.mark.parametrize("changes,fields", [
    ({"goal": "password=zzqq"}, ["goal"]),
    ({"completed": "ghp_zzqq"}, ["completed"]),
    ({"human_action": "[Link](zzqq)"}, ["human_action"]),
    ({"remaining": "a" * 241}, ["remaining"]),
    ({"goal": "token a1b2c3d4e5f6a7b8c9"}, ["goal"]),
    ({"effort_low_minutes": 60}, ["effort_range"]),
    ({"goal": "password=zzqq", "next_action": "ghp_zzqq"}, ["goal", "next_action"]),
])
def test_a_summary_text_refusal_names_the_fields_and_never_their_values(changes, fields):
    from mcp_shim.github_summary_protocol import explain
    with pytest.raises(ValidationError) as caught:
        summary(**changes)
    refusal = explain(caught.value)
    assert refusal["code"] == "invalid_github_summary" and refusal["reason"] == "invalid_summary_field"
    assert refusal["fields"] == fields
    text = str(refusal)
    for secret in ("password", "ghp_", "zzqq", "a1b2c3d4", "Link", "aaaa"):
        assert secret not in text


def test_an_unexpected_error_gets_the_generic_reason_and_no_text():
    from mcp_shim.github_summary_protocol import explain
    refusal = explain(TypeError("a secret value in a message"))
    assert refusal["code"] == "invalid_github_summary" and refusal["reason"] == "invalid_summary"
    assert "secret" not in str(refusal)
    refusal = explain(ValueError("a body fragment that is not one of the known reasons"))
    assert refusal["reason"] == "invalid_summary" and "fragment" not in str(refusal)


def test_the_tool_returns_the_reason_and_keeps_its_old_shape(monkeypatch):
    import mcp_shim.agent_mail_server as shim
    monkeypatch.setattr(shim, "_ensure_registered", lambda: {"ok": True})
    fields = dict(goal="Goal.", status="Working", completed="Done.", remaining="Left.", next_action="Next.",
                  human_action="None.")
    body = "Human requirements.\n"
    refused = shim.synth_city_render_github_summary(body, fields, expected_body_sha256="0" * 64)
    assert refused["ok"] is False and refused["error"]["code"] == "invalid_github_summary"
    assert refused["error"]["reason"] == "body_changed" and refused["suggestion"]
    bad_text = shim.synth_city_render_github_summary(body, {**fields, "goal": "password=zzqq"})
    assert bad_text["error"]["reason"] == "invalid_summary_field" and bad_text["error"]["fields"] == ["goal"]
    assert "zzqq" not in str(bad_text)
    ok = shim.synth_city_render_github_summary(
        body, fields, expected_body_sha256=hashlib.sha256(body.encode()).hexdigest())
    assert ok["ok"] is True and ok["changed"] is True and "error" not in ok


# --- #987 review 5927206088: an unexpected key in the summary dict is never echoed back --------------------------------

def _extra_key_error(**extra):
    base = dict(goal="Goal.", status="Working", completed="Done.", remaining="Left.", next_action="Next.",
                human_action="None.", reported_at=datetime(2026, 10, 7, 14, tzinfo=timezone.utc))
    with pytest.raises(ValidationError) as caught:
        CurrentSummary.model_validate({**base, **extra})
    return caught.value


@pytest.mark.parametrize("key", ["zzqq_marker_key", "ghp_zzqq", "password=zzqq", "a1b2c3d4e5f6a7b8c9d0"])
def test_an_unexpected_summary_key_is_reported_by_a_fixed_label_never_by_its_name(key):
    from mcp_shim.github_summary_protocol import explain
    refusal = explain(_extra_key_error(**{key: "value"}))
    assert refusal["reason"] == "invalid_summary_field" and refusal["fields"] == ["unknown_field"]
    assert "zzqq" not in str(refusal) and "a1b2c3d4" not in str(refusal)


def test_declared_and_unexpected_fields_together_name_the_declared_and_label_the_rest():
    from mcp_shim.github_summary_protocol import explain
    error = _extra_key_error(goal="password=zzqq", zzqq_one="x", zzqq_two="y")
    refusal = explain(error)
    assert refusal["fields"] == ["goal", "unknown_field"]
    assert "zzqq" not in str(refusal)


def test_the_tool_never_echoes_an_unexpected_key(monkeypatch):
    import mcp_shim.agent_mail_server as shim
    monkeypatch.setattr(shim, "_ensure_registered", lambda: {"ok": True})
    fields = dict(goal="Goal.", status="Working", completed="Done.", remaining="Left.", next_action="Next.",
                  human_action="None.", zzqq_secret_key="x")
    refused = shim.synth_city_render_github_summary("Human requirements.\n", fields)
    assert refused["ok"] is False and refused["error"]["reason"] == "invalid_summary_field"
    assert refused["error"]["fields"] == ["unknown_field"]
    assert "zzqq" not in str(refused)

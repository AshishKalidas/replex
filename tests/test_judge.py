"""final_output mode ``judge``: model-graded equivalence, recorded and replayed offline."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from tracekit import make_trace

import replex
from replex import compare
from replex.cassette import load
from replex.drift import judge as judge_module
from replex.drift.judge import JudgeError, Verdict, judge_request, judgement_name, parse_verdict
from replex.drift.policy import FinalOutputCheck, Policy, parse_policy
from replex.errors import PolicyError
from replex.testing import MockError, MockProvider, MockReply

ROOT = Path(__file__).resolve().parents[1]
KEY_ENV = "REPLEX_TEST_JUDGE_KEY"
FAKE_KEY = "judge-test-key-not-real"

RECORDED = "I've requested your refund. It reaches your card in 5-7 business days."
LATEST = "Done! Your refund is on its way."


def verdict(equivalent: bool, reason: str = "Both confirm the refund.") -> str:
    return json.dumps({"equivalent": equivalent, "reason": reason, "confidence": 0.9})


def run(text: str) -> Any:
    return make_trace(
        "refund_request", "model>get_order", ("get_order", {"id": 1}), f"model:{text}"
    )


def judge_policy(directory: Path, base_url: str, **extra: Any) -> Policy:
    check = {
        "mode": "judge",
        "model": "mock-judge",
        "criteria": "Same decision for the customer; tone may differ.",
        "base_url": base_url,
        "api_key_env": KEY_ENV,
        "judgements_dir": str(directory),
        "severity": "fail",
        **extra,
    }
    return parse_policy({"version": 1, "defaults": {"final_output": check}})


def output_findings(pol: Policy, latest: str = LATEST, recorded: str = RECORDED) -> list[Any]:
    report = compare(run(recorded), run(latest), pol)
    return [f for f in report.findings if f.check == "final_output"]


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(KEY_ENV, FAKE_KEY)
    return FAKE_KEY


@pytest.fixture
def replies() -> list[MockReply]:
    """What the mock judge answers, in order."""
    return []


@pytest.fixture
def mock(replies: list[MockReply]) -> Iterator[MockProvider]:
    with MockProvider(lambda request: replies.pop(0)) as provider:
        yield provider


def script(replies: list[MockReply], *items: MockReply | str) -> None:
    replies.extend(MockReply(text=i) if isinstance(i, str) else i for i in items)


# ------------------------------------------------------------------ verdicts


def test_different_meaning_fails_with_the_judges_reason(
    tmp_path: Path, key: str, mock: MockProvider, replies: list[MockReply]
) -> None:
    script(replies, verdict(False, "The latest answer drops the 5-7 day timeline."))
    pol = judge_policy(tmp_path, mock.openai_base_url)
    [f] = output_findings(pol)
    assert f.severity == "fail"
    assert f.reason == "The latest answer drops the 5-7 day timeline."
    assert f.message == (
        "final output means something different (mock-judge): "
        "The latest answer drops the 5-7 day timeline."
    )
    assert (f.before, f.after) == (RECORDED, LATEST)
    assert not compare(run(RECORDED), run(LATEST), pol).passed


def test_equivalent_answers_pass_and_report_the_reason(
    tmp_path: Path, key: str, mock: MockProvider, replies: list[MockReply]
) -> None:
    script(replies, verdict(True, "Both confirm the refund was requested."))
    pol = judge_policy(tmp_path, mock.openai_base_url)
    report = compare(run(RECORDED), run(LATEST), pol)
    [f] = [f for f in report.findings if f.check == "final_output"]
    assert report.passed
    assert f.severity == "info"
    assert f.reason == "Both confirm the refund was requested."
    assert "judged equivalent (mock-judge)" in f.message


def test_identical_answers_need_no_judge(tmp_path: Path, mock: MockProvider) -> None:
    pol = judge_policy(tmp_path, mock.openai_base_url)
    assert output_findings(pol, latest=RECORDED) == []
    assert mock.requests == []


def test_severity_ignore_switches_the_judge_off(tmp_path: Path, mock: MockProvider) -> None:
    pol = judge_policy(tmp_path, mock.openai_base_url, severity="ignore")
    assert output_findings(pol) == []
    assert mock.requests == []


def test_request_is_deterministic_and_carries_the_criteria(
    tmp_path: Path, key: str, mock: MockProvider, replies: list[MockReply]
) -> None:
    script(replies, verdict(True))
    output_findings(judge_policy(tmp_path, mock.openai_base_url))
    [req] = mock.requests
    assert req.model == "mock-judge"
    assert req.body["temperature"] == 0
    assert req.system == judge_module.SYSTEM_PROMPT
    user = req.last_user_text
    assert "Criteria: Same decision for the customer; tone may differ." in user
    assert f"<recorded_answer>\n{RECORDED}\n</recorded_answer>" in user
    assert f"<latest_answer>\n{LATEST}\n</latest_answer>" in user


# ------------------------------------------------------------------ record and replay


def test_second_evaluation_replays_offline_without_a_key(
    tmp_path: Path,
    key: str,
    mock: MockProvider,
    replies: list[MockReply],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script(replies, verdict(False, "Different decision."))
    pol = judge_policy(tmp_path / "judgements", mock.openai_base_url)
    first = output_findings(pol)
    [stored] = list((tmp_path / "judgements").glob("judgement-*.replex.json"))

    mock.stop()
    monkeypatch.delenv(KEY_ENV)
    second = output_findings(pol)
    assert [f.model_dump() for f in second] == [f.model_dump() for f in first]
    assert len(mock.requests) == 1

    cassette = load(stored)
    assert cassette.meta.provider == "mock"
    assert cassette.meta.model == "mock-judge"
    assert cassette.meta.tags == ["judgement"]
    assert cassette.usage.input_tokens > 0
    [exchange] = cassette.exchanges
    assert exchange.request.headers["authorization"] == "[REDACTED]"
    assert FAKE_KEY not in stored.read_text(encoding="utf-8")


def test_a_changed_input_is_a_new_judgement(
    tmp_path: Path, key: str, mock: MockProvider, replies: list[MockReply]
) -> None:
    script(replies, verdict(True), verdict(False, "Now denies the refund."))
    pol = judge_policy(tmp_path, mock.openai_base_url)
    output_findings(pol)
    [f] = output_findings(pol, latest="Sorry, no refund.")
    assert f.reason == "Now denies the refund."
    assert len(list(tmp_path.glob("judgement-*.replex.json"))) == 2


def test_judgement_names_hash_every_input() -> None:
    check = FinalOutputCheck(mode="judge")
    base = judgement_name(judge_request("a", "b", check))
    assert base == judgement_name(judge_request("a", "b", check))
    assert base.startswith("judgement-")
    others = {
        judgement_name(judge_request("a", "c", check)),
        judgement_name(judge_request("c", "b", check)),
        judgement_name(judge_request("a", "b", check.model_copy(update={"model": "other"}))),
        judgement_name(judge_request("a", "b", check.model_copy(update={"criteria": "other"}))),
    }
    assert base not in others and len(others) == 4


def test_judgements_dir_is_relative_to_the_policy(
    tmp_path: Path, key: str, mock: MockProvider, replies: list[MockReply]
) -> None:
    script(replies, verdict(True))
    pol_file = tmp_path / "replex.policy.yaml"
    pol_file.write_text(
        "version: 1\n"
        "defaults:\n"
        "  final_output:\n"
        f"    {{mode: judge, model: mock-judge, base_url: '{mock.openai_base_url}', "
        f"api_key_env: {KEY_ENV}}}\n",
        encoding="utf-8",
    )
    compare(run(RECORDED), run(LATEST), replex.load_policy(pol_file))
    assert len(list((tmp_path / "judgements").glob("judgement-*.replex.json"))) == 1


def test_project_redaction_rules_apply_to_judgements(
    tmp_path: Path, key: str, mock: MockProvider, replies: list[MockReply]
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.replex]\nredact_patterns = [{ name = "ticket", pattern = "TCK-\\\\d{6}" }]\n',
        encoding="utf-8",
    )
    script(replies, verdict(False, "Ticket TCK-482910 is no longer mentioned."))
    pol = judge_policy(tmp_path / "judgements", mock.openai_base_url)
    output_findings(pol, recorded="Refund requested for ticket TCK-482910.")
    [stored] = list((tmp_path / "judgements").glob("*.replex.json"))
    text = stored.read_text(encoding="utf-8")
    assert "TCK-482910" not in text
    assert "[REDACTED_TICKET_1]" in text


# ------------------------------------------------------------------ errors are findings


def test_no_key_and_no_stored_judgement_is_an_actionable_error(
    tmp_path: Path, mock: MockProvider
) -> None:
    [f] = output_findings(judge_policy(tmp_path, mock.openai_base_url))
    assert f.severity == "fail"
    assert f.reason is None
    assert f.message.startswith("final output judge error: no stored judgement judgement-")
    assert f"{KEY_ENV} is not set" in f.message
    assert "commit the file" in f.message
    assert mock.requests == []


def test_errors_take_the_configured_severity(tmp_path: Path, mock: MockProvider) -> None:
    [f] = output_findings(judge_policy(tmp_path, mock.openai_base_url, severity="warn"))
    assert f.severity == "warn"


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        ("They look the same to me.", "did not reply with JSON"),
        ('{"equivalent": true}', "reason: Field required"),
        ('{"equivalent": "yes", "reason": "same"}', "equivalent: Input should be a valid boolean"),
        ('{"equivalent": true, "reason": ""}', "reason: String should have at least 1"),
        ('{"equivalent": true, "reason": "same", "confidence": 2}', "confidence: Input should be"),
        ('{"equivalent": true, "reason": "same", "score": 1}', "score: Extra inputs"),
        ('["equivalent"]', "reply: Input should be a valid dictionary"),
        ("", "returned no text"),
    ],
)
def test_malformed_verdicts_are_errors_and_are_not_stored(
    tmp_path: Path, key: str, mock: MockProvider, replies: list[MockReply], reply: str, problem: str
) -> None:
    script(replies, reply)
    [f] = output_findings(judge_policy(tmp_path, mock.openai_base_url))
    assert f.severity == "fail"
    assert f.message.startswith("final output judge error: ")
    assert problem in f.message
    assert list(tmp_path.glob("*.replex.json")) == []


def test_http_errors_are_errors_and_are_not_stored(
    tmp_path: Path, key: str, mock: MockProvider, replies: list[MockReply]
) -> None:
    script(replies, MockReply(error=MockError(429, "rate_limit", "slow down")))
    [f] = output_findings(judge_policy(tmp_path, mock.openai_base_url))
    assert "the judge returned HTTP 429" in f.message
    assert "slow down" in f.message
    assert list(tmp_path.glob("*.replex.json")) == []


def test_unreachable_judge_is_an_error(tmp_path: Path, key: str) -> None:
    [f] = output_findings(judge_policy(tmp_path, "http://127.0.0.1:9/v1"))
    assert "the judge request to http://127.0.0.1:9/v1/chat/completions failed" in f.message


def test_judge_inside_an_active_session_is_an_error(
    tmp_path: Path, key: str, mock: MockProvider
) -> None:
    with replex.session("outer", mode="live"):
        [f] = output_findings(judge_policy(tmp_path, mock.openai_base_url))
    assert "the judge could not run" in f.message
    assert "sessions do not nest" in f.message
    assert mock.requests == []


def test_a_response_that_is_not_a_chat_completion_is_an_error() -> None:
    with pytest.raises(JudgeError, match="not a chat completion: <html>"):
        judge_module._content(httpx.Response(200, text="<html>"))
    with pytest.raises(JudgeError, match="not a chat completion"):
        judge_module._content(httpx.Response(200, json={"choices": []}))


def test_parse_verdict_accepts_a_fenced_reply_and_integer_confidence() -> None:
    v = parse_verdict('```json\n{"equivalent": false, "reason": "x", "confidence": 1}\n```')
    assert v == Verdict(equivalent=False, reason="x", confidence=1.0)
    assert parse_verdict('{"equivalent": true, "reason": "y"}').confidence is None
    with pytest.raises(JudgeError, match="no text"):
        parse_verdict(None)


# ------------------------------------------------------------------ policy


def test_policy_accepts_judge_settings_and_rejects_bad_ones() -> None:
    pol = parse_policy(
        {"version": 1, "defaults": {"final_output": {"mode": "judge", "severity": "fail"}}}
    )
    c = pol.defaults.final_output
    assert (c.model, c.base_url, c.api_key_env, c.judgements_dir) == (
        "anthropic/claude-haiku-5.5",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "judgements",
    )
    with pytest.raises(PolicyError, match="criteria"):
        parse_policy({"defaults": {"final_output": {"mode": "judge", "criteria": ""}}})
    with pytest.raises(PolicyError, match="judge_model"):
        parse_policy({"defaults": {"final_output": {"mode": "judge", "judge_model": "x"}}})


# ------------------------------------------------------------------ real judgements


EXAMPLE = ROOT / "examples" / "support_agent"
REFUND_V1 = (
    '{"decision": "refund_requested", "refund_id": "RF-1042", "message": "I\'ve requested '
    "refund RF-1042 for your Trail running shoes. It reaches your original payment method in "
    '5-7 business days."}'
)
REFUND_V2 = (
    '{"decision": "refund_requested", "refund_id": "RF-1042", "message": "Done! Refund '
    'RF-1042 is on its way for your Trail running shoes."}'
)
LENIENT = "Same decision and same refund for the customer; tone and level of detail may differ."
STRICT = (
    "Same decision and the same facts for the customer, including when the money arrives; "
    "tone may differ."
)


@pytest.mark.parametrize(("criteria", "equivalent"), [(LENIENT, True), (STRICT, False)])
def test_recorded_claude_judgements_replay_offline(
    monkeypatch: pytest.MonkeyPatch, criteria: str, equivalent: bool
) -> None:
    """The judgements quoted in docs/DRIFT.md, replayed with no key and no network."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    pol = parse_policy(
        {
            "version": 1,
            "defaults": {"final_output": {"mode": "judge", "criteria": criteria}},
        },
        base_dir=EXAMPLE,
    )
    [f] = output_findings(pol, latest=REFUND_V2, recorded=REFUND_V1)
    assert f.severity == ("info" if equivalent else "warn")
    assert f.reason

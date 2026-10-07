from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from replex.errors import ConfigError
from replex.redaction import (
    HEADER_PLACEHOLDER,
    RedactionRules,
    Redactor,
    check_directory,
    is_sensitive_name,
    scan_value,
)


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "x-api-key",
        "api-key",
        "Cookie",
        "set-cookie",
        "X-Goog-Api-Key",
        "x-auth-token",
        "client_secret",
        "anthropic-ratelimit-tokens-limit",
    ],
)
def test_sensitive_names(name: str) -> None:
    assert is_sensitive_name(name)


@pytest.mark.parametrize("name", ["content-type", "user-agent", "anthropic-version", "accept"])
def test_non_sensitive_names(name: str) -> None:
    assert not is_sensitive_name(name)


def test_headers_are_replaced_outright() -> None:
    r = Redactor()
    out = r.headers({"Authorization": "Bearer abc", "X-Api-Key": "k", "Content-Type": "json"})
    assert out == {
        "authorization": HEADER_PLACEHOLDER,
        "x-api-key": HEADER_PLACEHOLDER,
        "content-type": "json",
    }


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Authorization: Bearer abcdefghijklmnop123", "BEARER"),
        ("key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv", "API_KEY"),
        ("key sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz", "API_KEY"),
        ("aws AKIAABCDEFGHIJKLMNOP", "API_KEY"),
        ("gh ghp_abcdefghijklmnopqrstuvwxyz0123456789", "API_KEY"),
        ("mail jane.doe+x@example.co.uk now", "EMAIL"),
        ("call +44 20 7946 0958 today", "PHONE"),
        ("call (555) 123-4567 today", "PHONE"),
        ("call 555-123-4567 today", "PHONE"),
    ],
)
def test_default_patterns(text: str, kind: str) -> None:
    out = Redactor().text(text)
    assert f"[REDACTED_{kind}_1]" in out


@pytest.mark.parametrize(
    "text",
    [
        "order 1042",
        "2026-10-07T09:00:00Z",
        "risk-assessment-for-the-quarter-2026",
        "v1.2.3",
        "sha256:deadbeef",
        "msg_mock_0001",
        "toolu_01XFDUDYJgAACzvnptvVoYEL",
    ],
)
def test_no_false_positives(text: str) -> None:
    assert Redactor().text(text) == text


def test_tokens_are_stable_within_a_redactor() -> None:
    r = Redactor()
    a = r.value({"x": "a@example.com", "y": ["b@example.com", "a@example.com"]})
    assert a == {"x": "[REDACTED_EMAIL_1]", "y": ["[REDACTED_EMAIL_2]", "[REDACTED_EMAIL_1]"]}
    assert r.text("again a@example.com") == "again [REDACTED_EMAIL_1]"


def test_redaction_is_idempotent() -> None:
    r = Redactor()
    once = r.text("mail a@example.com, key sk-ant-abcdefghijklmnop, Bearer zzzzzzzzzzzz")
    assert r.text(once) == once


def test_url_query_redaction() -> None:
    out = Redactor().url("https://h.example/v1/x?key=SECRET&q=jane@example.com&n=1")
    assert "SECRET" not in out
    assert "jane@example.com" not in out
    assert "n=1" in out


def test_url_without_query() -> None:
    assert Redactor().url("https://api.anthropic.com/v1/messages") == (
        "https://api.anthropic.com/v1/messages"
    )


def test_custom_patterns_and_paths() -> None:
    rules = RedactionRules.from_config(
        redact_patterns=[r"ORD-\d+", {"name": "customer id", "pattern": r"CUST\d{3}"}],
        redact_paths=["customer.name", "items.*.sku", "$.meta.0"],
    )
    r = Redactor(rules)
    out = r.value(
        {
            "note": "ORD-77 for CUST123",
            "customer": {"name": "Jane", "id": 5},
            "items": [{"sku": "A"}, {"sku": 9}],
            "meta": ["x", "y"],
        }
    )
    assert out["note"] == "[REDACTED_CUSTOM_1] for [REDACTED_CUSTOMER_ID_1]"
    assert out["customer"] == {"name": "[REDACTED_FIELD_1]", "id": 5}
    assert out["items"] == [{"sku": "[REDACTED_FIELD_2]"}, {"sku": "[REDACTED_FIELD_3]"}]
    assert out["meta"] == ["[REDACTED_FIELD_4]", "y"]
    # path redaction is idempotent too
    assert r.value(out) == out


@pytest.mark.parametrize("bad", [123, {"name": "x"}, "("])
def test_bad_custom_patterns(bad: object) -> None:
    with pytest.raises(ConfigError):
        RedactionRules.from_config(redact_patterns=[bad])  # type: ignore[list-item]


def test_custom_kind_names_are_sanitised() -> None:
    rules = RedactionRules.from_config(
        redact_patterns=[{"name": "9lives", "pattern": "cat"}, {"name": "", "pattern": "dog"}]
    )
    r = Redactor(rules)
    assert r.text("cat dog") == "[REDACTED_C_9LIVES_1] [REDACTED_CUSTOM_1]"


def test_observe_and_fork_keep_numbering_consistent() -> None:
    recorder = Redactor()
    recorded = recorder.text("x@example.com then y@example.com")
    # A replay-side redactor that has seen the recorded tokens numbers new originals after them.
    replayer = Redactor()
    replayer.observe({"a": [recorded]})
    assert replayer.text("z@example.com") == "[REDACTED_EMAIL_3]"
    fork = replayer.fork()
    assert fork.text("w@example.com") == "[REDACTED_EMAIL_4]"
    assert replayer.text("w@example.com") == "[REDACTED_EMAIL_4]"
    replayer.adopt(fork)
    assert replayer.text("w@example.com") == "[REDACTED_EMAIL_4]"


def test_scan_value_finds_leaks_and_masks_them() -> None:
    data = {
        "events": [
            {
                "request": {
                    "headers": {"x-api-key": "sk-ant-realrealrealreal", "accept": "x"},
                    "body": {"text": "hi bob@example.com [REDACTED_EMAIL_1]"},
                }
            }
        ]
    }
    findings = scan_value(data, file="f")
    kinds = sorted(f.kind for f in findings)
    assert kinds == ["API_KEY", "EMAIL", "HEADER"]
    assert all("realrealreal" not in f.excerpt for f in findings)
    assert any(f.location == "events[0].request.headers.x-api-key" for f in findings)


def test_scan_clean_value() -> None:
    clean = {"headers": {"x-api-key": HEADER_PLACEHOLDER}, "body": "[REDACTED_EMAIL_1]"}
    assert scan_value(clean) == []


def test_check_directory(tmp_path: Path) -> None:
    (tmp_path / "ok.replex.json").write_text(json.dumps({"a": "fine"}))
    sub = tmp_path / "live"
    sub.mkdir()
    (sub / "bad.replex.json").write_text(json.dumps({"a": "me@example.com"}))
    (tmp_path / "broken.replex.json").write_text("{nope")
    (tmp_path / ".hidden.replex.json").write_text(json.dumps({"a": "me@example.com"}))
    findings = check_directory(tmp_path)
    assert sorted(f.kind for f in findings) == ["EMAIL", "UNREADABLE"]
    assert check_directory(tmp_path / "ok.replex.json") == []


# ----------------------------------------------------------------- properties

secret_strategy = st.one_of(
    st.from_regex(r"sk-ant-[A-Za-z0-9]{20,30}", fullmatch=True),
    st.from_regex(r"sk-proj-[A-Za-z0-9]{24,32}", fullmatch=True),
    st.from_regex(r"[a-z]{1,8}\.[a-z]{1,8}@[a-z]{2,8}\.(com|org|io)", fullmatch=True),
    st.from_regex(r"\+1 [2-9]\d{2} \d{3} \d{4}", fullmatch=True),
)
filler = st.text(alphabet=st.sampled_from(list("abc XYZ,.!?\n")), max_size=30)


@settings(max_examples=150, deadline=None)
@given(prefix=filler, secret=secret_strategy, suffix=filler)
def test_secrets_never_survive(prefix: str, secret: str, suffix: str) -> None:
    out = Redactor().value({"m": [prefix + " " + secret + " " + suffix]})
    assert secret not in json.dumps(out)
    assert scan_value(out) == []


@settings(max_examples=100, deadline=None)
@given(secrets=st.lists(secret_strategy, min_size=1, max_size=5))
def test_same_secret_same_token(secrets: list[str]) -> None:
    r = Redactor()
    first = [r.text(s) for s in secrets]
    second = [r.text(s) for s in secrets]
    assert first == second
    tokens = set(first)
    assert all(re.fullmatch(r"\[REDACTED_[A-Z_]+_\d+\]", t) for t in tokens)
    assert len(tokens) == len(set(secrets))


@settings(max_examples=100, deadline=None)
@given(text=st.text(max_size=60))
def test_idempotent_property(text: str) -> None:
    r = Redactor()
    once = r.text(text)
    assert r.text(once) == once

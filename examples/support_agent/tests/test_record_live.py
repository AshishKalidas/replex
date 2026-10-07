"""The live-recording script refuses to run without the owner's keys."""

from __future__ import annotations

import pytest
import record_live


@pytest.mark.parametrize(
    ("provider", "env", "message"),
    [
        ("claude", {}, "ANTHROPIC_API_KEY is not set"),
        ("mimo", {"MIMO_API_KEY": "x"}, "MIMO_BASE_URL, MIMO_MODEL"),
    ],
)
def test_refuses_without_keys(
    monkeypatch: pytest.MonkeyPatch, provider: str, env: dict[str, str], message: str
) -> None:
    for var in ("ANTHROPIC_API_KEY", "MIMO_API_KEY", "MIMO_BASE_URL", "MIMO_MODEL"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match=message):
        record_live.config_for(provider, stream=False)


def test_mimo_model_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMO_API_KEY", "k")
    monkeypatch.setenv("MIMO_BASE_URL", "https://mimo.example/v1")
    monkeypatch.setenv("MIMO_MODEL", "owner-chosen-model-id")
    cfg = record_live.config_for("mimo", stream=True)
    assert (cfg.provider, cfg.model, cfg.stream) == ("openai", "owner-chosen-model-id", True)

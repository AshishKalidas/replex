"""The live-recording script: keys and model ids from the environment, and its two modes.

The end-to-end tests point the OpenRouter target at the local mock provider, so they run
offline and the cassettes they write are labelled ``mock``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import record_live
from support_agent.scenarios import SCENARIOS
from support_agent.simulated_model import respond

import replex
from replex.testing import MockProvider

KEYS = (
    "ANTHROPIC_API_KEY",
    "MIMO_API_KEY",
    "MIMO_BASE_URL",
    "MIMO_MODEL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL",
    "OPENROUTER_MODEL",
    "CLAUDE_MODEL",
    "PROMPT_VERSION",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in KEYS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize(
    ("target", "env", "message"),
    [
        ("claude", {}, "ANTHROPIC_API_KEY is not set"),
        ("openrouter", {}, "OPENROUTER_API_KEY is not set"),
        ("mimo", {"MIMO_API_KEY": "x"}, "MIMO_BASE_URL, MIMO_MODEL"),
    ],
)
def test_refuses_without_keys(
    monkeypatch: pytest.MonkeyPatch, target: str, env: dict[str, str], message: str
) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match=message):
        record_live.config_for(target, stream=False)


def test_mimo_model_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIMO_API_KEY", "k")
    monkeypatch.setenv("MIMO_BASE_URL", "https://mimo.example/v1")
    monkeypatch.setenv("MIMO_MODEL", "owner-chosen-model-id")
    cfg = record_live.config_for("mimo", stream=True)
    assert (cfg.provider, cfg.model, cfg.stream) == ("openai", "owner-chosen-model-id", True)
    assert record_live.config_for("mimo", stream=False, model="flag-id").model == "flag-id"


def test_openrouter_model_from_flag_environment_or_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    cfg = record_live.config_for("openrouter", stream=False)
    assert (cfg.provider, cfg.base_url) == ("openai", "https://openrouter.ai/api/v1")
    assert cfg.model == "anthropic/claude-sonnet-5.5"
    monkeypatch.setenv("OPENROUTER_MODEL", "anthropic/claude-haiku-5.5")
    assert record_live.config_for("openrouter", stream=False).model == "anthropic/claude-haiku-5.5"
    cfg = record_live.config_for("openrouter", stream=False, model="x/y", prompt_version="v2")
    assert (cfg.model, cfg.prompt_version) == ("x/y", "v2")


@pytest.mark.parametrize(
    ("target", "model", "folder"),
    [
        ("claude", "claude-sonnet-5-5", "claude"),
        ("mimo", "any-id", "mimo"),
        ("openrouter", "anthropic/claude-sonnet-5.5", "openrouter-claude-sonnet-5.5"),
        ("openrouter", "vendor/model:free", "openrouter-model-free"),
    ],
)
def test_folder_name(target: str, model: str, folder: str) -> None:
    assert record_live.folder_name(target, model) == folder


@pytest.fixture
def mock_openrouter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """The OpenRouter target, served by the mock provider, writing under a temporary folder."""
    monkeypatch.setattr(record_live, "LIVE", tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    # RepleX refuses to record when CI is set; this test records against the mock on purpose.
    monkeypatch.delenv("CI", raising=False)
    with MockProvider(respond) as mock:
        monkeypatch.setenv("OPENROUTER_BASE_URL", f"{mock.url}/v1")
        yield tmp_path


def test_records_then_checks_for_drift(
    mock_openrouter: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert record_live.main(["openrouter", "--model", "vendor/model-a"]) == 0
    recorded = mock_openrouter / "openrouter-model-a"
    assert sorted(p.stem for p in recorded.glob("*.replex.json")) == sorted(
        f"{s.name}.replex" for s in SCENARIOS
    )
    meta = replex.load_cassette(recorded / "refund_request.replex.json").meta
    assert "model=vendor/model-a" in meta.tags
    assert "prompt=v1" in meta.tags
    assert meta.provider == "mock"

    same = ["openrouter", "--model", "vendor/model-a", "--against", "openrouter-model-a"]
    assert record_live.main(same) == 0
    assert "BEHAVIOUR CHANGED" not in capsys.readouterr().out

    changed = [*same, "--prompt", "v2"]
    assert record_live.main(changed) == 1
    out = capsys.readouterr().out
    assert "BEHAVIOUR CHANGED" in out
    assert "fail  tool_sequence" in out
    candidates = recorded / "candidates" / "openrouter-model-a-v2"
    assert len(list(candidates.glob("*.replex.json"))) == len(SCENARIOS)


def test_drift_check_needs_recordings(mock_openrouter: Path) -> None:
    with pytest.raises(SystemExit, match="no recordings to compare with"):
        record_live.main(["openrouter", "--against", "nothing-here"])

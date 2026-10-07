from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the host's CI / RepleX environment from leaking into tests."""
    for var in ("CI", "REPLEX_MODE", "REPLEX_ALLOW_RECORD_IN_CI", "REPLEX_CASSETTE_DIR",
                "REPLEX_MATCH", "REPLEX_TOOLS", "REPLEX_POLICY", "REPLEX_CANDIDATE_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REPLEX_GIT_SHA", "test000")

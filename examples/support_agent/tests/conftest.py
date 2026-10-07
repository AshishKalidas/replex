from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from support_agent import AgentConfig
from support_agent import tools as agent_tools
from support_agent.simulated_model import respond

from replex import Session
from replex.testing import MockProvider

# Replay never touches the network: any URL works, nothing listens on port 9.
OFFLINE_URL = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def _reset_side_effects() -> Iterator[None]:
    agent_tools.reset()
    yield
    agent_tools.reset()


@pytest.fixture
def agent_config(replex_session: Session) -> Iterator[Callable[..., AgentConfig]]:
    """Build an AgentConfig for the active RepleX mode.

    In replay mode the agent points at an address where nothing listens, which proves the run
    is served from the cassette. In any other mode a local mock provider plays the model.
    """
    mock = None
    if replex_session.mode != "replay":
        mock = MockProvider(respond).start()

    def make(provider: str = "anthropic", stream: bool = False) -> AgentConfig:
        base = mock.url if mock else OFFLINE_URL
        if provider == "openai":
            return AgentConfig.from_env(
                provider="openai",
                base_url=f"{base}/v1",
                api_key="test-key-not-real",
                model="mock-mimo",
                stream=stream,
            )
        return AgentConfig.from_env(
            provider="anthropic",
            base_url=base,
            api_key="test-key-not-real",
            model="mock-sonnet",
            stream=stream,
        )

    yield make
    if mock:
        mock.stop()

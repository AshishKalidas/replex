from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from support_agent import AgentConfig
from support_agent import tools as agent_tools
from support_agent.agent import Provider
from support_agent.simulated_model import respond
from support_agent.suite import OFFLINE_URL, simulated_config

from replex import Session
from replex.testing import MockProvider


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

    def make(provider: Provider = "anthropic", stream: bool = False) -> AgentConfig:
        return simulated_config(mock.url if mock else OFFLINE_URL, provider, stream)

    yield make
    if mock:
        mock.stop()

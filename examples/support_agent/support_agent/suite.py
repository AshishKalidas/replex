"""The entry point `replex run` calls for each scenario in replex.suite.yaml.

In replay mode the agent points at an address where nothing listens, which proves the run is
served from the cassette. In any other mode RepleX's mock provider plays the model, so the
suite records and checks for drift offline, with no API key.
"""

from __future__ import annotations

import replex
from replex.testing import MockProvider
from support_agent import tools as agent_tools
from support_agent.agent import AgentConfig, Provider, run_agent
from support_agent.simulated_model import respond

# Replay never touches the network: any URL works, nothing listens on port 9.
OFFLINE_URL = "http://127.0.0.1:9"


def simulated_config(
    base_url: str, provider: Provider = "anthropic", stream: bool = False
) -> AgentConfig:
    """Settings for the simulated model at ``base_url`` (``PROMPT_VERSION`` still applies)."""
    if provider == "openai":
        return AgentConfig.from_env(
            provider="openai",
            base_url=f"{base_url}/v1",
            api_key="test-key-not-real",
            model="mock-mimo",
            stream=stream,
        )
    return AgentConfig.from_env(
        provider="anthropic",
        base_url=base_url,
        api_key="test-key-not-real",
        model="mock-sonnet",
        stream=stream,
    )


def answer(question: str, provider: Provider = "anthropic", stream: bool = False) -> str:
    """Answer one customer message inside the active RepleX session."""
    agent_tools.reset()
    if replex.current_session().mode == "replay":
        return run_agent(question, simulated_config(OFFLINE_URL, provider, stream)).answer
    with MockProvider(respond) as mock:
        return run_agent(question, simulated_config(mock.url, provider, stream)).answer

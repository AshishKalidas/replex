"""Provider normalisers: recorded exchanges -> provider-neutral trace."""

from replex.providers.anthropic import AnthropicNormaliser
from replex.providers.base import (
    Normaliser,
    RawNormaliser,
    build_trace,
    detect_provider,
    normaliser_for,
    register_normaliser,
    unregister_normaliser,
)
from replex.providers.openai_compat import OpenAICompatNormaliser

register_normaliser(AnthropicNormaliser(), first=False)
register_normaliser(OpenAICompatNormaliser(), first=False)

__all__ = [
    "AnthropicNormaliser",
    "Normaliser",
    "OpenAICompatNormaliser",
    "RawNormaliser",
    "build_trace",
    "detect_provider",
    "normaliser_for",
    "register_normaliser",
    "unregister_normaliser",
]

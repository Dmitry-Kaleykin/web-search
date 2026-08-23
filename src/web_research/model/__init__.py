"""Local model clients and prompts."""

from .base import ResearchModel
from .errors import ModelError
from .fallback import FallbackModelClient
from .mcp_sampling import MCPSamplingModelClient
from .openai_compatible import OpenAICompatibleModelClient
from .unavailable import UnavailableModelClient

__all__ = [
    "FallbackModelClient",
    "MCPSamplingModelClient",
    "ModelError",
    "OpenAICompatibleModelClient",
    "ResearchModel",
    "UnavailableModelClient",
]

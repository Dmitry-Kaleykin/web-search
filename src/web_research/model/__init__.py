"""Local model clients and prompts."""

from .base import ResearchModel
from .errors import ModelError
from .mcp_sampling import MCPSamplingModelClient
from .openai_compatible import OpenAICompatibleModelClient
from .unavailable import UnavailableModelClient

__all__ = [
    "MCPSamplingModelClient",
    "ModelError",
    "OpenAICompatibleModelClient",
    "ResearchModel",
    "UnavailableModelClient",
]

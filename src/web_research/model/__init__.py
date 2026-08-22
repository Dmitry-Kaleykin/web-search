"""Local model clients and prompts."""

from .base import ResearchModel
from .openai_compatible import OpenAICompatibleModelClient

__all__ = ["OpenAICompatibleModelClient", "ResearchModel"]

"""Local model clients and prompts."""

from .base import ResearchModel
from .omlx import OMLXModelClient

__all__ = ["OMLXModelClient", "ResearchModel"]

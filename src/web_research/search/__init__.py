"""Search-provider adapters."""

from .base import SearchProvider
from .searxng import SearXNGSearchProvider

__all__ = ["SearXNGSearchProvider", "SearchProvider"]

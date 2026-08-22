"""Content-reader adapters."""

from .base import Reader
from .crawl4ai import Crawl4AIReader
from .http import HTTPReader
from .router import LayeredReader

__all__ = ["Crawl4AIReader", "HTTPReader", "LayeredReader", "Reader"]

from __future__ import annotations

from typing import Literal

from ..models import Document
from .base import Reader
from .quality import rendering_signals

RenderMode = Literal["auto", "never", "always"]


class LayeredReader:
    def __init__(self, primary: Reader, browser: Reader | None = None) -> None:
        self.primary = primary
        self.browser = browser

    async def close(self) -> None:
        await self.primary.close()
        if self.browser is not None:
            await self.browser.close()

    async def read(self, url: str, *, render: RenderMode = "auto") -> Document:
        if render not in {"auto", "never", "always"}:
            raise ValueError(f"Unsupported render mode: {render}")
        primary_error: Exception | None = None
        document: Document | None = None
        try:
            document = await self.primary.read(url)
        except Exception as exc:
            primary_error = exc

        if render == "never":
            if document is not None:
                if _browser_recommended(document):
                    document.warnings.append("browser_render_skipped:render_never")
                return document
            assert primary_error is not None
            raise primary_error

        if render == "auto" and document is not None and not _browser_recommended(document):
            return document
        if self.browser is None:
            if document is not None:
                document.warnings.append("browser_fallback_disabled")
                return document
            assert primary_error is not None
            raise primary_error

        try:
            return await self.browser.read(url)
        except Exception as browser_error:
            if document is not None:
                document.warnings.append(
                    f"browser_fallback_failed:{type(browser_error).__name__}:{browser_error}"
                )
                return document
            assert primary_error is not None
            raise RuntimeError(
                f"Both HTTP and browser readers failed. HTTP: {primary_error}; "
                f"browser: {browser_error}"
            ) from browser_error


def _browser_recommended(document: Document) -> bool:
    if rendering_signals(document.content):
        return True
    # Honor the old warning until cached documents written by earlier versions expire.
    return any(
        warning.startswith(("browser_recommended:", "possibly_incomplete:"))
        for warning in document.warnings
    )

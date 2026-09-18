from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from contextlib import suppress
from typing import Literal

from ..models import Document
from ..text import lexical_similarity
from .base import Reader
from .quality import rendering_signals

RenderMode = Literal["auto", "never", "always"]


class LayeredReader:
    def __init__(
        self,
        primary: Reader,
        browser: Reader | None = None,
        *,
        read_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self.primary = primary
        self.browser = browser
        self.store = getattr(primary, "store", None)
        self._pending: dict[str, asyncio.Task] = {}
        self._waiters: dict[str, int] = {}
        self._read_limit = read_semaphore or asyncio.Semaphore(8)

    async def close(self) -> None:
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.primary.close()
        if self.browser is not None:
            await self.browser.close()

    async def read(
        self,
        url: str,
        *,
        render: RenderMode = "auto",
        query: str | None = None,
        max_age_seconds: int | None = None,
        visual: bool = False,
        page: int = 1,
        actions: list[dict] | None = None,
    ) -> Document:
        variant = json.dumps([url, render, query, visual, page, actions], sort_keys=True)
        cache_key = "read:v3:" + hashlib.sha256(variant.encode()).hexdigest()
        ttl = (
            getattr(self.primary, "cache_ttl_seconds", 21600)
            if max_age_seconds is None
            else max_age_seconds
        )
        if self.store and ttl > 0 and not actions:
            cached = await asyncio.to_thread(self.store.get_document, cache_key, ttl)
            if cached:
                cached.warnings.append("cache_hit")
                return cached
        key = f"{cache_key}:{ttl}"
        if key not in self._pending:

            async def retrieve():
                async with self._read_limit:
                    result = await asyncio.wait_for(
                        self._read(
                            url,
                            render=render,
                            query=query,
                            max_age_seconds=max_age_seconds,
                            visual=visual,
                            page=page,
                            actions=actions,
                        ),
                        timeout=90,
                    )
                    if self.store and not actions:
                        await asyncio.to_thread(self.store.put_document, cache_key, result)
                    return result

            self._pending[key] = asyncio.create_task(retrieve())
            self._waiters[key] = 0
        task = self._pending[key]
        self._waiters[key] += 1
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            self._waiters[key] -= 1
            if self._waiters[key] == 0:
                self._pending.pop(key, None)
                self._waiters.pop(key, None)
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    async def _read(
        self,
        url: str,
        *,
        render: RenderMode,
        query: str | None,
        max_age_seconds: int | None,
        visual: bool,
        page: int,
        actions: list[dict] | None,
    ) -> Document:
        if render not in {"auto", "never", "always"}:
            raise ValueError(f"Unsupported render mode: {render}")
        primary_error: Exception | None = None
        document: Document | None = None
        try:
            from .http import HTTPReader

            if isinstance(self.primary, HTTPReader):
                document = await self.primary.read(
                    url, max_age_seconds=max_age_seconds, visual=visual, page=page
                )
            else:
                document = await self.primary.read(url)
        except Exception as exc:
            primary_error = exc

        if document is not None and document.content_type in {
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/webp",
        }:
            return document
        if render == "never":
            if document is not None:
                if _browser_recommended(document):
                    document.warnings.append("browser_render_skipped:render_never")
                return document
            assert primary_error is not None
            raise primary_error

        if (
            render == "auto"
            and not visual
            and not actions
            and document is not None
            and not _browser_recommended(document)
            and not _query_render_recommended(document, query)
        ):
            return document
        if self.browser is None:
            if document is not None:
                document.warnings.append("browser_fallback_disabled")
                return document
            assert primary_error is not None
            raise primary_error

        try:
            if visual or actions:
                return await self.browser.read(url, query=query, visual=visual, actions=actions)
            if query:
                return await self.browser.read(url, query=query)  # type: ignore[call-arg]
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

    async def read_for_research(
        self, url: str, *, query: str, max_age_seconds: int | None = None
    ) -> Document:
        """Read with result-context available for relevance-based browser escalation."""

        return await self.read(url, query=query, max_age_seconds=max_age_seconds)


def _browser_recommended(document: Document) -> bool:
    if rendering_signals(document.content):
        return True
    # Honor the old warning until cached documents written by earlier versions expire.
    return any(
        warning.startswith(("browser_recommended:", "possibly_incomplete:"))
        for warning in document.warnings
    )


def _query_render_recommended(document: Document, query: str | None) -> bool:
    if not query or document.method.startswith("crawl4ai"):
        return False
    title_relevance = lexical_similarity(document.title, query)
    content_relevance = lexical_similarity(document.content[:20_000], query)
    if title_relevance >= 0.05 and content_relevance < 0.01:
        document.warnings.append("browser_recommended:relevant_terms_missing")
        return True
    return False

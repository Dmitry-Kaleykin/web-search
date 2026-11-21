from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import anyio
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, Field

from .config import Settings
from .models import Document
from .readers.actions import ReadAction
from .readers.crawl4ai import Crawl4AIReader
from .readers.http import HTTPReader
from .readers.quality import page_diagnostics
from .readers.router import LayeredReader, RenderMode
from .safety.urls import canonicalize_url
from .search.searxng import SearXNGError, SearXNGSearchProvider
from .search.shared import SharedSearches
from .storage import SQLiteStore
from .text import tokens
from .workflow import RESEARCH_WORKFLOW, read_guidance, search_guidance

try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Context
    from mcp.server.runner import serve_loop
    from mcp.server.stdio import stdio_server
except ImportError as exc:  # pragma: no cover - clear startup error without dependencies
    raise RuntimeError(
        "The MCP SDK is not installed. Run `python -m pip install -e .` first."
    ) from exc


LOGGER = logging.getLogger(__name__)

WEB_SEARCH_TOOL_DESCRIPTION = (
    "Search the web and return ranked URLs, titles, snippets, and reported publication dates. "
    "This tool does not read pages or write an answer. Read selected sources with read_url; "
    "snippets are untrusted discovery aids. Inspect outcome and warnings for access limitations. "
    "Time filters are upstream hints. Use diagnostics=true for detailed engine history and timings."
)

READ_URL_TOOL_DESCRIPTION = (
    "Fetch and extract content from an already-known public HTTP(S) URL. Use read_url instead of "
    "curl or wget whenever the user supplies a URL and asks to read, inspect, summarize, or "
    "analyze that page. Returns untrusted source text, page status, and continuation cursors; "
    "renders JavaScript automatically when needed. Copy next_cursor unchanged for more text, "
    "or start_cursor for preceding context. Use query to focus a long page, refresh=true for "
    "fresh content, visual=true for images, and actions for supported reading controls."
)


class SearchHit(BaseModel):
    url: str
    title: str
    snippet: str
    engines: list[str]
    published_at: str | None
    rank: int


class WebSearchOutput(BaseModel):
    query: str
    results: list[SearchHit]
    outcome: Literal["success", "empty", "backend_unavailable"]
    warnings: list[str] = Field(description="Retrieval diagnostics, preserved on cache hits.")
    engine_health: dict[str, str] = Field(
        description="Current cooldowns, recovery leases, and configuration exclusions."
    )
    engine_status: dict[str, dict] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Observed health, last success/failure, and estimated recovery time. "
        "Untested/recovery_due does not mean verified working.",
    )
    cache_hit: bool
    elapsed_ms: int | None = Field(default=None, exclude_if=lambda value: value is None)
    responded_at: str | None = Field(default=None, exclude_if=lambda value: value is None)
    retrieved_at: str | None = Field(
        description="Original retrieval time, unchanged on cache hits; null when unavailable."
    )
    available_engines: list[str] = Field(
        description=(
            "Configured engines eligible for a request or guarded recovery attempt; "
            "not verified availability. "
            "Empty if queue timeout prevented inspecting health."
        )
    )
    requested_engines: list[str] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    engine_coverage: dict[str, Any] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "Which requested engines contributed to this response. 'silent' excludes engines "
            "already explained by engine_health; an engine in 'never_verified' has no recorded "
            "success, so its silence is a broken or walled integration, not an empty query. "
            "Meaningless when 'attribution' is absent."
        ),
    )
    guidance: list[str] = Field(
        default_factory=list,
        exclude_if=lambda value: not value,
        description="Actionable retrieval limitations, when present.",
    )


class ReadUrlOutput(BaseModel):
    url: str
    final_url: str
    title: str
    content: str
    content_type: str
    status_code: int | None
    page_status: Literal["ok", "incomplete", "suspected_error"]
    extraction_method: str
    retrieved_at: str
    published_at: str | None
    published_at_source: str | None
    links: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)
    warnings: list[str]
    content_characters: int
    content_truncated: bool
    content_start: int
    content_end: int
    has_more_content: bool
    next_cursor: str | int | None
    start_cursor: str | None = Field(
        default=None,
        description="Read the same snapshot from its beginning after a query-focused window.",
    )
    link_count: int
    links_included: bool = Field(default=False, exclude_if=lambda value: not value)
    links_truncated: bool
    snapshot_id: str | None = None
    visual_pages: list[int] = Field(default_factory=list, exclude_if=lambda value: not value)
    available_actions: list[dict[str, str]] = Field(
        default_factory=list, exclude_if=lambda value: not value
    )
    guidance: list[str] = Field(default_factory=list, exclude_if=lambda value: not value)


_RUNTIMES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_RUNTIME_LIMITS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SEARCH_LIMITS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_SHARED_SEARCHES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _shared_reader_runtime(settings: Settings):
    loop = asyncio.get_running_loop()
    runtimes = _RUNTIMES.setdefault(loop, {})
    if settings not in runtimes:
        runtimes[settings] = _create_reader_runtime(settings)
    return runtimes[settings]


async def close_reader_runtimes() -> None:
    shared = _SHARED_SEARCHES.pop(asyncio.get_running_loop(), None)
    if shared is not None:
        await shared.close()
    runtimes = _RUNTIMES.pop(asyncio.get_running_loop(), {})
    await asyncio.gather(*(runtime.close() for runtime in runtimes.values()))
    _RUNTIME_LIMITS.pop(asyncio.get_running_loop(), None)
    _SEARCH_LIMITS.pop(asyncio.get_running_loop(), None)


@asynccontextmanager
async def _lifespan(_server):
    try:
        yield {}
    finally:
        await close_reader_runtimes()


mcp = MCPServer(
    "Local Agentic Web Search",
    lifespan=_lifespan,
    instructions=RESEARCH_WORKFLOW,
)


@mcp.tool(
    name="read_url",
    description=READ_URL_TOOL_DESCRIPTION,
    structured_output=True,
)
async def read_url(
    url: Annotated[
        str,
        Field(
            description=(
                "A public HTTP(S) URL from the user, search results, or a page link. "
                "Pass it without rewriting it into a search query."
            )
        ),
    ],
    ctx: Context,
    query: Annotated[
        str | None,
        Field(
            description=(
                "Optional question or topic to focus extraction on. On the first call, read_url "
                "returns the most relevant content window instead of blindly returning page "
                "navigation from the beginning."
            )
        ),
    ] = None,
    render: Annotated[
        RenderMode,
        Field(
            description=(
                "Rendering policy: auto uses Chromium only when HTTP extraction appears "
                "incomplete; never does not launch Chromium; always attempts Chromium and "
                "preserves the HTTP result if browser rendering fails. Non-HTML documents "
                "use their native reader unless browser visuals/actions are requested."
            )
        ),
    ] = "auto",
    cursor: Annotated[
        str | int,
        Field(
            description=(
                "Use 0 for the first read, or copy the opaque next_cursor "
                "unchanged. Continuation reads an immutable snapshot for up to "
                "one hour; query/render options apply only to the first read "
            ),
        ),
    ] = 0,
    max_chars: Annotated[
        int,
        Field(
            ge=1_000,
            le=60_000,
            description=(
                "Maximum characters in this reply, not the page extraction limit. "
                "Use next_cursor for more text, or start_cursor for context before a focused "
                "window. Use 4000-8000 for batched calls to avoid client-side payload omission."
            ),
        ),
    ] = 4_000,
    include_links: Annotated[
        bool,
        Field(
            description=(
                "Whether to include extracted links. Leave false for reading and batched calls; "
                "set true only when the links are needed."
            )
        ),
    ] = False,
    refresh: Annotated[
        bool,
        Field(
            description=(
                "Bypass URL caches for a fresh first read. Cannot be combined with continuation "
            ),
        ),
    ] = False,
    visual: Annotated[
        bool,
        Field(
            description=(
                "Return an image for the main model to inspect charts, scanned "
                "PDFs or visual pages "
            ),
        ),
    ] = False,
    page: Annotated[
        int, Field(ge=1, le=100, description="PDF page to render when visual=true.")
    ] = 1,
    actions: Annotated[
        list[ReadAction] | None,
        Field(
            max_length=5,
            description=(
                "Optional read-only browser actions: expand details, select a "
                "tab, load more text, or scroll. Requires render=auto or always. "
            ),
        ),
    ] = None,
) -> Annotated[CallToolResult, ReadUrlOutput]:
    """Read a known URL, or continue the exact extraction from a previous call."""
    if not url.strip():
        raise ValueError("url must not be empty")
    settings = Settings.from_env()
    runtime = _shared_reader_runtime(settings)
    await ctx.report_progress(progress=0.1, total=1.0, message="Reading the supplied URL")
    if isinstance(cursor, str):
        if refresh or actions:
            raise ValueError("Refresh and actions require a new read with cursor=0")
        try:
            snapshot_id, offset = cursor.split(":")
            if not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
                raise ValueError
            offset = int(offset)
            if offset < 0:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Invalid read cursor; copy next_cursor unchanged") from exc
        document = await asyncio.to_thread(runtime.store.get_snapshot, snapshot_id)
        if canonicalize_url(document.url) != canonicalize_url(url.strip()):
            raise ValueError("Cursor belongs to a different URL")
    else:
        if cursor != 0:
            raise ValueError(
                "Numeric continuation is unsafe; use next_cursor from the first result"
            )
        if actions and render == "never":
            raise ValueError("Browser actions cannot be used with render=never")
        document = await runtime.reader.read(
            url.strip(),
            render=render,
            query=query,
            max_age_seconds=0 if refresh else None,
            visual=visual,
            page=page,
            actions=[action.model_dump() for action in actions] if actions else None,
        )
        snapshot_id = await asyncio.to_thread(runtime.store.put_snapshot, document)
        offset = 0
    output = _read_url_output(
        document,
        settings,
        cursor=offset,
        max_chars=max_chars,
        include_links=include_links,
        query=query if cursor == 0 else None,
    )
    output.snapshot_id = snapshot_id
    output.start_cursor = f"{snapshot_id}:0" if output.content_start > 0 else None
    output.available_actions = document.available_actions
    output.next_cursor = f"{snapshot_id}:{output.content_end}" if output.has_more_content else None
    output.visual_pages = [item["page"] for item in document.images]
    await ctx.report_progress(progress=1.0, total=1.0, message=f"URL read with {document.method}")
    if document.images:
        # Image blocks reach the calling model as actual multimodal evidence, not encoded prose.
        return CallToolResult(
            content=[
                TextContent(text=output.model_dump_json()),
                *[
                    ImageContent(data=item["data"], mime_type=item["mime_type"])
                    for item in document.images
                ],
            ],
            structured_content=output.model_dump(),
        )
    return output


@mcp.tool(
    name="web_search",
    description=WEB_SEARCH_TOOL_DESCRIPTION,
    structured_output=True,
)
async def web_search(
    query: Annotated[
        str, Field(min_length=1, max_length=2_000, description="A focused search query.")
    ],
    ctx: Context,
    limit: Annotated[int, Field(ge=1, le=20, description="Maximum results to return.")] = 10,
    page: Annotated[int, Field(ge=1, le=10, description="Search results page, starting at 1.")] = 1,
    language: Annotated[
        str | None,
        Field(max_length=32, description="Optional search language, for example en or ru."),
    ] = None,
    time_range: Annotated[
        Literal["day", "month", "year"] | None,
        Field(description="Optional upstream time filter; dates must still be assessed by you."),
    ] = None,
    refresh: Annotated[bool, Field(description="Bypass cached search results.")] = False,
    engine: Annotated[
        str | None,
        Field(
            min_length=1,
            max_length=100,
            description=(
                'Optional single configured engine name, e.g. "mojeek"; '
                "use a name from available_engines. "
                "Select an alternative when it offers a useful path after weak results. "
                "Cooldowns still apply. Omit to use the configured pool."
            ),
        ),
    ] = None,
    diagnostics: Annotated[
        bool, Field(description="Include detailed engine history, requested engines and timings.")
    ] = False,
) -> WebSearchOutput:
    """One bounded discovery request, without page reads or internal model calls."""
    settings = Settings.from_env()
    shared = _SHARED_SEARCHES.setdefault(asyncio.get_running_loop(), SharedSearches())
    key = (settings, query.strip(), limit, page, language, time_range, refresh, engine)
    output = await shared.run(
        key,
        lambda: _web_search(
            query,
            ctx,
            settings,
            limit=limit,
            page=page,
            language=language,
            time_range=time_range,
            refresh=refresh,
            engine=engine,
        ),
    )
    if not diagnostics:
        output.engine_status = None
        output.elapsed_ms = None
        output.responded_at = None
        output.requested_engines = None
        output.engine_coverage = None
    return output


async def _web_search(query, ctx, settings, *, limit, page, language, time_range, refresh, engine):
    if not query.strip():
        raise ValueError("query must not be empty")
    configured_engines = list(
        dict.fromkeys(
            name.strip() for name in settings.search_healthy_engines.split(",") if name.strip()
        )
    )
    if engine is not None and engine not in configured_engines:
        raise ValueError("engine must be one configured search engine name")
    requested_engines = configured_engines if engine is None else [engine]
    started = time.monotonic()
    results = []
    warnings: list[str] = []
    engine_health: dict[str, str] = {}
    engine_status = {}
    engine_health_known = False
    cache_hit = False
    retrieved_at = None
    outcome = "empty"
    # Cache hits bypass dispatch; fresh searches still reload state after waiting for the gate.
    gate = _SEARCH_LIMITS.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(1))
    try:
        async with asyncio.timeout(settings.search_timeout_seconds):
            runtime = _shared_reader_runtime(settings)
            search = SearXNGSearchProvider(
                settings.searxng_url,
                store=runtime.store,
                cache_ttl_seconds=settings.search_cache_ttl_seconds,
                timeout_seconds=min(20.0, settings.search_timeout_seconds),
                user_agent=settings.user_agent,
                healthy_engines=settings.search_healthy_engines,
                use_catalog=True,
            )
            inspected = False
            arguments = dict(
                limit=limit,
                page=page,
                language=language,
                time_range=time_range,
                engines=",".join(requested_engines),
            )
            try:
                cached = None if refresh else search.cached_search(query.strip(), **arguments)
                if cached is not None:
                    results = cached
                    inspected = True
                else:
                    async with gate:
                        inspected = True
                        # Health reads SQLite on demand; reload inventory after waiting too.
                        search.catalog = runtime.store.get_backend_metadata(search.base_url)
                        await ctx.report_progress(
                            progress=0.1, total=1.0, message="Searching the web"
                        )
                        if not search.healthy_engines:
                            raise SearXNGError("No search engines configured")
                        results = await search.search(query.strip(), refresh=refresh, **arguments)
                outcome = "success" if results else "empty"
            finally:
                if inspected:
                    warnings.extend(search.last_warnings)
                    engine_health = search.engine_health()
                    engine_status = search.engine_report()
                    engine_health_known = True
                    cache_hit = search.last_cache_hit
                    retrieved_at = search.last_retrieved_at
                await search.close()
    except TimeoutError:
        outcome = "backend_unavailable"
        warnings.append("search_timeout: search request or queue exceeded the time budget")
    except SearXNGError as exc:
        outcome = "backend_unavailable"
        warnings.append(f"search_failed: {exc}")
    cooling = {name.casefold() for name in engine_health}
    available_engines = (
        []
        if not engine_health_known or "searxng" in cooling
        else [name for name in configured_engines if name.casefold() not in cooling]
    )
    # Totals cannot tell an empty query from a dead integration. Attribution says which engines
    # spoke; everything else is either waiting (engine_health) or silent (this block).
    coverage: dict[str, Any] | None = None
    if engine_health_known:
        answered = {name.casefold() for hit in results for name in hit.engines}
        waiting = {name.casefold() for name in engine_health}
        never_verified = [
            name
            for name in requested_engines
            if engine_status.get(name, {}).get("status") == "untested"
            and not engine_status.get(name, {}).get("successes")
        ]
        coverage = {
            "requested": requested_engines,
            "answered": sorted(answered),
            "silent": [
                name for name in requested_engines if name.casefold() not in answered | waiting
            ],
            # SearXNG may omit per-engine attribution; then silence says nothing about engines.
            "attribution": "present" if answered or not results else "absent",
            # Current evidence, unlike retrieval provenance, so it is reported here rather than
            # in the warnings a cache hit has to reproduce byte for byte.
            "never_verified": never_verified,
        }
    return WebSearchOutput(
        query=query.strip(),
        results=[
            SearchHit(
                url=item.url,
                title=item.title[:1_000],
                snippet=item.snippet[:4_000],
                engines=item.engines,
                published_at=item.published_at,
                rank=item.rank,
            )
            for item in results
        ],
        outcome=outcome,
        warnings=warnings,
        engine_health=engine_health,
        engine_status=engine_status,
        cache_hit=cache_hit,
        elapsed_ms=int((time.monotonic() - started) * 1_000),
        responded_at=datetime.now(UTC).isoformat(),
        retrieved_at=retrieved_at,
        requested_engines=requested_engines,
        available_engines=available_engines,
        engine_coverage=coverage,
        guidance=search_guidance(outcome),
    )


@dataclass(slots=True)
class _ReaderRuntime:
    store: SQLiteStore
    reader: LayeredReader

    async def close(self) -> None:
        try:
            await self.reader.close()
        finally:
            self.store.close()


def _create_reader_runtime(settings: Settings) -> _ReaderRuntime:
    # Even a settings change must not create a fresh concurrency allowance.
    limits = _RUNTIME_LIMITS.setdefault(
        asyncio.get_running_loop(),
        (
            asyncio.Semaphore(8),
            asyncio.Semaphore(2),
            asyncio.Semaphore(max(1, settings.browser_max_concurrent_renders)),
        ),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(settings.data_dir.resolve()))
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str((settings.data_dir / "ms-playwright").resolve())
    )
    store = SQLiteStore(
        settings.data_dir / "research.sqlite3",
        search_ttl_seconds=settings.search_cache_ttl_seconds,
        document_ttl_seconds=settings.document_cache_ttl_seconds,
        search_max_rows=settings.cache_search_max_rows,
        document_max_rows=settings.cache_document_max_rows,
        document_max_payload_bytes=settings.cache_document_max_payload_bytes,
    )
    http_reader = HTTPReader(
        store=store,
        cache_ttl_seconds=settings.document_cache_ttl_seconds,
        user_agent=settings.user_agent,
        accept_language=settings.browser_accept_language,
        max_response_bytes=settings.max_response_bytes,
        document_semaphore=limits[1],
        max_content_chars=settings.document_max_chars,
        allow_private_urls=settings.allow_private_urls,
        allow_proxy_fake_ips=settings.allow_proxy_fake_ips,
    )
    browser_reader = (
        Crawl4AIReader(
            store=store,
            browser_user_agent=settings.browser_user_agent,
            accept_language=settings.browser_accept_language,
            stealth=settings.browser_stealth,
            channel=settings.browser_channel,
            proxy=settings.browser_proxy,
            max_content_chars=settings.document_max_chars,
            max_concurrent_renders=settings.browser_max_concurrent_renders,
            render_semaphore=limits[2],
            allow_private_urls=settings.allow_private_urls,
            allow_proxy_fake_ips=settings.allow_proxy_fake_ips,
        )
        if settings.enable_crawl4ai
        else None
    )
    return _ReaderRuntime(
        store=store, reader=LayeredReader(http_reader, browser_reader, read_semaphore=limits[0])
    )


def _read_url_output(
    document: Document,
    settings: Settings,
    *,
    cursor: int = 0,
    max_chars: int = 4_000,
    include_links: bool = False,
    query: str | None = None,
) -> ReadUrlOutput:
    if cursor > len(document.content):
        raise ValueError(
            f"cursor {cursor} exceeds extracted content length {len(document.content)}"
        )
    content_limit = min(max_chars, settings.read_url_max_chars)
    link_limit = settings.read_url_max_links
    full_content = document.content
    all_links = document.links
    content_start = cursor
    focused = False
    if query and cursor == 0:
        content_start = _focused_content_start(full_content, query, content_limit)
        focused = content_start > 0
    content_end = min(len(full_content), content_start + content_limit)
    has_more_content = content_end < len(full_content)
    content_truncated = content_start > 0 or has_more_content
    returned_links = all_links[:link_limit] if include_links else []
    links_truncated = len(returned_links) < len(all_links)
    warnings = list(document.warnings)
    for reason in page_diagnostics(
        document.title,
        document.content,
        status_code=document.status_code,
    ):
        warning = f"suspected_error_page:{reason}"
        if warning not in warnings:
            warnings.append(warning)
    if content_truncated:
        warnings.append("tool_output_truncated:content")
    if focused:
        warnings.append("tool_output_relevance_window")
    if links_truncated and include_links:
        warnings.append("tool_output_truncated:links")
    return ReadUrlOutput(
        url=document.url,
        final_url=document.final_url,
        title=document.title,
        content=full_content[content_start:content_end],
        content_type=document.content_type,
        status_code=document.status_code,
        page_status=_page_status(document, warnings),
        extraction_method=document.method,
        retrieved_at=document.retrieved_at,
        published_at=document.published_at,
        published_at_source=document.published_at_source,
        links=returned_links,
        warnings=warnings,
        content_characters=len(full_content),
        content_truncated=content_truncated,
        content_start=content_start,
        content_end=content_end,
        has_more_content=has_more_content,
        next_cursor=content_end if has_more_content else None,
        link_count=len(all_links),
        links_included=include_links,
        links_truncated=links_truncated,
        guidance=read_guidance(_page_status(document, warnings)),
    )


def _focused_content_start(content: str, query: str, max_chars: int = 4_000) -> int:
    """Select a bounded lexical window without deleting anything from the snapshot.

    Overlapping windows handle huge single paragraphs and terms split across adjacent blocks.
    Query coverage outranks density; a matching H1 must not hide a better passage far below it.
    This is navigation assistance, not semantic relevance or an evidence judgment.
    """
    query_tokens = tokens(query)
    if not query_tokens or len(content) <= max_chars:
        return 0
    for heading in re.finditer(r"(?m)^#\s+.+$", content):
        if query_tokens <= tokens(heading.group(0)):
            return heading.start()
    width = max(1, min(1_000, max_chars // 2))
    best_score = (0, 0.0)
    best_start = 0
    for start in range(0, len(content), max(1, width // 2)):
        # Link destinations can repeat query words dozens of times in a navigation list.
        # Rank visible labels rather than those URL tokens.
        window = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", content[start : start + width])
        window_tokens = tokens(window)
        matched = len(query_tokens & window_tokens)
        score = (matched, matched / max(1, len(window_tokens)))
        if score > best_score:
            best_score, best_start = score, start
    if best_score[0] == 0:
        return 0
    # Include the preceding local heading or paragraph boundary when it fits, keeping enough
    # of the budget after the hit to include the whole selected window.
    context_start = max(0, best_start - min(2_000, max_chars // 2))
    headings = list(re.finditer(r"(?m)^#{1,6}\s+.+$", content[context_start:best_start]))
    if headings:
        return context_start + headings[-1].start()
    paragraph_start = content.rfind("\n\n", context_start, best_start)
    return paragraph_start + 2 if paragraph_start >= 0 else best_start


def _page_status(
    document: Document,
    warnings: list[str] | None = None,
) -> Literal["ok", "incomplete", "suspected_error"]:
    effective_warnings = warnings if warnings is not None else document.warnings
    if document.status_code is not None and document.status_code >= 400:
        return "suspected_error"
    if any(warning.startswith("suspected_error_page:") for warning in effective_warnings):
        return "suspected_error"
    if any(
        warning.startswith(
            (
                "browser_recommended:",
                "browser_output_incomplete:",
                "browser_render_skipped:",
                "document_page_unreadable:",
                "document_pages_truncated:",
                "ocr_unavailable:",
                "ocr_failed:",
                "content_truncated:",
                "browser_content_filtered",
                "text_decoding_lossy",
            )
        )
        for warning in effective_warnings
    ):
        return "incomplete"
    return "ok"


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOGGER.info("Starting Local Agentic Web Search MCP server")
    anyio.run(_run_stdio_server)


async def _run_stdio_server() -> None:
    """Keep the existing stdio handshake compatible with installed MCP adapters."""
    lowlevel = mcp._lowlevel_server
    async with (
        stdio_server() as (read_stream, write_stream),
        lowlevel.lifespan(lowlevel) as lifespan_state,
    ):
        await serve_loop(
            lowlevel,
            read_stream,
            write_stream,
            lifespan_state=lifespan_state,
            init_options=lowlevel.create_initialization_options(),
        )


if __name__ == "__main__":
    main()

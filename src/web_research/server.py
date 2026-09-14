from __future__ import annotations

import asyncio
import logging
import os
import re
import weakref
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

import anyio
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import BaseModel, Field

from .agent import ResearchAgent
from .config import Settings
from .controller import ResearchController
from .freshness import publication_window
from .model.base import ResearchModel
from .model.mcp_sampling import MCPSamplingModelClient
from .model.openai_compatible import OpenAICompatibleModelClient
from .model.unavailable import UnavailableModelClient
from .models import Document
from .readers.actions import ReadAction
from .readers.crawl4ai import Crawl4AIReader
from .readers.http import HTTPReader
from .readers.quality import page_diagnostics
from .readers.router import LayeredReader, RenderMode
from .reranking import OpenAICompatibleReranker
from .safety.urls import canonicalize_url
from .search.searxng import SearXNGSearchProvider
from .storage import SQLiteStore
from .text import lexical_similarity

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
    "Research a web-dependent question and return a cited, evidence-checked synthesis. "
    "Use read_url instead when the user already supplied the URL and source discovery is not "
    "needed. "
    "Make one self-contained call for the whole request; do not invoke web_search in parallel. "
    "Pass the user's temporal wording faithfully. For relative requests such as latest, recent, "
    "current, or today, keep that wording relative; the server resolves it from its own clock. "
    "Never add a calendar year unless the user explicitly supplied that year. Inspect outcome and "
    "warnings before deciding whether to retry, answer partially, or use another approach."
)

READ_URL_TOOL_DESCRIPTION = (
    "Fetch and extract content from an already-known public HTTP(S) URL. Use read_url instead of "
    "curl or wget whenever the user supplies a URL and asks to read, inspect, summarize, or "
    "analyze that page. It uses safe bounded HTTP retrieval, Trafilatura with a basic HTML "
    "fallback, and automatic headless-Chromium escalation when rendering appears necessary. "
    "It reports HTTP and suspected error-page status and returns pageable inline content. Use "
    "refresh=true bypasses cached pages. Copy next_cursor unchanged to continue the same snapshot. "
    "Use visual=true for PDF pages, images or charts and read-only actions for tabs or load-more. "
    "Use the optional query parameter to focus a navigation-heavy or long page. "
    "Use web_search instead "
    "when sources need to be discovered or corroborated."
)


class ConcurrentResearchError(RuntimeError):
    pass


class _SingleFlight:
    """Reject overlapping runs before they contend for one local model."""

    def __init__(self) -> None:
        self.active = False

    def start(self) -> None:
        if self.active:
            raise ConcurrentResearchError(
                "Another web_search call is already running. Wait for it to finish and make one "
                "self-contained call instead of parallel searches."
            )
        self.active = True

    def finish(self) -> None:
        self.active = False


RUN_GATE = _SingleFlight()


class ToolSource(BaseModel):
    id: str
    url: str
    title: str
    domain: str
    source_family: str = ""
    source_class: Literal["primary", "expert", "independent", "news", "community", "unknown"]
    retrieved_at: str
    published_at: str | None
    published_at_source: str | None
    extraction_method: str
    warnings: list[str]


class ToolCoverageItem(BaseModel):
    requirement_id: str
    covered: bool
    source_count: int
    reason: str


class ToolCoverage(BaseModel):
    score: float
    sufficient: bool
    items: list[ToolCoverageItem]
    unresolved_gaps: list[str]
    conflicts: list[str]


class ToolStats(BaseModel):
    pipeline_profile: str
    search_queries: int
    empty_searches: int
    search_failures: int
    search_backend_failures: int
    pages_fetched: int
    distinct_domains: int
    elapsed_ms: int
    browsing_elapsed_ms: int
    cache_hits: int
    fetch_failures: int
    reranker_model: str
    reranker_requests: int
    reranker_candidates: int
    reranker_failures: int
    reranker_disabled: bool
    candidates_rejected_irrelevant: int
    relevance_batches_rejected: int
    prefetch_started: int
    prefetch_unused: int
    followed_links_discovered: int


class WebSearchOutput(BaseModel):
    research_id: str
    answer_markdown: str
    sources: list[ToolSource]
    coverage: ToolCoverage
    outcome: Literal["success", "partial", "no_evidence", "backend_unavailable"]
    retryable: bool
    stop_reason: str
    stats: ToolStats
    warnings: list[str]


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
    links: list[str]
    warnings: list[str]
    content_characters: int
    content_truncated: bool
    content_start: int
    content_end: int
    has_more_content: bool
    next_cursor: str | int | None
    link_count: int
    links_included: bool
    links_truncated: bool
    snapshot_id: str | None = None
    visual_pages: list[int] = Field(default_factory=list)
    available_actions: list[dict[str, str]] = Field(default_factory=list)


_RUNTIMES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_RUNTIME_LIMITS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _shared_reader_runtime(settings: Settings):
    loop = asyncio.get_running_loop()
    runtimes = _RUNTIMES.setdefault(loop, {})
    if settings not in runtimes:
        runtimes[settings] = _create_reader_runtime(settings)
    return runtimes[settings]


async def close_reader_runtimes() -> None:
    runtimes = _RUNTIMES.pop(asyncio.get_running_loop(), {})
    await asyncio.gather(*(runtime.close() for runtime in runtimes.values()))
    _RUNTIME_LIMITS.pop(asyncio.get_running_loop(), None)


@asynccontextmanager
async def _lifespan(_server):
    try:
        yield {}
    finally:
        await close_reader_runtimes()


mcp = MCPServer(
    "Local Agentic Web Search",
    lifespan=_lifespan,
    instructions=(
        "When the user supplies a URL and asks to read, inspect, summarize, or analyze that page, "
        "use read_url instead of curl, wget, or shell-based downloading. Use web_search when "
        "sources must be discovered, compared, or corroborated, and pass a self-contained request. "
        "For batched read_url calls, request a small max_chars value and omit links unless needed. "
        "Make one web_search call at a time; parallel calls contend for the same local model and "
        "are rejected. "
        "Preserve the user's temporal wording and never invent a calendar year for latest, "
        "recent, current, or today; web_search uses its server clock. "
        "The tool reads sources, tracks evidence gaps, and returns a cited synthesis. Inspect its "
        "outcome, coverage, and warnings before deciding whether to retry or continue by another "
        "method."
    ),
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
                "The exact public HTTP(S) URL supplied by the user. Pass it directly without "
                "rewriting it into a search query."
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
                "preserves the HTTP result if browser rendering fails."
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
                "Maximum characters to return inline. Use 4000-8000 for batched or fan-out calls "
                "to avoid client-side payload omission."
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
        str,
        Field(
            description=(
                "The user's complete research request. Preserve relative temporal wording such "
                "as latest or recent; do not add a year unless the user stated it."
            )
        ),
    ],
    ctx: Context,
    effort: Literal["quick", "auto", "thorough"] = "auto",
    freshness: Annotated[
        str | None,
        Field(
            description=(
                "Optional time constraint copied from the user, such as 'recent', 'current', or "
                "'published since 2025'. Do not resolve relative wording to a guessed year."
            )
        ),
    ] = None,
) -> WebSearchOutput:
    """Research a web-dependent question and return a cited, evidence-checked synthesis.

    The query must be self-contained and should include products/entities, comparison criteria,
    locale, constraints, and desired output when those matter. Effort changes safety ceilings and
    evidence strictness; it is not a fixed page count. Freshness may be natural language, such as
    "current as of today" or "published since 2025".
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    settings = Settings.from_env()
    runtime = _shared_reader_runtime(settings)
    store = runtime.store
    search = SearXNGSearchProvider(
        settings.searxng_url,
        store=store,
        cache_ttl_seconds=0
        if publication_window(freshness or query)
        else settings.search_cache_ttl_seconds,
        user_agent=settings.user_agent,
        max_retries=settings.search_max_retries,
        retry_base_seconds=settings.search_retry_base_seconds,
        healthy_engines=settings.search_healthy_engines,
        diversity_min_results=settings.search_diversity_min_results,
        max_retry_wait_seconds=settings.search_max_retry_wait_seconds,
    )
    reader = runtime.reader
    model = _create_model(ctx, settings)
    reranker = _create_reranker(settings)
    controller = ResearchController(
        search=search,
        reader=reader,
        agent=ResearchAgent(model),
        store=store,
        reranker=reranker,
        prefetch_pages=settings.prefetch_pages,
        reranker_min_relevance_score=settings.reranker_min_relevance_score,
        reranker_relative_relevance_ratio=settings.reranker_relative_relevance_ratio,
        lexical_min_relevance_score=settings.lexical_min_relevance_score,
    )

    async def report(value: float, message: str) -> None:
        await ctx.report_progress(progress=value, total=1.0, message=message)

    try:
        RUN_GATE.start()
        try:
            result = await controller.run(
                query.strip(),
                effort=effort,
                freshness=freshness,
                progress=report,
            )
            if result.stats.reranker_model:
                LOGGER.info(
                    "Reranker usage: model=%s requests=%d candidates=%d failures=%d disabled=%s",
                    result.stats.reranker_model,
                    result.stats.reranker_requests,
                    result.stats.reranker_candidates,
                    result.stats.reranker_failures,
                    result.stats.reranker_disabled,
                )
            LOGGER.info(
                "Relevance gate: rejected_candidates=%d rejected_batches=%d",
                result.stats.candidates_rejected_irrelevant,
                result.stats.relevance_batches_rejected,
            )
            return WebSearchOutput.model_validate(result.as_dict())
        finally:
            RUN_GATE.finish()
    finally:
        await search.close()
        if reranker is not None:
            await reranker.close()
        await model.close()


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
        max_response_bytes=settings.max_response_bytes,
        document_semaphore=limits[1],
        max_content_chars=settings.document_max_chars,
        allow_private_urls=settings.allow_private_urls,
        allow_proxy_fake_ips=settings.allow_proxy_fake_ips,
    )
    browser_reader = (
        Crawl4AIReader(
            store=store,
            user_agent=settings.user_agent,
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
        content_start = _focused_content_start(full_content, query)
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
    )


def _focused_content_start(content: str, query: str) -> int:
    # A relevant page title/H1 is usually the safest entry point: it preserves definitions and
    # status callouts that precede a later matching paragraph, while skipping global navigation.
    for heading in re.finditer(r"(?m)^#\s+.+$", content):
        if lexical_similarity(heading.group(0), query) > 0:
            return heading.start()

    spans: list[tuple[float, int]] = []
    for match in re.finditer(r"[^\n]+(?:\n(?!\s*\n)[^\n]+)*", content):
        paragraph = match.group(0).strip()
        if paragraph:
            spans.append((lexical_similarity(paragraph, query), match.start()))
    if not spans:
        return 0
    score, start = max(spans, key=lambda item: item[0])
    if score <= 0:
        return 0
    heading_start = content.rfind("\n#", max(0, start - 2_000), start)
    return heading_start + 1 if heading_start >= 0 else start


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
            )
        )
        for warning in effective_warnings
    ):
        return "incomplete"
    return "ok"


def _create_model(ctx: Context, settings: Settings) -> ResearchModel:
    if MCPSamplingModelClient.supported(ctx):
        return MCPSamplingModelClient(
            ctx,
            timeout_seconds=settings.model_timeout_seconds,
            max_tokens=settings.model_max_tokens,
            temperature=settings.model_temperature,
        )
    if settings.model_id:
        return OpenAICompatibleModelClient(
            settings.model_base_url,
            settings.model_id,
            api_key=settings.model_api_key,
            timeout_seconds=settings.model_timeout_seconds,
            max_tokens=settings.model_max_tokens,
            temperature=settings.model_temperature,
        )
    return UnavailableModelClient()


def _create_reranker(settings: Settings) -> OpenAICompatibleReranker | None:
    if not settings.reranker_model_id:
        return None
    return OpenAICompatibleReranker(
        settings.reranker_base_url,
        settings.reranker_model_id,
        api_key=settings.reranker_api_key,
        timeout_seconds=settings.reranker_timeout_seconds,
        max_candidates=settings.reranker_max_candidates,
    )


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOGGER.info("Starting Local Agentic Web Search MCP server")
    anyio.run(_run_stdio_server)


async def _run_stdio_server() -> None:
    """Use handshake-era stdio so iterative MCP sampling has a duplex back-channel."""
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

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Annotated, Literal

import anyio
from pydantic import BaseModel, Field

from .agent import ResearchAgent
from .config import Settings, budget_for
from .controller import ResearchController
from .model.base import ResearchModel
from .model.fallback import FallbackModelClient
from .model.mcp_sampling import MCPSamplingModelClient
from .model.openai_compatible import OpenAICompatibleModelClient
from .model.unavailable import UnavailableModelClient
from .models import Document
from .readers.crawl4ai import Crawl4AIReader
from .readers.http import HTTPReader
from .readers.quality import page_diagnostics
from .readers.router import LayeredReader, RenderMode
from .reranking import OpenAICompatibleReranker
from .search.searxng import SearXNGSearchProvider
from .storage import SQLiteStore

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
    "Never add a calendar year unless the user explicitly supplied that year."
)

READ_URL_TOOL_DESCRIPTION = (
    "Fetch and extract content from an already-known public HTTP(S) URL. Use read_url instead of "
    "curl or wget whenever the user supplies a URL and asks to read, inspect, summarize, or "
    "analyze that page. It uses safe bounded HTTP retrieval, Trafilatura with a basic HTML "
    "fallback, and automatic headless-Chromium escalation when rendering appears necessary. "
    "It reports HTTP and suspected error-page status and returns pageable inline content. Use "
    "web_search instead when sources need to be discovered or corroborated."
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
    search_queries: int
    pages_fetched: int
    distinct_domains: int
    elapsed_ms: int
    browsing_elapsed_ms: int
    cache_hits: int
    fetch_failures: int
    evidence_model: str
    evidence_model_attempts: int
    evidence_model_successes: int
    evidence_model_failures: int
    evidence_model_fallbacks: int
    evidence_model_disabled: bool
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
    next_cursor: int | None
    link_count: int
    links_included: bool
    links_truncated: bool


mcp = MCPServer(
    "Local Agentic Web Search",
    instructions=(
        "When the user supplies a URL and asks to read, inspect, summarize, or analyze that page, "
        "use read_url instead of curl, wget, or shell-based downloading. Use web_search when "
        "sources must be discovered, compared, or corroborated, and pass a self-contained request. "
        "For batched read_url calls, request a small max_chars value and omit links unless needed. "
        "Make one web_search call at a time; parallel calls contend for the same local model and "
        "are rejected. "
        "Preserve the user's temporal wording and never invent a calendar year for latest, "
        "recent, current, or today; web_search uses its server clock. "
        "The tool reads sources, tracks evidence gaps, and returns a cited synthesis."
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
        int,
        Field(
            ge=0,
            description=(
                "Zero-based character offset into the cached extracted content. Use next_cursor "
                "from a previous result to continue reading the page."
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
) -> ReadUrlOutput:
    """Read one known URL through the shared layered retrieval stack."""

    if not url.strip():
        raise ValueError("url must not be empty")
    settings = Settings.from_env()
    runtime = _create_reader_runtime(settings)
    try:
        await ctx.report_progress(progress=0.1, total=1.0, message="Fetching the supplied URL")
        document = await runtime.reader.read(url.strip(), render=render)
        await ctx.report_progress(
            progress=1.0,
            total=1.0,
            message=f"URL read with {document.method}",
        )
        return _read_url_output(
            document,
            settings,
            cursor=cursor,
            max_chars=max_chars,
            include_links=include_links,
        )
    finally:
        await runtime.close()


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
    runtime = _create_reader_runtime(settings)
    store = runtime.store
    search = SearXNGSearchProvider(
        settings.searxng_url,
        store=store,
        cache_ttl_seconds=settings.search_cache_ttl_seconds,
        user_agent=settings.user_agent,
    )
    reader = runtime.reader
    model = _create_model(ctx, settings)
    evidence_model = _create_evidence_model(settings, model)
    reranker = _create_reranker(settings)
    if settings.evidence_model_id:
        LOGGER.info(
            "Dedicated evidence model configured: %s at %s (authentication: %s)",
            settings.evidence_model_id,
            settings.evidence_model_base_url,
            "configured" if settings.evidence_model_api_key else "none",
        )
    else:
        LOGGER.info("Evidence analysis uses the Pi active model")
    controller = ResearchController(
        search=search,
        reader=reader,
        agent=ResearchAgent(
            model,
            evidence_model=evidence_model,
            evidence_model_name=settings.evidence_model_id or "pi-active",
        ),
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
                budget=budget_for(effort),
                progress=report,
            )
            LOGGER.info(
                "Evidence model usage: model=%s attempts=%d successes=%d failures=%d "
                "fallbacks=%d disabled=%s",
                result.stats.evidence_model,
                result.stats.evidence_model_attempts,
                result.stats.evidence_model_successes,
                result.stats.evidence_model_failures,
                result.stats.evidence_model_fallbacks,
                result.stats.evidence_model_disabled,
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
        await runtime.close()
        if reranker is not None:
            await reranker.close()
        if evidence_model is not model:
            await evidence_model.close()
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
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(settings.data_dir.resolve()))
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str((settings.data_dir / "ms-playwright").resolve())
    )
    store = SQLiteStore(settings.data_dir / "research.sqlite3")
    http_reader = HTTPReader(
        store=store,
        cache_ttl_seconds=settings.document_cache_ttl_seconds,
        user_agent=settings.user_agent,
        max_response_bytes=settings.max_response_bytes,
        allow_private_urls=settings.allow_private_urls,
        allow_proxy_fake_ips=settings.allow_proxy_fake_ips,
    )
    browser_reader = (
        Crawl4AIReader(
            store=store,
            user_agent=settings.user_agent,
            allow_private_urls=settings.allow_private_urls,
            allow_proxy_fake_ips=settings.allow_proxy_fake_ips,
        )
        if settings.enable_crawl4ai
        else None
    )
    return _ReaderRuntime(store=store, reader=LayeredReader(http_reader, browser_reader))


def _read_url_output(
    document: Document,
    settings: Settings,
    *,
    cursor: int = 0,
    max_chars: int = 4_000,
    include_links: bool = False,
) -> ReadUrlOutput:
    if cursor > len(document.content):
        raise ValueError(
            f"cursor {cursor} exceeds extracted content length {len(document.content)}"
        )
    content_limit = min(max_chars, settings.read_url_max_chars)
    link_limit = settings.read_url_max_links
    full_content = document.content
    all_links = document.links
    content_end = min(len(full_content), cursor + content_limit)
    has_more_content = content_end < len(full_content)
    content_truncated = cursor > 0 or has_more_content
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
    if links_truncated and include_links:
        warnings.append("tool_output_truncated:links")
    return ReadUrlOutput(
        url=document.url,
        final_url=document.final_url,
        title=document.title,
        content=full_content[cursor:content_end],
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
        content_start=cursor,
        content_end=content_end,
        has_more_content=has_more_content,
        next_cursor=content_end if has_more_content else None,
        link_count=len(all_links),
        links_included=include_links,
        links_truncated=links_truncated,
    )


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


def _create_evidence_model(settings: Settings, fallback: ResearchModel) -> ResearchModel:
    if not settings.evidence_model_id:
        return fallback
    preferred = OpenAICompatibleModelClient(
        settings.evidence_model_base_url,
        settings.evidence_model_id,
        api_key=settings.evidence_model_api_key,
        timeout_seconds=settings.evidence_model_timeout_seconds,
        max_tokens=settings.evidence_model_max_tokens,
        temperature=settings.evidence_model_temperature,
    )
    return FallbackModelClient(preferred, fallback)


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

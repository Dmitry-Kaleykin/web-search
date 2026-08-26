from __future__ import annotations

import logging
import os
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
from .readers.crawl4ai import Crawl4AIReader
from .readers.http import HTTPReader
from .readers.router import LayeredReader
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
    "Make one self-contained call for the whole request; do not invoke web_search in parallel. "
    "Pass the user's temporal wording faithfully. For relative requests such as latest, recent, "
    "current, or today, keep that wording relative; the server resolves it from its own clock. "
    "Never add a calendar year unless the user explicitly supplied that year."
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


mcp = MCPServer(
    "Local Agentic Web Search",
    instructions=(
        "Use web_search for current or web-dependent research. Pass a self-contained request. "
        "Make one web_search call at a time; parallel calls contend for the same local model and "
        "are rejected. "
        "Preserve the user's temporal wording and never invent a calendar year for latest, "
        "recent, current, or today; web_search uses its server clock. "
        "The tool reads sources, tracks evidence gaps, and returns a cited synthesis."
    ),
)


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
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(settings.data_dir.resolve()))
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH", str((settings.data_dir / "ms-playwright").resolve())
    )
    store = SQLiteStore(settings.data_dir / "research.sqlite3")
    search = SearXNGSearchProvider(
        settings.searxng_url,
        store=store,
        cache_ttl_seconds=settings.search_cache_ttl_seconds,
        user_agent=settings.user_agent,
    )
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
    reader = LayeredReader(http_reader, browser_reader)
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
            return WebSearchOutput.model_validate(result.as_dict())
        finally:
            RUN_GATE.finish()
    finally:
        await search.close()
        await reader.close()
        if reranker is not None:
            await reranker.close()
        if evidence_model is not model:
            await evidence_model.close()
        await model.close()
        store.close()


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

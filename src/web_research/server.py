from __future__ import annotations

import logging
import os
from typing import Literal

import anyio
from pydantic import BaseModel

from .agent import ResearchAgent
from .config import Settings, budget_for
from .controller import ResearchController
from .model.base import ResearchModel
from .model.mcp_sampling import MCPSamplingModelClient
from .model.openai_compatible import OpenAICompatibleModelClient
from .model.unavailable import UnavailableModelClient
from .readers.crawl4ai import Crawl4AIReader
from .readers.http import HTTPReader
from .readers.router import LayeredReader
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


class ToolSource(BaseModel):
    id: str
    url: str
    title: str
    domain: str
    source_class: Literal["primary", "expert", "independent", "news", "community", "unknown"]
    retrieved_at: str
    published_at: str | None
    extraction_method: str
    warnings: list[str]


class ToolCoverageItem(BaseModel):
    requirement_id: str
    covered: bool
    source_count: int
    has_primary: bool
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
    independent_domains: int
    elapsed_ms: int
    cache_hits: int
    fetch_failures: int


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
        "The tool reads sources, tracks evidence gaps, and returns a cited synthesis."
    ),
)


@mcp.tool(name="web_search", structured_output=True)
async def web_search(
    query: str,
    ctx: Context,
    effort: Literal["quick", "auto", "thorough"] = "auto",
    freshness: str | None = None,
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
    controller = ResearchController(
        search=search,
        reader=reader,
        agent=ResearchAgent(model),
        store=store,
    )

    async def report(value: float, message: str) -> None:
        await ctx.report_progress(progress=value, total=1.0, message=message)

    try:
        result = await controller.run(
            query.strip(),
            effort=effort,
            freshness=freshness,
            budget=budget_for(effort),
            progress=report,
        )
        return WebSearchOutput.model_validate(result.as_dict())
    finally:
        await search.close()
        await reader.close()
        await model.close()
        store.close()


def _create_model(ctx: Context, settings: Settings) -> ResearchModel:
    if MCPSamplingModelClient.supported(ctx):
        return MCPSamplingModelClient(
            ctx,
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

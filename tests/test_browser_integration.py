"""Real local Chromium evaluation. Run with WEB_SEARCH_RUN_BROWSER_TESTS=1."""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from web_research.readers.crawl4ai import Crawl4AIReader
from web_research.readers.http import HTTPReader
from web_research.readers.router import LayeredReader
from web_research.storage import SQLiteStore

pytestmark = pytest.mark.skipif(
    os.environ.get("WEB_SEARCH_RUN_BROWSER_TESTS") != "1",
    reason="Set WEB_SEARCH_RUN_BROWSER_TESTS=1 to run local Chromium integration",
)


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        requests: ClassVar[list[str]] = []

        def do_GET(self):
            self.requests.append(self.path)
            if self.path == "/robots.txt":
                content = b"User-agent: *\nAllow: /\n"
            elif self.path == "/error":
                content = b"<html><title>404 Not Found</title><body>Page not found</body></html>"
            else:
                content = b"""<!doctype html><html><head><title>Laboratory results</title></head>
                <body><main><p id="result">Loading...</p>
                <button role="tab" id="tab"
                onclick="document.querySelector('#result').textContent=
                'Tab result: verified value 73 units.'">Measurements</button>

                <details id="details">
                <summary>Methodology</summary>
                <p>Measured with a calibrated instrument.</p></details>
                <button id="more"
                onclick="document.querySelector('#result').textContent=
                'Additional result: verified value 84 units.'">Load more</button>
                <button id="delete"
                onclick="fetch('/danger',{method:'POST'})">Delete account</button>
                <script>setTimeout(()=>
                document.querySelector('#result').textContent=
                'Laboratory result: verified value 42 units. ' +
                'This measurement comes from three calibrated instruments and was independently '+
                'checked by the laboratory staff.',100);</script>
                </main></body></html>"""
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/plain" if self.path == "/robots.txt" else "text/html"
            )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", Handler.requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def browser_environment(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(project / ".web-search-data/ms-playwright"))
    monkeypatch.setenv("CRAWL4_AI_BASE_DIRECTORY", str(tmp_path))


async def test_javascript_article_and_readonly_interaction(
    local_site, browser_environment, tmp_path
):
    base, requests = local_site
    store = SQLiteStore(tmp_path / "cache.sqlite3")
    browser = Crawl4AIReader(store=store, allow_private_urls=True, max_concurrent_renders=1)
    reader = LayeredReader(HTTPReader(store=store, allow_private_urls=True), browser)
    try:
        article = await reader.read(base + "/article", render="always", visual=True)
        assert "42 units" in article.content
        assert article.images
        offered_tab = next(item for item in article.available_actions if item["kind"] == "tab")
        offered = await reader.read(
            base + "/article",
            actions=[
                {
                    "kind": offered_tab["kind"],
                    "selector": offered_tab["selector"],
                }
            ],
        )
        assert "73 units" in offered.content
        tab = await reader.read(base + "/article", actions=[{"kind": "tab", "selector": "#tab"}])
        assert "73 units" in tab.content
        more = await reader.read(
            base + "/article", actions=[{"kind": "load_more", "selector": "#more"}]
        )
        assert "84 units" in more.content
        invalid = await reader.read(
            base + "/article", actions=[{"kind": "load_more", "selector": "#delete"}]
        )
        assert any(w.startswith("browser_fallback_failed:") for w in invalid.warnings)
        assert "/danger" not in requests
    finally:
        await reader.close()
        store.close()


async def test_http_soft_error_is_not_clean_evidence(local_site, browser_environment):
    base, _ = local_site
    reader = LayeredReader(
        HTTPReader(allow_private_urls=True), Crawl4AIReader(allow_private_urls=True)
    )
    try:
        document = await reader.read(base + "/error")
        assert any(w.startswith("suspected_error_page:") for w in document.warnings)
    finally:
        await reader.close()

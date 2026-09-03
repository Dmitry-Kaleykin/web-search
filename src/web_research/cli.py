from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

from .config import Settings


async def _doctor() -> int:
    try:
        import httpx
    except ImportError:
        print("FAIL dependencies: run `python -m pip install -e .`", file=sys.stderr)
        return 1

    settings = Settings.from_env()
    failed = False
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(settings.data_dir.resolve()))
    browser_dir = Path(
        os.environ.setdefault(
            "PLAYWRIGHT_BROWSERS_PATH",
            str((settings.data_dir / "ms-playwright").resolve()),
        )
    ).expanduser()
    print(f"OK   data directory: {settings.data_dir.resolve()}")
    if settings.allow_proxy_fake_ips:
        print("INFO proxy fake-IP DNS compatibility: enabled for 198.18.0.0/15")
    if settings.enable_crawl4ai:
        if importlib.util.find_spec("crawl4ai") is None:
            failed = True
            print(
                "FAIL Crawl4AI fallback is enabled but not installed; "
                "install `.[browser]` and run `crawl4ai-setup`",
                file=sys.stderr,
            )
        else:
            print("OK   Crawl4AI Python package is installed")
            if _browser_runtime_present(browser_dir):
                print(f"OK   Chromium runtime: {browser_dir.resolve()}")
            else:
                failed = True
                print(
                    f"FAIL Chromium runtime not found in {browser_dir.resolve()}; "
                    "run the setup command from README.md",
                    file=sys.stderr,
                )

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.get(
                f"{settings.searxng_url.rstrip('/')}/search",
                params={"q": "searxng", "format": "json"},
            )
            response.raise_for_status()
            if "json" not in response.headers.get("content-type", "").casefold():
                raise RuntimeError(
                    "answered text/html instead of JSON; this is an anti-bot challenge page or "
                    "'json' is missing from search.formats"
                )
            payload = response.json()
            results = payload.get("results", [])
            count = len(results)
            print(f"OK   SearXNG JSON API: {count} result(s)")

            # A non-empty result set can still mean the search is broken: SearXNG returns 200 with
            # whatever survived, so one reachable index answering alone is a degraded instance.
            engines: dict[str, int] = {}
            for item in results:
                for name in item.get("engines") or ([item["engine"]] if item.get("engine") else []):
                    engines[str(name)] = engines.get(str(name), 0) + 1
            unresponsive = payload.get("unresponsive_engines") or []
            if unresponsive:
                print(
                    "WARN unresponsive upstream engines: "
                    + ", ".join(
                        f"{entry[0]} ({entry[1]})" if isinstance(entry, list) else str(entry)
                        for entry in unresponsive[:8]
                    )
                )
            if count and len(engines) == 1:
                only = next(iter(engines))
                print(
                    f"WARN all {count} results came from the single engine '{only}'; widen the "
                    "engine set in docker/searxng/settings.yml",
                    file=sys.stderr,
                )
            elif engines:
                print(
                    "OK   engine diversity: "
                    + ", ".join(f"{name}={hits}" for name, hits in sorted(engines.items()))
                )
            elif count:
                print("WARN results carry no engine attribution; diversity cannot be verified")
        except Exception as exc:
            failed = True
            print(f"FAIL SearXNG JSON API: {exc}", file=sys.stderr)

        if not settings.model_id:
            print("OK   model strategy: dynamic MCP client sampling")
            print("INFO no direct model fallback is configured")
        else:
            headers = {}
            if settings.model_api_key:
                headers["Authorization"] = f"Bearer {settings.model_api_key}"
            try:
                response = await client.get(
                    f"{settings.model_base_url.rstrip('/')}/models", headers=headers
                )
                response.raise_for_status()
                models = response.json().get("data", [])
                ids = {str(item.get("id")) for item in models if isinstance(item, dict)}
                if ids and settings.model_id not in ids:
                    failed = True
                    print(
                        f"FAIL configured fallback model {settings.model_id!r} is not in "
                        f"/models: {json.dumps(sorted(ids))}",
                        file=sys.stderr,
                    )
                else:
                    print(f"OK   direct fallback model endpoint: {settings.model_id}")
            except Exception as exc:
                failed = True
                print(f"FAIL direct fallback model endpoint: {exc}", file=sys.stderr)

        if not settings.evidence_model_id:
            print("INFO no dedicated evidence model is configured")
        else:
            headers = {}
            if settings.evidence_model_api_key:
                headers["Authorization"] = f"Bearer {settings.evidence_model_api_key}"
            try:
                response = await client.get(
                    f"{settings.evidence_model_base_url.rstrip('/')}/models", headers=headers
                )
                response.raise_for_status()
                models = response.json().get("data", [])
                ids = {str(item.get("id")) for item in models if isinstance(item, dict)}
                if settings.evidence_model_id not in ids:
                    failed = True
                    print(
                        f"FAIL configured evidence model {settings.evidence_model_id!r} is not "
                        f"in /models: {json.dumps(sorted(ids))}",
                        file=sys.stderr,
                    )
                else:
                    print(f"OK   dedicated evidence model: {settings.evidence_model_id}")
            except Exception as exc:
                failed = True
                print(f"FAIL dedicated evidence model endpoint: {exc}", file=sys.stderr)

        if not settings.reranker_model_id:
            print("INFO no semantic candidate reranker is configured")
        else:
            headers = {}
            if settings.reranker_api_key:
                headers["Authorization"] = f"Bearer {settings.reranker_api_key}"
            try:
                response = await client.post(
                    f"{settings.reranker_base_url.rstrip('/')}/rerank",
                    headers=headers,
                    json={
                        "model": settings.reranker_model_id,
                        "query": "web research",
                        "documents": ["web research evidence", "unrelated decorative text"],
                        "top_n": 2,
                        "return_documents": False,
                    },
                )
                response.raise_for_status()
                results = response.json().get("results", [])
                if not isinstance(results, list) or len(results) != 2:
                    raise ValueError("endpoint did not return two candidate scores")
                print(f"OK   semantic reranker: {settings.reranker_model_id}")
            except Exception as exc:
                failed = True
                print(f"FAIL semantic reranker endpoint: {exc}", file=sys.stderr)

        failed = _storage_report(settings, failed)
    return 1 if failed else 0


def _storage_report(settings: Settings, failed: bool) -> bool:
    """Report cache growth so unbounded growth is visible instead of inferred from disk."""
    from .storage import SQLiteStore

    path = settings.data_dir / "research.sqlite3"
    if not path.exists():
        print("INFO no research database yet")
        return failed
    try:
        store = SQLiteStore(
            path,
            search_ttl_seconds=settings.search_cache_ttl_seconds,
            document_ttl_seconds=settings.document_cache_ttl_seconds,
            search_max_rows=settings.cache_search_max_rows,
            document_max_rows=settings.cache_document_max_rows,
            document_max_payload_bytes=settings.cache_document_max_payload_bytes,
        )
        try:
            stats = store.stats()
        finally:
            store.close()
    except Exception as exc:
        print(f"FAIL storage: {exc}", file=sys.stderr)
        return True
    file_mb = stats["file_bytes"] / 1e6
    documents = stats["document_cache"]
    largest_mb = documents["largest_row_bytes"] / 1e6
    print(
        f"OK   storage: {file_mb:.1f} MB | documents={documents['rows']} "
        f"({documents['bytes'] / 1e6:.1f} MB, largest {largest_mb:.2f} MB) | "
        f"searches={stats['search_cache']['rows']} | events={stats['events']['rows']}"
    )
    ceiling_mb = settings.cache_document_max_payload_bytes / 1e6
    if largest_mb > ceiling_mb:
        print(
            f"WARN cached document exceeds the {ceiling_mb:.1f} MB ceiling; "
            "run web-search-maint to evict and compact",
            file=sys.stderr,
        )
    if file_mb > 100:
        print("WARN database is over 100 MB; run web-search-maint to compact", file=sys.stderr)
    return failed


def _db_bytes(path: Path) -> int:
    """Main database plus WAL and SHM sidecars, which hold committed data."""
    total = path.stat().st_size if path.exists() else 0
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            total += sidecar.stat().st_size
    return total


def _maintenance() -> int:
    from .storage import SQLiteStore

    settings = Settings.from_env()
    path = settings.data_dir / "research.sqlite3"
    if not path.exists():
        print("no database to maintain")
        return 0
    before = _db_bytes(path) / 1e6
    store = SQLiteStore(
        path,
        search_ttl_seconds=settings.search_cache_ttl_seconds,
        document_ttl_seconds=settings.document_cache_ttl_seconds,
        search_max_rows=settings.cache_search_max_rows,
        document_max_rows=settings.cache_document_max_rows,
        document_max_payload_bytes=settings.cache_document_max_payload_bytes,
    )
    try:
        report = store.maintenance()
    finally:
        store.close()
    removed = report["rows_removed"]
    after = report["file_bytes"] / 1e6
    print(
        f"evicted search_cache={removed['search_cache']} "
        f"document_cache={removed['document_cache']}"
    )
    for table in ("search_cache", "document_cache", "research_runs", "events"):
        entry = report[table]
        print(
            f"  {table:14} rows={entry['rows']:<5} "
            f"{entry['bytes'] / 1e6:6.1f} MB largest={entry['largest_row_bytes'] / 1e6:.2f} MB"
        )
    print(f"database {before:.1f} MB -> {after:.1f} MB")
    return 0


def maintenance_main() -> None:
    raise SystemExit(_maintenance())


def _browser_runtime_present(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    executable_names = {
        "Chromium",
        "chrome",
        "chrome.exe",
        "chrome-headless-shell",
        "headless_shell",
    }
    return any(path.is_file() and path.name in executable_names for path in directory.rglob("*"))


def doctor_main() -> None:
    raise SystemExit(asyncio.run(_doctor()))

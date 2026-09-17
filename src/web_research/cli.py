from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
from pathlib import Path

from .config import Settings
from .search.searxng import SearXNGSearchProvider
from .storage import SQLiteStore


async def _doctor() -> int:
    if importlib.util.find_spec("httpx") is None:
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
    if shutil.which("tesseract"):
        print("OK   Tesseract OCR is available")
    else:
        print(
            "WARN Tesseract OCR is unavailable; install tesseract for scanned text. "
            "PDF layout extraction and visual page output remain available."
        )
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

    store = SQLiteStore(settings.data_dir / "research.sqlite3")
    search = SearXNGSearchProvider(
        settings.searxng_url,
        store=store,
        max_retries=0,
        timeout_seconds=min(20.0, settings.search_timeout_seconds),
        healthy_engines=settings.search_healthy_engines,
        user_agent=settings.user_agent,
    )
    try:
        async with asyncio.timeout(settings.search_timeout_seconds):
            if not search.healthy_engines:
                raise RuntimeError("No search engines configured")
            results = await search.search(
                "searxng",
                engines=",".join(search.healthy_engines),
                refresh=True,
            )
        print(f"OK   SearXNG JSON API: {len(results)} result(s)")
        engines: dict[str, int] = {}
        for item in results:
            for name in item.engines:
                engines[name] = engines.get(name, 0) + 1
        if len(engines) == 1:
            print(f"WARN all results came from one engine: {next(iter(engines))}")
        elif engines:
            print("OK   engines: " + ", ".join(f"{name}={hits}" for name, hits in engines.items()))
    except Exception as exc:
        failed = True
        print(f"FAIL SearXNG JSON API: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        for warning in search.last_warnings:
            print(f"WARN {warning}")
        await search.close()
        store.close()

    print("OK   reasoning: handled by the calling model; no model endpoint or sampling needed")
    failed = _storage_report(settings, failed)
    return 1 if failed else 0


def _storage_report(settings: Settings, failed: bool) -> bool:
    """Report cache growth so unbounded growth is visible instead of inferred from disk."""
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
            # The payoff of persisting cooldowns: doctor runs in its own process, so this is how
            # it can report what the running server (or an earlier one) already learned.
            cooldowns = store.active_engine_cooldowns()
        finally:
            store.close()
    except Exception as exc:
        print(f"FAIL storage: {exc}", file=sys.stderr)
        return True
    for engine, (reason, remaining) in sorted(cooldowns.items()):
        print(
            f"WARN engine cooldown on {engine}: {reason} ({int(remaining)}s left, survives restart)"
        )
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
        f"evicted search_cache={removed['search_cache']} document_cache={removed['document_cache']}"
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

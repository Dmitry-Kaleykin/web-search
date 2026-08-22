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
            payload = response.json()
            count = len(payload.get("results", []))
            print(f"OK   SearXNG JSON API: {count} result(s)")
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
    return 1 if failed else 0


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

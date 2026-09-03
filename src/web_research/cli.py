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

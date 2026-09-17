from __future__ import annotations

import io
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from web_research.cli import _browser_runtime_present, _doctor
from web_research.config import Settings
from web_research.storage import SQLiteStore


class DoctorBrowserRuntimeTests(unittest.TestCase):
    def test_detects_browser_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "chromium-1" / "chrome-mac" / "Chromium"
            binary.parent.mkdir(parents=True)
            binary.touch()

            self.assertTrue(_browser_runtime_present(Path(directory)))

    def test_rejects_empty_browser_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(_browser_runtime_present(Path(directory)))


class DoctorSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_doctor_does_not_probe_models_and_respects_cooldowns(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                data_dir=Path(directory),
                enable_crawl4ai=False,
                search_healthy_engines="brave,google cse",
                model_id="unused-model",
                reranker_model_id="unused-reranker",
            )
            store = SQLiteStore(Path(directory) / "research.sqlite3")
            store.record_engine_cooldown("brave", "too many requests", time.time() + 900)
            store.close()
            with (
                patch("web_research.cli.Settings.from_env", return_value=settings),
                patch("httpx.AsyncClient.get", new_callable=AsyncMock) as get,
                patch("httpx.AsyncClient.post", new_callable=AsyncMock) as post,
                patch(
                    "web_research.search.searxng.SearXNGSearchProvider._request",
                    new_callable=AsyncMock,
                ) as request,
                redirect_stdout(io.StringIO()),
            ):
                request.return_value = {"results": [], "failures": []}
                result = await _doctor()
            self.assertEqual(result, 0)
            self.assertEqual(request.call_args.args[0]["engines"], "google cse")
            get.assert_not_awaited()
            post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

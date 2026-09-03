from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from web_research.models import Document, SearchResult
from web_research.storage import SQLiteStore


def _document(url: str, content: str = "body") -> Document:
    return Document(url=url, final_url=url, title="title", content=content, method="http")


def _search(url: str) -> list[SearchResult]:
    return [SearchResult(url=url, title="title")]


class SQLiteStoreCacheTests(unittest.TestCase):
    def _store(self, directory: str, **kwargs) -> SQLiteStore:
        return SQLiteStore(Path(directory) / "cache.sqlite3", **kwargs)

    def _age(self, store: SQLiteStore, table: str, url: str, seconds: float) -> None:
        with store._connection:
            store._connection.execute(
                f"UPDATE {table} SET stored_at = ? WHERE rowid = "
                "(SELECT MAX(rowid) FROM " + table + ")",
                (time.time() - seconds,),
            )

    def test_expired_rows_are_deleted_not_just_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory, document_ttl_seconds=60, prune_every_n_writes=9999)
            store.put_document("https://example.com/a", _document("https://example.com/a"))
            self._age(store, "document_cache", "x", 120)
            removed = store.prune()
            self.assertEqual(removed["document_cache"], 1)
            self.assertIsNone(store.get_document("https://example.com/a", 60))
            remaining = store._connection.execute(
                "SELECT COUNT(*) FROM document_cache"
            ).fetchone()[0]
            self.assertEqual(remaining, 0)
            store.close()

    def test_row_ceiling_keeps_the_newest_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(
                directory,
                document_ttl_seconds=86_400,
                document_max_rows=2,
                prune_every_n_writes=9999,
            )
            for index in range(6):
                store.put_document(f"https://example.com/{index}", _document("body"))
                time.sleep(0.002)
            store.prune()
            urls = [
                row[0]
                for row in store._connection.execute(
                    "SELECT url FROM document_cache ORDER BY stored_at ASC"
                )
            ]
            self.assertEqual(len(urls), 2)
            self.assertEqual(urls[-1], "https://example.com/5")
            store.close()

    def test_oversized_rows_are_evicted_even_while_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(
                directory,
                document_ttl_seconds=86_400,
                document_max_payload_bytes=1_000,
                prune_every_n_writes=9999,
            )
            store.put_document("https://example.com/small", _document("tiny"))
            store.put_document("https://example.com/huge", _document("x" * 20_000))
            removed = store.prune()
            self.assertEqual(removed["document_cache"], 1)
            self.assertIsNotNone(store.get_document("https://example.com/small", 86_400))
            self.assertIsNone(store.get_document("https://example.com/huge", 86_400))
            store.close()

    def test_pruning_is_amortised_across_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(
                directory,
                search_ttl_seconds=30,
                search_max_rows=100,
                prune_every_n_writes=1,
            )
            store.put_search("old", _search("https://example.com/old"))
            self._age(store, "search_cache", "x", 60)
            store.put_search("fresh", _search("https://example.com/fresh"))
            rows = store._connection.execute("SELECT COUNT(*) FROM search_cache").fetchone()[0]
            self.assertEqual(rows, 1)
            self.assertIsNotNone(store.get_search("fresh", 30))
            store.close()

    def test_maintenance_compacts_the_file_after_large_rows_leave(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            store = SQLiteStore(path, document_ttl_seconds=86_400, prune_every_n_writes=9999)
            for index in range(8):
                store.put_document(f"https://example.com/{index}", _document("x" * 400_000))
            store.close()

            store = SQLiteStore(
                path,
                document_ttl_seconds=86_400,
                document_max_rows=1,
                document_max_payload_bytes=1_000_000,
                prune_every_n_writes=9999,
            )
            before = store.stats()["file_bytes"]
            report = store.maintenance()
            after = store.stats()["file_bytes"]
            self.assertEqual(report["rows_removed"]["document_cache"], 7)
            self.assertLess(after, before)
            self.assertEqual(report["document_cache"]["rows"], 1)
            store.close()

    def test_cooldowns_persist_and_are_restored_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.sqlite3"
            store = SQLiteStore(path)
            store.record_engine_cooldown("brave", "CAPTCHA challenge", time.time() + 1200)
            store.close()

            reopened = SQLiteStore(path)
            active = reopened.active_engine_cooldowns()
            self.assertIn("brave", active)
            self.assertEqual(active["brave"][0], "CAPTCHA challenge")
            self.assertGreater(active["brave"][1], 1100)
            reopened.close()

    def test_a_shorter_later_failure_never_shortens_a_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.record_engine_cooldown("brave", "CAPTCHA challenge", time.time() + 1800)
            store.record_engine_cooldown("brave", "rate limited", time.time() + 60)
            active = store.active_engine_cooldowns()
            self.assertEqual(active["brave"][0], "CAPTCHA challenge")
            self.assertGreater(active["brave"][1], 1700)
            store.close()

    def test_expired_cooldowns_are_dropped_not_just_hidden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            store.record_engine_cooldown("brave", "CAPTCHA challenge", time.time() - 1)
            self.assertEqual(store.active_engine_cooldowns(), {})
            remaining = store._connection.execute(
                "SELECT COUNT(*) FROM engine_health"
            ).fetchone()[0]
            self.assertEqual(remaining, 0)
            store.close()


if __name__ == "__main__":
    unittest.main()

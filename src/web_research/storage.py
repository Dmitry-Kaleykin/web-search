from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import Document, ResearchResult, SearchResult


class SQLiteStore:
    """Local cache and run journal.

    TTLs are enforced on read, which is not the same as eviction: without deletion the file grows
    monotonically and every read pays the cost of scanning an ever-larger index. Writes therefore
    carry amortised pruning, and ``maintenance()`` reclaims space that pruning alone leaves behind
    as free pages.
    """

    def __init__(
        self,
        path: Path,
        *,
        search_ttl_seconds: int = 900,
        document_ttl_seconds: int = 21_600,
        search_max_rows: int = 2_000,
        document_max_rows: int = 400,
        document_max_payload_bytes: int = 2_000_000,
        prune_every_n_writes: int = 25,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.search_ttl_seconds = search_ttl_seconds
        self.document_ttl_seconds = document_ttl_seconds
        self.search_max_rows = max(1, search_max_rows)
        self.document_max_rows = max(1, document_max_rows)
        self.document_max_payload_bytes = max(1_000, document_max_payload_bytes)
        self.prune_every_n_writes = max(1, prune_every_n_writes)
        self._writes_since_prune = 0
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS search_cache (
                    cache_key TEXT PRIMARY KEY,
                    stored_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_cache (
                    url TEXT PRIMARY KEY,
                    stored_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_runs (
                    id TEXT PRIMARY KEY,
                    started_at REAL NOT NULL,
                    completed_at REAL,
                    query TEXT NOT NULL,
                    effort TEXT NOT NULL,
                    result TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    research_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )

    def get_search(self, key: str, ttl_seconds: int) -> list[SearchResult] | None:
        row = self._get_fresh("search_cache", "cache_key", key, ttl_seconds)
        if row is None:
            return None
        return [SearchResult(**item) for item in json.loads(row["payload"])]

    def put_search(self, key: str, results: list[SearchResult]) -> None:
        payload = json.dumps([_object_dict(item) for item in results], ensure_ascii=False)
        self._upsert_cache(
            "search_cache",
            "cache_key",
            key,
            payload,
            ttl_seconds=self.search_ttl_seconds,
            max_rows=self.search_max_rows,
        )

    def get_document(self, url: str, ttl_seconds: int) -> Document | None:
        row = self._get_fresh("document_cache", "url", url, ttl_seconds)
        return Document(**json.loads(row["payload"])) if row else None

    def put_document(self, url: str, document: Document) -> None:
        payload = json.dumps(_object_dict(document), ensure_ascii=False)
        self._upsert_cache(
            "document_cache",
            "url",
            url,
            payload,
            ttl_seconds=self.document_ttl_seconds,
            max_rows=self.document_max_rows,
        )

    def start_run(self, research_id: str, query: str, effort: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO research_runs(id, started_at, query, effort) VALUES (?, ?, ?, ?)",
                (research_id, time.time(), query, effort),
            )

    def finish_run(self, result: ResearchResult) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE research_runs SET completed_at = ?, result = ? WHERE id = ?",
                (time.time(), json.dumps(result.as_dict(), ensure_ascii=False), result.research_id),
            )

    def cancel_run(self, research_id: str) -> None:
        """Finalize an unfinished run and record its cancellation atomically."""
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE research_runs SET completed_at = ? WHERE id = ? AND completed_at IS NULL",
                (time.time(), research_id),
            )
            if cursor.rowcount:
                self._connection.execute(
                    "INSERT INTO events(research_id, created_at, event_type, payload) "
                    "VALUES (?, ?, 'cancelled', '{}')",
                    (research_id, time.time()),
                )

    def event(self, research_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO events(research_id, created_at, event_type, payload) "
                "VALUES (?, ?, ?, ?)",
                (research_id, time.time(), event_type, json.dumps(payload, ensure_ascii=False)),
            )

    def _get_fresh(
        self, table: str, key_column: str, key: str, ttl_seconds: int
    ) -> sqlite3.Row | None:
        threshold = time.time() - ttl_seconds
        with self._lock:
            return self._connection.execute(
                f"SELECT payload FROM {table} WHERE {key_column} = ? AND stored_at >= ?",
                (key, threshold),
            ).fetchone()

    def _upsert_cache(
        self,
        table: str,
        key_column: str,
        key: str,
        payload: str,
        *,
        ttl_seconds: int,
        max_rows: int,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                f"INSERT INTO {table}({key_column}, stored_at, payload) VALUES (?, ?, ?) "
                f"ON CONFLICT({key_column}) DO UPDATE SET stored_at=excluded.stored_at, "
                "payload=excluded.payload",
                (key, time.time(), payload),
            )
            self._writes_since_prune += 1
            if self._writes_since_prune >= self.prune_every_n_writes:
                self._writes_since_prune = 0
                self._prune_table(
                    table,
                    ttl_seconds=ttl_seconds,
                    max_rows=max_rows,
                    max_payload_bytes=(
                        self.document_max_payload_bytes if table == "document_cache" else 0
                    ),
                )

    def _prune_table(
        self, table: str, *, ttl_seconds: int, max_rows: int, max_payload_bytes: int = 0
    ) -> int:
        """Drop expired rows, oversized rows, and everything older than the row ceiling."""
        removed = 0
        with self._lock, self._connection:
            removed += self._connection.execute(
                f"DELETE FROM {table} WHERE stored_at < ?", (time.time() - ttl_seconds,)
            ).rowcount
            if max_payload_bytes:
                removed += self._connection.execute(
                    f"DELETE FROM {table} WHERE LENGTH(payload) > ?", (max_payload_bytes,)
                ).rowcount
            removed += self._connection.execute(
                f"DELETE FROM {table} WHERE rowid NOT IN "
                f"(SELECT rowid FROM {table} ORDER BY stored_at DESC LIMIT ?)",
                (max_rows,),
            ).rowcount
        return removed

    def prune(self) -> dict[str, int]:
        """Evict expired, oversized, and over-ceiling cache rows."""
        return {
            "search_cache": self._prune_table(
                "search_cache",
                ttl_seconds=self.search_ttl_seconds,
                max_rows=self.search_max_rows,
            ),
            "document_cache": self._prune_table(
                "document_cache",
                ttl_seconds=self.document_ttl_seconds,
                max_rows=self.document_max_rows,
                max_payload_bytes=self.document_max_payload_bytes,
            ),
        }

    def maintenance(self) -> dict[str, Any]:
        """Prune every cache and compact the file. Unsafe to run alongside active caching.

        WAL mode means freed pages and VACUUM's rewritten database can both sit in the
        ``-wal`` sidecar while the main file keeps reporting its old size, so the checkpoint
        is what actually returns the bytes to the filesystem. Without it ``maintenance()``
        would report success while disk usage stayed exactly where it was.
        """
        with self._lock:
            removed = self.prune()
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.execute("VACUUM")
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        stats = self.stats()
        stats["rows_removed"] = removed
        return stats

    def stats(self) -> dict[str, Any]:
        """Row counts and payload bytes per table, for diagnostics."""
        tables = {
            "search_cache": "payload",
            "document_cache": "payload",
            "research_runs": "result",
            "events": "payload",
        }
        out: dict[str, Any] = {}
        with self._lock:
            for table, blob_column in tables.items():
                row = self._connection.execute(
                    f"SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH({blob_column})), 0) AS bytes, "
                    f"COALESCE(MAX(LENGTH({blob_column})), 0) AS largest FROM {table}"
                ).fetchone()
                out[table] = {
                    "rows": int(row["n"]),
                    "bytes": int(row["bytes"]),
                    "largest_row_bytes": int(row["largest"]),
                }
            out["file_bytes"] = self.path.stat().st_size if self.path.exists() else 0
            # WAL and SHM sidecars hold real committed data; ignoring them under-reports usage.
            for suffix in ("-wal", "-shm"):
                sidecar = self.path.with_name(self.path.name + suffix)
                out["sidecar_bytes"] = out.get("sidecar_bytes", 0) + (
                    sidecar.stat().st_size if sidecar.exists() else 0
                )
            out["file_bytes"] += out["sidecar_bytes"]
        return out


def _object_dict(value: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(value)

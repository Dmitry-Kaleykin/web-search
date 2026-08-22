from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import Document, ResearchResult, SearchResult


class SQLiteStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
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
        self._upsert_cache("search_cache", "cache_key", key, payload)

    def get_document(self, url: str, ttl_seconds: int) -> Document | None:
        row = self._get_fresh("document_cache", "url", url, ttl_seconds)
        return Document(**json.loads(row["payload"])) if row else None

    def put_document(self, url: str, document: Document) -> None:
        payload = json.dumps(_object_dict(document), ensure_ascii=False)
        self._upsert_cache("document_cache", "url", url, payload)

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

    def _upsert_cache(self, table: str, key_column: str, key: str, payload: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                f"INSERT INTO {table}({key_column}, stored_at, payload) VALUES (?, ?, ?) "
                f"ON CONFLICT({key_column}) DO UPDATE SET stored_at=excluded.stored_at, "
                "payload=excluded.payload",
                (key, time.time(), payload),
            )


def _object_dict(value: Any) -> dict[str, Any]:
    from dataclasses import asdict

    return asdict(value)

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Document, SearchResult


@dataclass(slots=True)
class CachedSearch:
    results: list[SearchResult]
    retrieved_at: str | None
    warnings: list[str]


class SQLiteStore:
    """Local retrieval caches, read snapshots, and engine cooldowns.

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
        self._upgrade_health()

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
                CREATE TABLE IF NOT EXISTS read_snapshots (
                    id TEXT PRIMARY KEY,
                    stored_at REAL NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS engine_health (
                    engine TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def _upgrade_health(self) -> None:
        # Serialize migrations across MCP/doctor processes sharing the database.
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            columns = {
                row[1] for row in self._connection.execute("PRAGMA table_info(engine_health)")
            }
            if "state" not in columns:
                self._connection.execute(
                    "ALTER TABLE engine_health ADD COLUMN state TEXT NOT NULL DEFAULT '{}'"
                )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS backend_metadata ("
                "url TEXT PRIMARY KEY, stored_at REAL NOT NULL, payload TEXT NOT NULL)"
            )

    def engine_records(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                row["engine"]: {**dict(row), "state": json.loads(row["state"])}
                for row in self._connection.execute("SELECT * FROM engine_health")
            }

    def update_engine(self, engine: str, update: Callable) -> dict[str, Any]:
        """Atomic state transitions, including exclusive recovery leases across processes."""
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT * FROM engine_health WHERE engine = ?", (engine,)
            ).fetchone()
            record = (
                {**dict(row), "state": json.loads(row["state"])}
                if row
                else {"engine": engine, "reason": "", "expires_at": 0, "updated_at": 0, "state": {}}
            )
            update(record)
            self._connection.execute(
                "INSERT INTO engine_health(engine, reason, expires_at, updated_at, state) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(engine) DO UPDATE SET "
                "reason=excluded.reason, expires_at=excluded.expires_at, "
                "updated_at=excluded.updated_at, state=excluded.state",
                (
                    engine,
                    record["reason"],
                    record["expires_at"],
                    record["updated_at"],
                    json.dumps(record["state"]),
                ),
            )
            return record

    def put_backend_metadata(self, url: str, payload: dict) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO backend_metadata VALUES (?, ?, ?)",
                (url, time.time(), json.dumps(payload)),
            )

    def get_backend_metadata(self, url: str, ttl: float = 900) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM backend_metadata WHERE url = ? AND stored_at >= ?",
                (url, time.time() - ttl),
            ).fetchone()
            return json.loads(row[0]) if row else None

    def put_snapshot(self, document: Document) -> str:
        """Immutable, expiring continuation state, bounded independently of mutable URL caches."""
        payload = json.dumps(_object_dict(document), ensure_ascii=False)
        if len(payload.encode("utf-8")) > 8_000_000:
            raise ValueError(
                "Extracted document exceeds the 8 MB snapshot limit; "
                "request a smaller source resource"
            )
        token = uuid.uuid4().hex
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO read_snapshots VALUES (?, ?, ?)", (token, time.time(), payload)
            )
            self._prune_snapshots()
        return token

    def get_snapshot(self, token: str) -> Document:
        row = self._get_fresh("read_snapshots", "id", token, 3600)
        if row is None:
            raise ValueError("Read cursor expired or was evicted; restart with cursor=0")
        return Document(**json.loads(row["payload"]))

    def _prune_snapshots(self) -> int:
        removed = self._prune_table("read_snapshots", ttl_seconds=3600, max_rows=100)
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT id, LENGTH(CAST(payload AS BLOB)) AS size FROM read_snapshots "
                "ORDER BY stored_at DESC"
            ).fetchall()
            total = 0
            for row in rows:
                total += row["size"]
                if total > 32_000_000:
                    removed += self._connection.execute(
                        "DELETE FROM read_snapshots WHERE id = ?", (row["id"],)
                    ).rowcount
        return removed

    def get_search(self, key: str, ttl_seconds: int) -> list[SearchResult] | None:
        cached = self.get_search_entry(key, ttl_seconds)
        return cached.results if cached is not None else None

    def get_search_entry(self, key: str, ttl_seconds: int) -> CachedSearch | None:
        row = self._get_fresh("search_cache", "cache_key", key, ttl_seconds)
        if row is None:
            return None
        payload = json.loads(row["payload"])
        if isinstance(payload, list):
            # Pre-provenance caches remain usable, but cannot claim a clean retrieval.
            return CachedSearch(
                [SearchResult(**item) for item in payload],
                None,
                [
                    "search_cache_legacy:original retrieval time and diagnostics unavailable; "
                    "use refresh=true to retrieve again"
                ],
            )
        return CachedSearch(
            [SearchResult(**item) for item in payload["results"]],
            payload["retrieved_at"],
            payload["warnings"],
        )

    def put_search(
        self,
        key: str,
        results: list[SearchResult],
        *,
        retrieved_at: str | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "results": [_object_dict(item) for item in results],
                "retrieved_at": retrieved_at or datetime.now(UTC).isoformat(),
                "warnings": warnings or [],
            },
            ensure_ascii=False,
        )
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
        if row is None:
            return None
        document = Document(**json.loads(row["payload"]))
        try:
            retrieved = datetime.fromisoformat(document.retrieved_at.replace("Z", "+00:00"))
            if retrieved.tzinfo is None:
                retrieved = retrieved.replace(tzinfo=UTC)
            age = time.time() - retrieved.timestamp()
        except ValueError:
            return None
        # Writing a focused variant must not reset the age of the underlying evidence.
        return document if 0 <= age <= ttl_seconds else None

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

    def record_engine_cooldown(self, engine: str, reason: str, expires_at: float) -> None:
        """Persist an engine cooldown so it survives a restart and crosses processes.

        ``expires_at`` must be wall-clock (``time.time()``). Monotonic values have an arbitrary
        origin per process, so persisting one would make every restored cooldown either already
        expired or permanently stuck.
        """
        now = time.time()
        with self._lock, self._connection:
            # A cooldown is evidence: never let a later, shorter failure shorten it.
            self._connection.execute(
                """
                INSERT INTO engine_health(engine, reason, expires_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(engine) DO UPDATE SET
                    reason = excluded.reason,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                WHERE excluded.expires_at > engine_health.expires_at
                """,
                (engine, reason, expires_at, now),
            )

    def active_engine_cooldowns(self, now: float | None = None) -> dict[str, tuple[str, float]]:
        """Return active waits without deleting the history needed for recovery."""
        current = time.time() if now is None else now
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT engine, reason, expires_at FROM engine_health ORDER BY expires_at DESC"
            ).fetchall()
        return {
            engine: (reason, expires_at - current)
            for engine, reason, expires_at in rows
            if expires_at > current
        }

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

    def _prune_engine_health(self) -> int:
        with self._lock, self._connection:
            return self._connection.execute(
                "DELETE FROM engine_health WHERE updated_at < ? AND expires_at <= ?",
                (time.time() - 90 * 86400, time.time()),
            ).rowcount

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
            "engine_health": self._prune_engine_health(),
            "read_snapshots": self._prune_snapshots(),
            "backend_metadata": self._prune_table(
                "backend_metadata", ttl_seconds=86400, max_rows=100
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
            "engine_health": "reason",
            "read_snapshots": "payload",
            "backend_metadata": "payload",
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
            # Historical journals remain readable, but are never created or maintained as logs.
            existing = {
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            out["legacy_tables"] = {
                table: {
                    "rows": self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                }
                for table in ("research_runs", "events")
                if table in existing
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

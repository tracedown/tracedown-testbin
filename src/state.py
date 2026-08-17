"""Keyed hit counters backing the stateful endpoints, stored in SQLite.

Why SQLite and not a dict: the API is publicly hostable — state must be
bounded on disk rather than RAM, stay consistent across multiple uvicorn
workers (one WAL-mode file, cross-process), and survive restarts. Entries
expire after STATE_TTL_SECONDS of inactivity and the table is capped at
STATE_MAX_KEYS (oldest-touched evicted first).

stdlib sqlite3 only; calls run in a worker thread behind a process-level
lock so the event loop never blocks on I/O.
"""

import asyncio
import os
import sqlite3
import threading
import time

from config import STATE_MAX_KEYS, STATE_TTL_SECONDS

DB_PATH = os.environ.get("STATE_DB_PATH", "/tmp/tracedown-testbin-state.db")

# Opportunistic maintenance cadence: sweep expired/excess rows every N writes.
SWEEP_EVERY = 100


class StateStore:
    def __init__(self, path: str = DB_PATH) -> None:
        self._lock = threading.Lock()
        self._writes = 0
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS state (
                key        TEXT PRIMARY KEY,
                count      INTEGER NOT NULL,
                last_touch REAL NOT NULL
            )"""
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_state_touch ON state(last_touch)")
        # Persistent allow/deny lists — runtime-mutable via the admin API,
        # shared across workers, restart-safe. 'kind' is allow | deny.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS ip_rules (
                cidr       TEXT NOT NULL,
                kind       TEXT NOT NULL CHECK(kind IN ('allow','deny')),
                created_at REAL NOT NULL,
                PRIMARY KEY (cidr, kind)
            )"""
        )
        self._conn.commit()

    # ── async facade (thread offload) ──

    async def incr(self, key: str) -> int:
        return await asyncio.to_thread(self._incr, key)

    async def get(self, key: str) -> int | None:
        return await asyncio.to_thread(self._get, key)

    async def delete(self, key: str) -> bool:
        return await asyncio.to_thread(self._delete, key)

    async def keys(self) -> dict[str, int]:
        return await asyncio.to_thread(self._keys)

    async def clear(self) -> int:
        return await asyncio.to_thread(self._clear)

    async def add_ip_rule(self, cidr: str, kind: str) -> bool:
        return await asyncio.to_thread(self._add_ip_rule, cidr, kind)

    async def remove_ip_rule(self, cidr: str, kind: str) -> bool:
        return await asyncio.to_thread(self._remove_ip_rule, cidr, kind)

    async def ip_rules(self) -> dict[str, list[str]]:
        return await asyncio.to_thread(self._ip_rules)

    # ── sync core ──

    def _incr(self, key: str) -> int:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                """INSERT INTO state (key, count, last_touch) VALUES (?, 1, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     count = CASE WHEN state.last_touch < ? THEN 1 ELSE state.count + 1 END,
                     last_touch = excluded.last_touch
                   RETURNING count""",
                (key, now, now - STATE_TTL_SECONDS),
            ).fetchone()
            self._writes += 1
            if self._writes % SWEEP_EVERY == 0:
                self._maintain(now)
            self._conn.commit()
            return int(row[0])

    def _get(self, key: str) -> int | None:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM state WHERE key = ? AND last_touch >= ?",
                (key, now - STATE_TTL_SECONDS),
            ).fetchone()
            return int(row[0]) if row else None

    def _delete(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM state WHERE key = ?", (key,))
            self._conn.commit()
            return cur.rowcount > 0

    def _keys(self) -> dict[str, int]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, count FROM state WHERE last_touch >= ?",
                (now - STATE_TTL_SECONDS,),
            ).fetchall()
            return {k: int(c) for k, c in rows}

    def _clear(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM state")
            self._conn.commit()
            return cur.rowcount

    def _add_ip_rule(self, cidr: str, kind: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ip_rules (cidr, kind, created_at) VALUES (?, ?, ?)",
                (cidr, kind, time.time()),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def _remove_ip_rule(self, cidr: str, kind: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM ip_rules WHERE cidr = ? AND kind = ?", (cidr, kind)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def _ip_rules(self) -> dict[str, list[str]]:
        with self._lock:
            rows = self._conn.execute("SELECT cidr, kind FROM ip_rules").fetchall()
        out: dict[str, list[str]] = {"allow": [], "deny": []}
        for cidr, kind in rows:
            out[kind].append(cidr)
        return out

    def _maintain(self, now: float) -> None:
        """Drops expired rows, then trims to the key cap (oldest first)."""
        self._conn.execute("DELETE FROM state WHERE last_touch < ?", (now - STATE_TTL_SECONDS,))
        self._conn.execute(
            """DELETE FROM state WHERE key IN (
                 SELECT key FROM state ORDER BY last_touch DESC LIMIT -1 OFFSET ?
               )""",
            (STATE_MAX_KEYS,),
        )


store = StateStore()

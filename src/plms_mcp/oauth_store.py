"""Single-owner OAuth storage; PostgreSQL never stores school credentials/cookies.

All SQL is static and parameterized. PostgreSQL uses native transactions and a
transaction-scoped advisory lock, including during first-time initialization.
One application worker is still required for the HTTP consent rate limits.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlsplit


class StateStoreError(RuntimeError):
    """A redacted storage failure, safe for logs and client error boundaries."""


# SQLite and PostgreSQL statements are intentionally explicit. Never rewrite
# arbitrary SQL or emulate SQLite's INSERT OR REPLACE/BEGIN IMMEDIATE on Postgres.
SQL = {
    "config": ("SELECT value FROM config WHERE name=?", "SELECT value FROM config WHERE name=%s"),
    "set_config": (
        "INSERT INTO config (name, value) VALUES (?, ?)",
        "INSERT INTO config (name, value) VALUES (%s, %s)",
    ),
    "state": (
        "SELECT expires, payload, family FROM state WHERE kind=? AND key=?",
        "SELECT expires, payload, family FROM state WHERE kind=%s AND key=%s",
    ),
    "put": (
        "INSERT INTO state VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(kind,key) DO UPDATE SET "
        "client_id=excluded.client_id, family=excluded.family, expires=excluded.expires, payload=excluded.payload",
        "INSERT INTO state VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT(kind,key) DO UPDATE SET "
        "client_id=excluded.client_id, family=excluded.family, expires=excluded.expires, payload=excluded.payload",
    ),
    "delete": ("DELETE FROM state WHERE kind=? AND key=?", "DELETE FROM state WHERE kind=%s AND key=%s"),
    "prune": ("DELETE FROM state WHERE expires<=?", "DELETE FROM state WHERE expires<=%s"),
    "count": ("SELECT count(*) FROM state WHERE kind=?", "SELECT count(*) FROM state WHERE kind=%s"),
    "family": (
        "SELECT family FROM state WHERE key=? AND kind IN ('access','refresh')",
        "SELECT family FROM state WHERE key=%s AND kind IN ('access','refresh')",
    ),
    "revoke": ("DELETE FROM state WHERE family=?", "DELETE FROM state WHERE family=%s"),
    "rotate": (
        "DELETE FROM state WHERE family=? AND kind IN ('access','refresh')",
        "DELETE FROM state WHERE family=%s AND kind IN ('access','refresh')",
    ),
    "latch_init": (
        "INSERT INTO login_latch (generation) VALUES (?) ON CONFLICT(generation) DO NOTHING",
        "INSERT INTO login_latch (generation) VALUES (%s) ON CONFLICT(generation) DO NOTHING",
    ),
    "latch_read": (
        "SELECT attempt FROM login_latch WHERE generation=?",
        "SELECT attempt FROM login_latch WHERE generation=%s",
    ),
    "latch_claim": (
        "UPDATE login_latch SET attempt=? WHERE generation=? AND attempt IS NULL",
        "UPDATE login_latch SET attempt=%s WHERE generation=%s AND attempt IS NULL",
    ),
    "latch_clear": (
        "UPDATE login_latch SET attempt=NULL WHERE generation=? AND attempt=?",
        "UPDATE login_latch SET attempt=NULL WHERE generation=%s AND attempt=%s",
    ),
}


class OAuthStore:
    def __init__(self, *, db_path: str | Path | None = None, database_url: str | None = None):
        self.lock = threading.RLock()
        self._in_transaction = False
        self._database_url = database_url
        self.postgres = database_url is not None
        self._connection = None
        self._driver = None
        if self.postgres:
            if urlsplit(database_url).scheme not in {"postgresql", "postgres"}:
                raise ValueError("DATABASE_URL must be a PostgreSQL URL")
            try:
                import psycopg
            except ImportError:
                raise StateStoreError("PostgreSQL requires the postgres installation extra") from None
            self._driver = psycopg
        else:
            location = db_path or os.environ.get("PLMS_OAUTH_DB") or "data/oauth.sqlite3"
            if str(location) != ":memory:":
                path = Path(location).expanduser().absolute()
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(fd)
                os.chmod(path, 0o600)
                location = str(path)
            self._connection = sqlite3.connect(str(location), isolation_level=None, check_same_thread=False)
        try:
            with self.transaction():
                payload_type = "BYTEA" if self.postgres else "BLOB"
                expiry_type = "DOUBLE PRECISION" if self.postgres else "REAL"
                for statement in (
                    "CREATE TABLE IF NOT EXISTS config (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    "CREATE TABLE IF NOT EXISTS state (kind TEXT NOT NULL, key TEXT NOT NULL, client_id TEXT, "
                    f"family TEXT, expires {expiry_type} NOT NULL, payload {payload_type} NOT NULL, "
                    "PRIMARY KEY(kind, key))",
                    "CREATE INDEX IF NOT EXISTS state_family ON state(family)",
                    "CREATE INDEX IF NOT EXISTS state_expiry ON state(expires)",
                    "CREATE TABLE IF NOT EXISTS login_latch (generation TEXT PRIMARY KEY, attempt TEXT)",
                ):
                    self._raw(statement)
        except BaseException:
            self.close()
            raise

    def _connect(self):
        if self.postgres and (self._connection is None or self._connection.closed):
            # Keyword settings override URL options. Require certificate AND
            # hostname verification; use a current libpq with system root support.
            self._connection = self._driver.connect(
                self._database_url,
                autocommit=True,
                sslmode="verify-full",
                sslrootcert=os.environ.get("PLMS_POSTGRES_SSLROOTCERT", "system"),
                connect_timeout=10,
                prepare_threshold=None,
            )
        return self._connection

    def _raw(self, statement, params=()):
        with self.lock:
            try:
                return self._connect().execute(statement, params)
            except (sqlite3.Error, self._driver.Error if self._driver else sqlite3.Error):
                # Do not replay an uncertain write/COMMIT. A later independent
                # request can reconnect; a claimed login remains fail-closed.
                if self.postgres and self._connection is not None:
                    self._connection.close()
                raise StateStoreError("Persistent authentication state is unavailable") from None

    def _query(self, name, params=()):
        with self.lock:
            if self._in_transaction:
                return self._raw(SQL[name][int(self.postgres)], params)
            # SET LOCAL is compatible with Neon transaction pooling; startup
            # options are not. Bound standalone reads/writes the same way.
            with self.transaction():
                return self._raw(SQL[name][int(self.postgres)], params)

    def transaction(self):
        # Do not use contextlib.contextmanager: MCP protocol exceptions are
        # frozen dataclasses and reject its assignment to __traceback__.
        return _Transaction(self)

    def _rollback(self):
        # Never reconnect merely to roll back an uncertain/dead transaction.
        if not self.postgres or (self._connection and not self._connection.closed):
            try:
                self._raw("ROLLBACK")
            except StateStoreError:
                pass

    def close(self):
        with self.lock:
            if self._connection is not None:
                self._connection.close()

    def config(self, name):
        row = self._query("config", (name,)).fetchone()
        return row[0] if row else None

    def set_config(self, name, value):
        self._query("set_config", (name, value))

    def state(self, kind, key):
        return self._query("state", (kind, key)).fetchone()

    def put(self, kind, key, client_id, family, expires, payload):
        self._query("put", (kind, key, client_id, family, expires, payload))

    def delete(self, kind, key):
        self._query("delete", (kind, key))

    def prune(self, now):
        self._query("prune", (now,))

    def count(self, kind):
        return self._query("count", (kind,)).fetchone()[0]

    def family(self, key):
        row = self._query("family", (key,)).fetchone()
        return row[0] if row else None

    def revoke(self, family, *, active_only=False):
        self._query("rotate" if active_only else "revoke", (family,))

    def initialize_latch(self, generation):
        with self.transaction():
            self._query("latch_init", (generation,))

    def login_disabled(self, generation):
        row = self._query("latch_read", (generation,)).fetchone()
        return row is None or row[0] is not None

    def claim_login(self, generation, attempt):
        with self.transaction():
            return self._query("latch_claim", (attempt, generation)).rowcount == 1

    def complete_login(self, generation, attempt):
        with self.transaction():
            return self._query("latch_clear", (generation, attempt)).rowcount == 1


class _Transaction:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        store = self.store
        store.lock.acquire()
        try:
            if store._in_transaction:
                raise StateStoreError("Nested authentication-state transaction is unsupported")
            store._raw("BEGIN" if store.postgres else "BEGIN IMMEDIATE")
            store._in_transaction = True
            try:
                if store.postgres:
                    store._raw("SET LOCAL statement_timeout = '10s'")
                    store._raw("SET LOCAL lock_timeout = '10s'")
                    # Transaction-level lock is safe with Neon transaction pooling.
                    store._raw("SELECT pg_advisory_xact_lock(1347177811, 1)")
            except BaseException:
                store._rollback()
                store._in_transaction = False
                raise
            return store
        except BaseException:
            store.lock.release()
            raise

    def __exit__(self, exc_type, exc, traceback):
        store = self.store
        try:
            if exc_type is None:
                try:
                    store._raw("COMMIT")
                except BaseException:
                    store._rollback()
                    raise
            else:
                store._rollback()
        finally:
            store._in_transaction = False
            store.lock.release()
        return False

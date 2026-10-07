"""Real PostgreSQL integration in a disposable, localhost-only test service.

Opt-in with PLMS_TEST_POSTGRES=1 and PLMS_TEST_DATABASE_URL. Never use a
production URL. Each test creates and removes only its own random test database.
The workflow supplies synthetic credentials and a temporary test TLS certificate.
"""

import asyncio
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

import test_cloud_login as login_cases
import test_cloud_state as state_cases

from plms_mcp.oauth_store import StateStoreError
from plms_mcp.remote_auth import PersonalOAuthProvider

ENABLED = os.environ.get("PLMS_TEST_POSTGRES") == "1"


def require_disposable_url(raw):
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.port != 5432
        or parsed.username != "postgres"
        or parsed.password != "synthetic-ci-only-password"
        or parsed.path != "/postgres"
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Integration tests require the exact disposable localhost service settings")
    return parsed


class DisposablePostgres:
    def setUp(self):
        import psycopg

        raw = os.environ.get("PLMS_TEST_DATABASE_URL", "")
        parsed = require_disposable_url(raw)
        self.admin = psycopg.connect(
            raw, autocommit=True, sslmode="verify-full",
            hostaddr="127.0.0.1",
            sslrootcert=os.environ.get("PLMS_POSTGRES_SSLROOTCERT", "system"),
            connect_timeout=10,
        )
        self.test_database = "plms_ci_" + uuid.uuid4().hex
        try:
            self.admin.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(self.test_database)))
        except BaseException:
            self.admin.close()
            raise
        self.addCleanup(self._drop_owned_test_database)
        self.database_url = urlunsplit(parsed._replace(path="/" + self.test_database, query="hostaddr=127.0.0.1"))
        parent = getattr(super(), "setUp", None)
        if parent:
            parent()

    def _drop_owned_test_database(self):
        import psycopg

        try:
            self.admin.execute(
                psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(psycopg.sql.Identifier(self.test_database))
            )
        finally:
            self.admin.close()


@unittest.skipUnless(ENABLED, "Disposable PostgreSQL integration is opt-in")
class PostgresStateTests(DisposablePostgres, state_cases.CloudStateTests):
    def test_concurrent_initialization_uses_one_salt_and_key(self):
        barrier = threading.Barrier(2)

        def create():
            barrier.wait()
            return PersonalOAuthProvider(state_cases.ORIGIN, state_cases.PASSWORD, database_url=self.database_url)

        with ThreadPoolExecutor(max_workers=2) as pool:
            one, two = pool.submit(create), pool.submit(create)
            first, second = one.result(), two.result()
        try:
            tokens = first._mint("synthetic-client", ["plms:read"])
            self.assertIsNotNone(asyncio.run(second.load_access_token(tokens.access_token)))
            self.assertEqual(first._salt, second._salt)
        finally:
            first.close()
            second.close()

    def test_dead_connection_fails_closed_then_reconnects(self):
        self.store.initialize_latch("synthetic-generation")
        self.assertTrue(self.store.claim_login("synthetic-generation", "synthetic-attempt"))
        backend = self.store._connection.info.backend_pid
        terminated = self.admin.execute("SELECT pg_terminate_backend(%s, 5000)", (backend,)).fetchone()[0]
        self.assertTrue(terminated)
        with self.assertRaises(StateStoreError):
            self.store.login_disabled("synthetic-generation")
        self.assertTrue(self.store.login_disabled("synthetic-generation"))
        self.assertFalse(self.store.claim_login("synthetic-generation", "second-attempt"))


@unittest.skipUnless(ENABLED, "Disposable PostgreSQL integration is opt-in")
class PostgresOAuthTests(DisposablePostgres, state_cases.OAuthPersistenceTests):
    pass


@unittest.skipUnless(ENABLED, "Disposable PostgreSQL integration is opt-in")
class PostgresLoginTests(DisposablePostgres, login_cases.CloudLoginTests):
    pass


class PostgresHarnessSafetyTests(unittest.TestCase):
    def test_expected_synthetic_local_service_is_accepted(self):
        self.assertEqual(require_disposable_url(
            "postgresql://postgres:synthetic-ci-only-password@localhost:5432/postgres"
        ).hostname, "localhost")

    def test_libpq_query_overrides_and_fragments_are_rejected(self):
        base = "postgresql://postgres:synthetic-ci-only-password@localhost:5432/postgres"
        for suffix in ["?host=remote.invalid", "?hostaddr=192.0.2.1", "?dbname=production", "#ignored"]:
            with self.subTest(suffix=suffix), self.assertRaises(RuntimeError):
                require_disposable_url(base + suffix)

    def test_non_test_accounts_databases_hosts_and_ports_are_rejected(self):
        base = "postgresql://postgres:synthetic-ci-only-password@localhost:5432/postgres"
        for value in [
            base.replace("localhost", "remote.invalid"), base.replace("5432", "5433"),
            base.replace("/postgres", "/production"), base.replace("postgres:", "owner:"),
            base.replace("synthetic-ci-only-password", "real-secret"),
        ]:
            with self.subTest(), self.assertRaises(RuntimeError):
                require_disposable_url(value)

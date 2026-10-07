"""No school credentials/network needed. Also runnable with stdlib unittest.

These tests do not replace the full pytest suite or a real PostgreSQL run.
"""

import asyncio
import base64
import hashlib
import os
import re
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mcp.server.auth.provider import TokenError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from plms_mcp.cloud_vault import CloudVault
from plms_mcp.oauth_store import OAuthStore, StateStoreError
from plms_mcp.remote_auth import PersonalOAuthProvider, create_auth

ORIGIN = "https://cloud-test.example"
PASSWORD = "synthetic-only-connection-password-123456"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"


def dump_store(store):
    with store.transaction():
        return repr([
            store._raw("SELECT * FROM config").fetchall(),
            store._raw("SELECT * FROM state").fetchall(),
            store._raw("SELECT * FROM login_latch").fetchall(),
        ])


class CloudStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "oauth.sqlite3"
        self.store = self.open_store()

    def open_store(self):
        return OAuthStore(db_path=self.path, database_url=getattr(self, "database_url", None))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def vault(self, store=None, generation="synthetic-generation-1"):
        return CloudVault(
            store or self.store, username="synthetic-school-user",
            password="synthetic-school-password", generation=generation,
        )

    def test_latch_survives_restart_and_never_rearms_same_generation(self):
        first = self.vault()
        self.assertTrue(first.begin_login(first.read("credentials")))
        self.store.close()
        self.store = self.open_store()
        restarted = self.vault()
        self.assertTrue(restarted.read("credentials")["disabled"])
        self.assertFalse(restarted.begin_login(restarted.read("credentials")))

    def test_only_matching_attempt_can_clear_latch(self):
        self.store.initialize_latch("synthetic-generation")
        self.assertTrue(self.store.claim_login("synthetic-generation", "synthetic-attempt"))
        self.assertFalse(self.store.complete_login("synthetic-generation", "stale-attempt"))
        self.assertTrue(self.store.login_disabled("synthetic-generation"))
        self.assertTrue(self.store.complete_login("synthetic-generation", "synthetic-attempt"))
        self.assertFalse(self.store.login_disabled("synthetic-generation"))

    def test_generation_rotation_does_not_rearm_old_deployment(self):
        old = self.vault()
        self.assertTrue(old.begin_login(old.read("credentials")))
        current = self.vault(generation="synthetic-generation-2")
        self.assertFalse(current.read("credentials")["disabled"])
        self.assertTrue(current.begin_login(current.read("credentials")))
        current.complete_login(current.read("credentials"))
        restarted_old = self.vault()
        self.assertTrue(restarted_old.read("credentials")["disabled"])

    def test_two_connections_claim_once(self):
        self.store.initialize_latch("synthetic-generation")
        second = self.open_store()
        barrier = threading.Barrier(2)

        def claim(store, attempt):
            barrier.wait()
            return store.claim_login("synthetic-generation", attempt)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(claim, self.store, "synthetic-first")
                b = pool.submit(claim, second, "synthetic-second")
                self.assertEqual(sorted([a.result(), b.result()]), [False, True])
        finally:
            second.close()

    def test_school_secrets_cookies_and_snapshots_never_enter_database(self):
        vault = self.vault()
        vault.write("cookies", [{"value": "synthetic-private-cookie"}])
        vault.write("snapshots", {"text": "synthetic-student-private-note"})
        vault.begin_login(vault.read("credentials"))
        dump = dump_store(self.store)
        for secret in [
            "synthetic-school-user", "synthetic-school-password",
            "synthetic-private-cookie", "synthetic-student-private-note",
        ]:
            self.assertNotIn(secret, dump)
        self.assertFalse(list(Path(self.temp.name).glob("*.enc")))
        self.assertIsNone(self.vault().read("cookies"))
        self.assertIsNone(self.vault().read("snapshots"))

    def test_read_returns_detached_session_records(self):
        vault = self.vault()
        vault.write("cookies", [{"value": "synthetic-cookie"}])
        returned = vault.read("cookies")
        returned[0]["value"] = "changed"
        self.assertEqual(vault.read("cookies")[0]["value"], "synthetic-cookie")

    def test_credential_write_is_not_a_latch_reset(self):
        vault = self.vault()
        vault.begin_login(vault.read("credentials"))
        with self.assertRaises(ValueError):
            vault.write("credentials", {"disabled": False})
        self.assertTrue(vault.read("credentials")["disabled"])

    def test_environment_opt_in_requires_postgres_and_complete_settings(self):
        with patch.dict(os.environ, {"PLMS_ENV_LOGIN": "1"}, clear=True):
            with self.assertRaises(ValueError):
                CloudVault.from_environment()
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused.invalid/test"}, clear=True):
            with self.assertRaises(ValueError):
                CloudVault.from_environment()

    def test_rollback_does_not_leave_partial_oauth_state(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.put("code", "hash", "client", None, time.time() + 60, b"encrypted")
                raise RuntimeError("synthetic transaction failure")
        self.assertIsNone(self.store.state("code", "hash"))

    def test_upsert_preserves_only_latest_encrypted_state(self):
        self.store.put("code", "hash", "client", None, 1, b"first")
        self.store.put("code", "hash", "client", "family", 2, b"second")
        self.assertEqual(self.store.state("code", "hash"), (2, b"second", "family"))

    def test_sqlite_permissions_and_symlink_guard(self):
        if self.store.postgres:
            self.skipTest("SQLite filesystem-specific guard")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        link = Path(self.temp.name) / "symlink.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            OAuthStore(db_path=link)


class OAuthPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "oauth.sqlite3"
        self.provider, routes = create_auth(ORIGIN, PASSWORD, db_path=self.path, database_url=getattr(self, "database_url", None))
        self.client = TestClient(Starlette(routes=routes), base_url=ORIGIN, follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.provider.close()
        self.temp.cleanup()

    def open_provider(self, password=PASSWORD):
        return PersonalOAuthProvider(ORIGIN, password, db_path=self.path, database_url=getattr(self, "database_url", None))

    def tokens(self):
        registration = self.client.post("/register", json={
            "redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "plms:read",
        }).json()
        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        redirect = self.client.get("/authorize", params={
            "client_id": registration["client_id"], "response_type": "code", "redirect_uri": CALLBACK,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "synthetic-state",
            "scope": "plms:read", "resource": ORIGIN + "/mcp",
        })
        page = self.client.get(redirect.headers["location"])
        form = {name: re.search(fr'name="{name}" value="([^"]+)"', page.text)[1] for name in ["ticket", "csrf"]}
        form.update(password=PASSWORD, decision="allow")
        approved = self.client.post("/connect", data=form, headers={"Origin": ORIGIN})
        self.assertEqual(approved.status_code, 303)
        self.assertEqual(self.client.post("/connect", data=form, headers={"Origin": ORIGIN}).status_code, 400)
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        exchange = {
            "grant_type": "authorization_code", "client_id": registration["client_id"], "code": code,
            "code_verifier": verifier, "redirect_uri": CALLBACK, "resource": ORIGIN + "/mcp",
        }
        wrong = self.client.post("/token", data={**exchange, "code_verifier": "b" * 64})
        self.assertEqual(wrong.status_code, 400)
        result = self.client.post("/token", data=exchange)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.client.post("/token", data=exchange).json()["error"], "invalid_grant")
        return registration, result.json()

    def test_pkce_rotation_replay_and_restart(self):
        registration, tokens = self.tokens()
        second = self.open_provider()
        try:
            self.assertIsNotNone(asyncio.run(second.load_access_token(tokens["access_token"])))
            rotated = self.client.post("/token", data={
                "grant_type": "refresh_token", "client_id": registration["client_id"],
                "refresh_token": tokens["refresh_token"], "resource": ORIGIN + "/mcp",
            })
            self.assertEqual(rotated.status_code, 200, rotated.text)
            new = rotated.json()
            self.assertIsNone(asyncio.run(second.load_access_token(tokens["access_token"])))
            self.assertIsNotNone(asyncio.run(second.load_access_token(new["access_token"])))
            replay = self.client.post("/token", data={
                "grant_type": "refresh_token", "client_id": registration["client_id"],
                "refresh_token": tokens["refresh_token"], "resource": ORIGIN + "/mcp",
            })
            self.assertEqual(replay.json()["error"], "invalid_grant")
            self.assertIsNone(asyncio.run(second.load_access_token(new["access_token"])))
        finally:
            second.close()

    def test_raw_tokens_and_connect_password_are_not_stored(self):
        _, tokens = self.tokens()
        dump = dump_store(self.provider._store)
        for value in [PASSWORD, tokens["access_token"], tokens["refresh_token"]]:
            self.assertNotIn(value, dump)

    def test_preloaded_refresh_replay_commits_family_revocation(self):
        client = SimpleNamespace(client_id="synthetic-client")
        tokens = self.provider._mint(client.client_id, ["plms:read"])
        first = asyncio.run(self.provider.load_refresh_token(client, tokens.refresh_token))
        second = asyncio.run(self.provider.load_refresh_token(client, tokens.refresh_token))
        rotated = asyncio.run(self.provider.exchange_refresh_token(client, first, ["plms:read"]))
        try:
            asyncio.run(self.provider.exchange_refresh_token(client, second, ["plms:read"]))
        except TokenError as error:
            self.assertEqual(error.error, "invalid_grant")
        else:
            self.fail("Reused refresh token unexpectedly succeeded")
        self.assertIsNone(asyncio.run(self.provider.load_access_token(rotated.access_token)))

    def test_wrong_client_cannot_use_replay_to_revoke_another_client(self):
        client = SimpleNamespace(client_id="synthetic-client")
        other = SimpleNamespace(client_id="synthetic-other-client")
        tokens = self.provider._mint(client.client_id, ["plms:read"])
        loaded = asyncio.run(self.provider.load_refresh_token(client, tokens.refresh_token))
        rotated = asyncio.run(self.provider.exchange_refresh_token(client, loaded, ["plms:read"]))
        try:
            asyncio.run(self.provider.exchange_refresh_token(other, loaded, ["plms:read"]))
        except TokenError:
            pass
        else:
            self.fail("Cross-client refresh unexpectedly succeeded")
        self.assertIsNotNone(asyncio.run(self.provider.load_access_token(rotated.access_token)))

    def test_wrong_connect_password_does_not_reset_oauth_database(self):
        _, tokens = self.tokens()
        with self.assertRaises(ValueError):
            self.open_provider("different-synthetic-connect-password")
        self.assertIsNotNone(asyncio.run(self.provider.load_access_token(tokens["access_token"])))

    def test_two_providers_exchange_same_code_only_once(self):
        client = SimpleNamespace(client_id="synthetic-client")
        code = SimpleNamespace(code="synthetic-code")
        self.provider._put("code", code.code, {"client_id": client.client_id, "scopes": ["plms:read"]}, time.time() + 60)
        second = self.open_provider()
        barrier = threading.Barrier(2)

        def exchange(provider):
            barrier.wait()
            try:
                asyncio.run(provider.exchange_authorization_code(client, code))
                return True
            except TokenError:
                return False

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(exchange, self.provider)
                b = pool.submit(exchange, second)
                self.assertEqual(sorted([a.result(), b.result()]), [False, True])
        finally:
            second.close()


class FakeDriverError(Exception):
    pass


class FakeConnection:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.fail_on = None

    def execute(self, query, params=()):
        self.calls.append((query, params))
        if query == self.fail_on:
            raise FakeDriverError("synthetic-database-password-must-not-leak")
        return SimpleNamespace(rowcount=1, fetchone=lambda: None)

    def close(self):
        self.closed = True


class PostgresDriverContractTests(unittest.TestCase):
    def setUp(self):
        # Keep the default-root contract independent of integration-test TLS.
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("PLMS_POSTGRES_SSLROOTCERT", None)
        self.connections = []
        self.kwargs = []

        def connect(*args, **kwargs):
            connection = FakeConnection()
            self.connections.append(connection)
            self.kwargs.append(kwargs)
            return connection

        self.driver = SimpleNamespace(connect=connect, Error=FakeDriverError)
        self.module_patch = patch.dict("sys.modules", {"psycopg": self.driver})
        self.module_patch.start()
        self.store = OAuthStore(database_url="postgresql://synthetic.invalid/test?sslmode=disable")

    def tearDown(self):
        self.store.close()
        self.module_patch.stop()

    def test_native_sql_tls_timeouts_and_transaction_lock(self):
        self.store.put("code", "hash", "client", None, 1, b"ciphertext")
        self.assertEqual(self.kwargs[0]["sslmode"], "verify-full")
        self.assertEqual(self.kwargs[0]["sslrootcert"], "system")
        self.assertEqual(self.kwargs[0]["connect_timeout"], 10)
        self.assertNotIn("options", self.kwargs[0])
        statements = [query for query, _ in self.connections[0].calls]
        self.assertTrue(any("BYTEA" in query for query in statements))
        self.assertIn("SET LOCAL statement_timeout = '10s'", statements)
        self.assertIn("SELECT pg_advisory_xact_lock(1347177811, 1)", statements)
        self.assertFalse(any("BEGIN IMMEDIATE" in query or "INSERT OR REPLACE" in query for query in statements))
        upsert = next(query for query in statements if query.startswith("INSERT INTO state"))
        self.assertIn("%s", upsert)
        self.assertNotIn("?", upsert)

    def test_explicit_ca_preserves_hostname_verification(self):
        with patch.dict(os.environ, {"PLMS_POSTGRES_SSLROOTCERT": "/synthetic/test-ca.crt"}):
            store = OAuthStore(database_url="postgresql://synthetic.invalid/test?sslmode=disable")
        try:
            self.assertEqual(self.kwargs[-1]["sslrootcert"], "/synthetic/test-ca.crt")
            self.assertEqual(self.kwargs[-1]["sslmode"], "verify-full")
        finally:
            store.close()

    def test_failed_write_is_redacted_never_replayed_and_next_request_reconnects(self):
        first = self.connections[0]
        first.fail_on = "UPDATE login_latch SET attempt=%s WHERE generation=%s AND attempt IS NULL"
        with self.assertRaises(StateStoreError) as error:
            self.store.claim_login("synthetic-generation", "synthetic-attempt")
        self.assertNotIn("synthetic-database-password", str(error.exception))
        self.assertTrue(first.closed)
        self.assertEqual(sum(query == first.fail_on for query, _ in first.calls), 1)
        self.assertEqual(len(self.connections), 1)
        self.store.login_disabled("synthetic-generation")
        self.assertEqual(len(self.connections), 2)

    def test_uncertain_commit_never_replayed(self):
        first = self.connections[0]
        first.fail_on = "COMMIT"
        first.calls.clear()
        with self.assertRaises(StateStoreError):
            self.store.claim_login("synthetic-generation", "synthetic-attempt")
        self.assertEqual(sum(query == "COMMIT" for query, _ in first.calls), 1)
        self.assertEqual(len(self.connections), 1)
        self.assertFalse(any(query == "ROLLBACK" for query, _ in first.calls))


if __name__ == "__main__":
    unittest.main()

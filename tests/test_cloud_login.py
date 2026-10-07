"""PLMSClient + CloudVault behavior with a synthetic authenticator, no network.

Load an isolated client module so the school authentication/parser dependencies
are mocked without altering imports used by the rest of the test suite.
"""

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import httpx

from plms_mcp.cloud_vault import CloudVault
from plms_mcp.errors import AuthenticationRequired
from plms_mcp.oauth_store import OAuthStore, StateStoreError


def client_module():
    fake = ModuleType("plms_mcp.auth")
    fake.BASE = "https://plms.postech.ac.kr"

    def export(client):
        return [
            {"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path}
            for cookie in client.cookies.jar
        ]

    def import_cookies(client, cookies):
        for cookie in cookies:
            client.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])

    async def request(*args, **kwargs):
        return httpx.Response(200, text="synthetic-authenticated-page")

    async def login(*args, **kwargs):
        raise AssertionError("Test must install its synthetic authenticator")

    fake.export_cookies = export
    fake.import_cookies = import_cookies
    fake.logged_in = lambda html: html == "synthetic-authenticated-page"
    fake.auth_request = request
    fake.login_sso = login
    fake.login_direct = login
    source = Path(__file__).parents[1] / "src/plms_mcp/client.py"
    spec = importlib.util.spec_from_file_location("plms_mcp._isolated_cloud_test_client", source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"plms_mcp.auth": fake}):
        spec.loader.exec_module(module)
    return module


class CloudLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "oauth.sqlite3"
        self.store = self.open_store()
        self.vault = CloudVault(
            self.store, username="synthetic-user", password="synthetic-password", generation="generation-1"
        )
        self.module = client_module()
        self.client = self.module.PLMSClient(
            self.vault, transport=httpx.MockTransport(lambda request: httpx.Response(500))
        )
        self.calls = 0

    def open_store(self):
        return OAuthStore(db_path=self.path, database_url=getattr(self, "database_url", None))

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        self.temp.cleanup()

    async def success(self, http, username, password):
        self.calls += 1
        await asyncio.sleep(0)
        http.cookies.set("MoodleSession", "same-synthetic-cookie", domain="plms.postech.ac.kr", path="/")

    async def test_simultaneous_same_cookie_success_performs_one_login(self):
        self.module.login_sso = self.success
        self.client.http.cookies.set("MoodleSession", "same-synthetic-cookie", domain="plms.postech.ac.kr", path="/")
        self.client._save_cookies()
        cookies, generation = self.client._saved_cookies, self.client._login_generation
        results = await asyncio.gather(*[
            self.client.refresh_session(expected_cookies=cookies, expected_generation=generation)
            for _ in range(2)
        ])
        self.assertEqual(results, [True, True])
        self.assertEqual(self.calls, 1)
        self.assertFalse(self.store.login_disabled("generation-1"))

    async def test_failed_login_is_redacted_and_not_repeated_after_restart(self):
        async def failure(*args):
            self.calls += 1
            raise RuntimeError("synthetic-password")

        self.module.login_sso = failure
        with self.assertRaises(AuthenticationRequired) as error:
            await self.client.refresh_session()
        self.assertNotIn("synthetic-password", str(error.exception))
        self.assertFalse(await self.client.refresh_session())
        restarted_store = self.open_store()
        try:
            restarted = CloudVault(restarted_store, username="synthetic-user", password="synthetic-password", generation="generation-1")
            self.assertTrue(restarted.read("credentials")["disabled"])
        finally:
            restarted_store.close()
        self.assertEqual(self.calls, 1)

    async def test_cancellation_leaves_durable_claim(self):
        started = asyncio.Event()

        async def hanging(*args):
            self.calls += 1
            started.set()
            await asyncio.Event().wait()

        self.module.login_sso = hanging
        task = asyncio.create_task(self.client.refresh_session())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.store.login_disabled("generation-1"))
        self.assertFalse(await self.client.refresh_session())
        self.assertEqual(self.calls, 1)

    async def test_missing_cookie_does_not_clear_claim(self):
        async def false_success(*args):
            self.calls += 1

        self.module.login_sso = false_success
        with self.assertRaises(AuthenticationRequired):
            await self.client.refresh_session()
        self.assertTrue(self.store.login_disabled("generation-1"))

    async def test_failed_session_verification_does_not_clear_claim(self):
        self.module.login_sso = self.success

        async def unauthenticated(*args):
            return httpx.Response(200, text="synthetic-login-page")

        self.module.auth_request = unauthenticated
        with self.assertRaises(AuthenticationRequired):
            await self.client.refresh_session()
        self.assertTrue(self.store.login_disabled("generation-1"))
        self.assertIsNone(self.vault.read("cookies"))

    async def test_database_claim_failure_never_calls_school_login(self):
        self.module.login_sso = self.success
        with patch.object(self.store, "claim_login", side_effect=StateStoreError("Persistent state unavailable")):
            with self.assertRaises(StateStoreError):
                await self.client.refresh_session()
        self.assertEqual(self.calls, 0)

    async def test_uncertain_completion_retains_claim(self):
        self.module.login_sso = self.success
        with patch.object(self.store, "complete_login", side_effect=StateStoreError("Persistent state unavailable")):
            with self.assertRaises(StateStoreError):
                await self.client.refresh_session()
        self.assertTrue(self.store.login_disabled("generation-1"))
        self.assertFalse(await self.client.refresh_session())
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()

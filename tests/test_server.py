"""Run the actual protected MCP application across multiple stateless requests."""

import httpx
from starlette.testclient import TestClient
from test_remote_auth import ORIGIN, PASSWORD, authorize, exchange, register

import plms_mcp.server as server_module
from plms_mcp.client import PLMSClient
from plms_mcp.storage import Vault


def test_stateless_requests_keep_upstream_client_alive_until_app_shutdown(tmp_path, monkeypatch):
    def respond(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="""
        <html><a href="/login/logout.php">Logout</a><main>
        <a href="/course/view.php?id=100">Example course</a></main></html>""",
        )

    vault = Vault(tmp_path / "private")
    upstream = PLMSClient(vault, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(server_module, "PLMSClient", lambda _: upstream)
    app = server_module.create_http_app(vault, ORIGIN, PASSWORD)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        registration = register(client).json()
        tokens = exchange(client, registration, authorize(client, registration)).json()
        headers = {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-06-18",
            "Authorization": "Bearer " + tokens["access_token"],
        }
        for i in range(3):
            payload = {
                "jsonrpc": "2.0",
                "id": i + 1,
                "method": "tools/call",
                "params": {"name": "plms_status", "arguments": {}},
            }
            response = client.post("/mcp", json=payload, headers=headers)
            assert response.status_code == 200, response.text
            result = response.json()["result"]
            assert not result.get("isError"), result
            assert result["structuredContent"]["data"]["authenticated"]
            assert not upstream._closed
    assert upstream._closed

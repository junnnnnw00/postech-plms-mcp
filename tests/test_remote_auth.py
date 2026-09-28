"""Exercise real SDK OAuth handlers, rather than mocking PKCE/authentication."""

import asyncio
import base64
import hashlib
import re
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.testclient import TestClient

from plms_mcp.remote_auth import ACCESS_TTL, PersonalOAuthProvider, create_auth

ORIGIN = "https://plms.example.test"
PASSWORD = "test-connect-secret-with-40-random-characters"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def auth(tmp_path):
    provider, routes = create_auth(ORIGIN, PASSWORD, db_path=tmp_path / "oauth.sqlite3")
    with TestClient(Starlette(routes=routes), base_url=ORIGIN, follow_redirects=False) as client:
        yield provider, client
    provider.close()


def register(client, **overrides):
    metadata = {
        "client_name": "Test desktop",
        "redirect_uris": [CALLBACK],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": "plms:read",
    }
    metadata.update(overrides)
    return client.post("/register", json=metadata)


def start(client, registration, **overrides):
    params = {
        "client_id": registration["client_id"],
        "response_type": "code",
        "redirect_uri": CALLBACK,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "opaque-app-state",
        "scope": "plms:read",
        "resource": ORIGIN + "/mcp",
    }
    params.update(overrides)
    return client.get("/authorize", params=params)


def consent_form(client, registration):
    redirect = start(client, registration)
    assert redirect.status_code == 302
    assert urlsplit(redirect.headers["location"]).path == "/connect"
    page = client.get(redirect.headers["location"])
    assert page.status_code == 200
    ticket = re.search(r'name="ticket" value="([^"]+)"', page.text)[1]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    return page, {"ticket": ticket, "csrf": csrf, "password": PASSWORD, "decision": "allow"}


def authorize(client, registration):
    _, form = consent_form(client, registration)
    approved = client.post("/connect", data=form, headers={"Origin": ORIGIN})
    assert approved.status_code == 303, approved.text
    redirect = parse_qs(urlsplit(approved.headers["location"]).query)
    assert redirect["state"] == ["opaque-app-state"]
    return redirect["code"][0]


def exchange(client, registration, code, **overrides):
    form = {
        "grant_type": "authorization_code",
        "client_id": registration["client_id"],
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": CALLBACK,
        "resource": ORIGIN + "/mcp",
    }
    form.update(overrides)
    if registration.get("client_secret"):
        form["client_secret"] = registration["client_secret"]
    return client.post("/token", data=form)


def refresh(client, registration, token, **overrides):
    data = {
        "grant_type": "refresh_token",
        "client_id": registration["client_id"],
        "refresh_token": token,
        "resource": ORIGIN + "/mcp",
    }
    data.update(overrides)
    return client.post("/token", data=data)


def test_complete_pkce_flow_and_single_use_code(auth):
    provider, client = auth
    registration = register(client).json()
    code = authorize(client, registration)
    result = exchange(client, registration, code)
    assert result.status_code == 200, result.text
    tokens = result.json()
    assert tokens["expires_in"] == ACCESS_TTL
    info = asyncio.run(provider.load_access_token(tokens["access_token"]))
    assert info.resource == ORIGIN + "/mcp"
    assert info.subject == "instance-owner"
    assert info.scopes == ["plms:read"]
    assert exchange(client, registration, code).json()["error"] == "invalid_grant"


def test_pkce_required_and_verifier_must_match(auth):
    _, client = auth
    registration = register(client).json()
    missing_pkce = start(client, registration, code_challenge=None)
    assert "code=" not in missing_pkce.headers.get("location", "")
    plain = start(client, registration, code_challenge_method="plain")
    assert "error=" in plain.headers.get("location", "")
    code = authorize(client, registration)
    wrong = exchange(client, registration, code, code_verifier="b" * 64)
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_grant"
    assert exchange(client, registration, code).status_code == 200


def test_cross_client_code_and_redirect_substitution_rejected(auth):
    _, client = auth
    first, second = register(client).json(), register(client).json()
    code = authorize(client, first)
    assert exchange(client, second, code).json()["error"] == "invalid_grant"
    assert (
        exchange(client, first, code, redirect_uri="https://evil.test/callback").json()["error"]
        == "invalid_request"
    )
    assert exchange(client, first, code).status_code == 200
    invalid = start(client, first, redirect_uri="https://evil.test/callback")
    assert invalid.status_code == 400
    assert "location" not in invalid.headers


def test_resource_mismatch_at_authorize_token_and_refresh(auth):
    provider, client = auth
    registration = register(client).json()
    result = start(client, registration, resource="https://other.test/mcp")
    assert "error=invalid_request" in result.headers["location"]
    code = authorize(client, registration)
    wrong = exchange(client, registration, code, resource="https://other.test/mcp")
    assert wrong.status_code == 400
    assert wrong.json()["error"] == "invalid_target"
    tokens = exchange(client, registration, code).json()
    assert (
        refresh(client, registration, tokens["refresh_token"], resource="https://other.test/mcp").json()[
            "error"
        ]
        == "invalid_target"
    )
    # Even corrupt/misconfigured stored state cannot authenticate the wrong audience.
    data = provider._get("access", tokens["access_token"])
    data["resource"] = "https://other.test/mcp"
    provider._put("access", tokens["access_token"], data, time.time() + 60)
    assert asyncio.run(provider.load_access_token(tokens["access_token"])) is None


def test_csrf_session_origin_and_password_required(auth):
    _, client = auth
    registration = register(client).json()
    page, form = consent_form(client, registration)
    assert "HttpOnly" in page.headers["set-cookie"]
    assert "Secure" in page.headers["set-cookie"]
    assert "SameSite=lax" in page.headers["set-cookie"]
    assert page.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.post("/connect", data=form).status_code == 403
    assert client.post("/connect", data=form, headers={"Origin": "null"}).status_code == 403
    assert client.post("/connect", data=form, headers={"Origin": "https://evil.test"}).status_code == 403
    assert (
        client.post("/connect", data={**form, "csrf": "wrong"}, headers={"Origin": ORIGIN}).status_code == 403
    )
    saved = client.cookies.get("__Host-plms-consent")
    client.cookies.clear()
    assert client.post("/connect", data=form, headers={"Origin": ORIGIN}).status_code == 403
    client.cookies.set("__Host-plms-consent", saved)
    assert (
        client.post("/connect", data={**form, "password": "wrong"}, headers={"Origin": ORIGIN}).status_code
        == 401
    )
    assert client.post("/connect", data=form, headers={"Origin": ORIGIN}).status_code == 303
    assert client.post("/connect", data=form, headers={"Origin": ORIGIN}).status_code == 400


def test_native_form_headers_preserve_origin_and_allow_only_selected_callback(auth):
    _, client = auth
    selected = "https://selected.example/oauth/callback"
    registration = register(client, redirect_uris=[CALLBACK, selected]).json()
    redirect = start(client, registration, redirect_uri=selected)
    page = client.get(redirect.headers["location"])
    # Browser-native POST navigation nulls Origin under no-referrer. Keep
    # same-origin here while suppressing all referrer data on the final redirect.
    assert page.headers["referrer-policy"] == "same-origin"
    csp = page.headers["content-security-policy"]
    assert "form-action 'self' https://selected.example;" in csp
    assert "chatgpt.com" not in csp
    assert "https:;" not in csp
    ticket = re.search(r'name="ticket" value="([^"]+)"', page.text)[1]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    response = client.post(
        "/connect",
        data={"ticket": ticket, "csrf": csrf, "password": PASSWORD, "decision": "allow"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 303
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["location"].startswith(selected + "?")


@pytest.mark.parametrize("uri", ["https://*.example/callback", "https://example.com;script-src/callback"])
def test_callback_cannot_inject_or_widen_csp(auth, uri):
    _, client = auth
    assert register(client, redirect_uris=[uri]).status_code == 400


def test_denial_returns_state_without_code(auth):
    _, client = auth
    registration = register(client).json()
    _, form = consent_form(client, registration)
    result = client.post(
        "/connect", data={**form, "decision": "deny", "password": ""}, headers={"Origin": ORIGIN}
    )
    query = parse_qs(urlsplit(result.headers["location"]).query)
    assert query == {"error": ["access_denied"], "state": ["opaque-app-state"]}


def test_refresh_rotation_reuse_detection_and_revocation(auth):
    provider, client = auth
    registration = register(client).json()
    initial = exchange(client, registration, authorize(client, registration)).json()
    other = register(client).json()
    assert refresh(client, other, initial["refresh_token"]).json()["error"] == "invalid_grant"
    rotated_response = refresh(client, registration, initial["refresh_token"])
    assert rotated_response.status_code == 200
    rotated = rotated_response.json()
    assert initial["access_token"] != rotated["access_token"]
    assert initial["refresh_token"] != rotated["refresh_token"]
    assert asyncio.run(provider.load_access_token(initial["access_token"])) is None
    assert asyncio.run(provider.load_access_token(rotated["access_token"])) is not None
    assert refresh(client, registration, initial["refresh_token"]).json()["error"] == "invalid_grant"
    assert asyncio.run(provider.load_access_token(rotated["access_token"])) is None
    assert refresh(client, registration, rotated["refresh_token"]).json()["error"] == "invalid_grant"
    fresh = exchange(client, registration, authorize(client, registration)).json()
    revoked = client.post(
        "/revoke", data={"client_id": registration["client_id"], "token": fresh["access_token"]}
    )
    assert revoked.status_code == 200
    assert asyncio.run(provider.load_access_token(fresh["access_token"])) is None
    assert refresh(client, registration, fresh["refresh_token"]).json()["error"] == "invalid_grant"


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com/callback",
        "https://user:pass@example.com/callback",
        "https://example.com/callback#fragment",
        "file:///callback",
        "javascript:alert(1)",
    ],
)
def test_reject_unsafe_redirects(auth, uri):
    _, client = auth
    assert register(client, redirect_uris=[uri]).status_code == 400


@pytest.mark.parametrize(
    "uri",
    [
        "https://client.example/callback",
        "http://127.0.0.1:8673/callback",
        "http://[::1]:8765/callback",
        "http://localhost:8080/callback",
    ],
)
def test_accept_https_and_loopback_redirects(auth, uri):
    _, client = auth
    assert register(client, redirect_uris=[uri]).status_code == 201


def test_confidential_client_requires_secret(auth):
    _, client = auth
    registration = register(client, token_endpoint_auth_method="client_secret_post").json()
    code = authorize(client, registration)
    public = {**registration, "client_secret": None}
    assert exchange(client, public, code).status_code == 401
    assert exchange(client, registration, code).status_code == 200


def test_expired_code_refresh_and_access_rejected(auth):
    provider, client = auth
    registration = register(client).json()
    code = authorize(client, registration)
    provider._db.execute("UPDATE state SET expires=0 WHERE kind='code'")
    assert exchange(client, registration, code).json()["error"] == "invalid_grant"
    tokens = exchange(client, registration, authorize(client, registration)).json()
    provider._db.execute("UPDATE state SET expires=0 WHERE kind IN ('access','refresh')")
    assert asyncio.run(provider.load_access_token(tokens["access_token"])) is None
    assert refresh(client, registration, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_login_bruteforce_is_rate_limited(auth):
    _, client = auth
    registration = register(client).json()
    _, form = consent_form(client, registration)
    for _ in range(10):
        assert (
            client.post(
                "/connect", data={**form, "password": "wrong"}, headers={"Origin": ORIGIN}
            ).status_code
            == 401
        )
    assert client.post("/connect", data=form, headers={"Origin": ORIGIN}).status_code == 429


def test_oversized_request_rejected(auth):
    _, client = auth
    assert client.post("/register", content="x" * 17000).status_code == 413
    assert client.get("/authorize?" + "x" * 17000).status_code == 413


def test_encrypted_persistence_across_restart(tmp_path):
    path = tmp_path / "oauth.sqlite3"
    provider, routes = create_auth(ORIGIN, PASSWORD, db_path=path)
    with TestClient(Starlette(routes=routes), base_url=ORIGIN, follow_redirects=False) as client:
        registration = register(client).json()
        tokens = exchange(client, registration, authorize(client, registration)).json()
    provider.close()
    content = path.read_bytes()
    for secret in [PASSWORD, tokens["access_token"], tokens["refresh_token"], CALLBACK]:
        assert secret.encode() not in content
    assert path.stat().st_mode & 0o077 == 0
    restored = PersonalOAuthProvider(ORIGIN, PASSWORD, db_path=path)
    assert asyncio.run(restored.get_client(registration["client_id"])) is not None
    assert asyncio.run(restored.load_access_token(tokens["access_token"])) is not None
    restored.close()
    with pytest.raises(ValueError, match="password mismatch"):
        PersonalOAuthProvider(ORIGIN, PASSWORD + "different", db_path=path)


def test_fastmcp_protected_resource_integration(tmp_path):
    provider, routes = create_auth(ORIGIN, PASSWORD, db_path=tmp_path / "oauth.sqlite3")
    server = FastMCP(
        "Auth integration",
        token_verifier=provider,
        auth=provider.auth_settings,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=[urlsplit(ORIGIN).netloc], allowed_origins=[ORIGIN]
        ),
    )

    @server.tool()
    def hello() -> str:
        return "authenticated"

    app = server.streamable_http_app()
    app.router.routes[0:0] = routes
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        resource_metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert resource_metadata.status_code == 200
        assert resource_metadata.json()["resource"] == ORIGIN + "/mcp"
        assert client.get("/.well-known/oauth-authorization-server").status_code == 200
        registration = register(client).json()
        tokens = exchange(client, registration, authorize(client, registration)).json()
        headers = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert client.post("/mcp", json=request, headers=headers).status_code == 401
        accepted = client.post(
            "/mcp", json=request, headers={**headers, "Authorization": "Bearer " + tokens["access_token"]}
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["result"]["tools"][0]["name"] == "hello"
        assert (
            client.post(
                "/mcp", json=request, headers={**headers, "Authorization": "Bearer invalid"}
            ).status_code
            == 401
        )
    provider.close()

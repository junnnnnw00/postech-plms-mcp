"""Single-owner OAuth for a personal PLMS MCP deployment.

Each student runs a separate instance. PLMS credentials never enter this OAuth
flow: the owner supplies a separate, strong PLMS_CONNECT_PASSWORD. SQLite stores
encrypted OAuth state, indexed by hashes of opaque credentials. Keep its file
across deployments; changing the connect password requires deleting that file
and reconnecting clients. Run one ASGI worker (rate limits are process-local).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

SCOPE = "plms:read"
CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
CLIENT_TTL = 90 * 24 * 3600
MAX_BODY = 16_384


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _random() -> str:
    return secrets.token_urlsafe(32)


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}"


def _callback_origin(value: str) -> str:
    """A single CSP host source, never a wildcard or injected directive."""
    origin = _origin(value)
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-:\[\]]+", origin):
        raise ValueError("Callback origin is not a safe CSP source")
    return origin


def _loopback(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


class PersonalOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    """Persistent, revocable OAuth grants for one instance owner."""

    def __init__(self, issuer_url: str, connect_password: str, *, db_path: str | Path | None = None):
        issuer = str(AnyHttpUrl(issuer_url)).rstrip("/")
        parts = urlsplit(issuer)
        if parts.query or parts.fragment or parts.username or parts.password or parts.path:
            raise ValueError(
                "PLMS_PUBLIC_URL must be an origin without a path, credentials, query or fragment"
            )
        if parts.scheme != "https" and not (parts.scheme == "http" and _loopback(parts.hostname)):
            raise ValueError("Remote OAuth requires HTTPS (HTTP is allowed only on loopback for development)")
        if len(connect_password) < 20:
            raise ValueError("PLMS_CONNECT_PASSWORD must contain at least 20 characters; use a random secret")
        self.issuer_url = issuer
        self.resource_url = issuer + "/mcp"
        self.origin = _origin(issuer)
        self.secure_cookie = parts.scheme == "https"
        self.cookie_name = "__Host-plms-consent" if self.secure_cookie else "plms-consent-dev"
        self.registration_options = ClientRegistrationOptions(
            enabled=True,
            valid_scopes=[SCOPE],
            default_scopes=[SCOPE],
            client_secret_expiry_seconds=CLIENT_TTL,
        )
        self.revocation_options = RevocationOptions(enabled=True)
        self.auth_settings = AuthSettings(
            issuer_url=AnyHttpUrl(issuer),
            resource_server_url=AnyHttpUrl(self.resource_url),
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=self.registration_options,
            revocation_options=self.revocation_options,
        )
        location = db_path or os.environ.get("PLMS_OAUTH_DB") or "data/oauth.sqlite3"
        if str(location) != ":memory:":
            # Preserve the final path component so O_NOFOLLOW can actually reject
            # an existing database symlink instead of opening its resolved target.
            path = Path(location).expanduser().absolute()
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Create atomically with private mode; never silently follow symlinks.
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            os.chmod(path, 0o600)
            location = str(path)
        self._db = sqlite3.connect(str(location), isolation_level=None, check_same_thread=False)
        self._lock = threading.RLock()
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS config (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state (
                kind TEXT NOT NULL, key TEXT NOT NULL, client_id TEXT,
                family TEXT, expires REAL NOT NULL, payload BLOB NOT NULL,
                PRIMARY KEY(kind, key)
            );
            CREATE INDEX IF NOT EXISTS state_family ON state(family);
            CREATE INDEX IF NOT EXISTS state_expiry ON state(expires);
        """)
        row = self._db.execute("SELECT value FROM config WHERE name='salt'").fetchone()
        self._salt = base64.b64decode(row[0]) if row else secrets.token_bytes(16)
        derived = self._derive(connect_password)
        self._password_hash = derived[:32]
        self._cipher = Fernet(base64.urlsafe_b64encode(derived[32:]))
        if row:
            check = self._db.execute("SELECT value FROM config WHERE name='check'").fetchone()
            try:
                valid = check and self._cipher.decrypt(check[0].encode()) == b"PLMS OAuth storage v1"
            except InvalidToken:
                valid = False
            if not valid:
                self._db.close()
                raise ValueError(
                    "OAuth database password mismatch; restore the connect password or reset OAuth state"
                )
        else:
            with self._lock:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._db.execute(
                        "INSERT INTO config VALUES ('salt', ?)", (base64.b64encode(self._salt).decode(),)
                    )
                    self._db.execute(
                        "INSERT INTO config VALUES ('check', ?)",
                        (self._cipher.encrypt(b"PLMS OAuth storage v1").decode(),),
                    )
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        # The rate limit bounds attempts over time, but concurrent scrypt calls
        # otherwise each allocate ~16 MiB and can exhaust small personal servers.
        # Use a thread semaphore because cancellation of an HTTP coroutine does
        # not stop an already running KDF in the executor.
        self._password_slots = threading.BoundedSemaphore(2)

    def close(self) -> None:
        self._db.close()

    def _derive(self, password: str) -> bytes:
        return hashlib.scrypt(password.encode(), salt=self._salt, n=2**14, r=8, p=1, dklen=64)

    def _derive_bounded(self, password: str) -> bytes:
        with self._password_slots:
            return self._derive(password)

    async def _password_matches(self, password: str) -> bool:
        if len(password) > 1024:
            return False
        candidate = await asyncio.to_thread(self._derive_bounded, password)
        return hmac.compare_digest(candidate[:32], self._password_hash)

    def _limited(self, key: str, limit: int, window: int) -> bool:
        now = time.monotonic()
        if key not in self._attempts and len(self._attempts) >= 4096:
            for old_key in list(self._attempts):
                if not self._attempts[old_key] or self._attempts[old_key][-1] <= now - 3600:
                    del self._attempts[old_key]
            if len(self._attempts) >= 4096:
                return True
        queue = self._attempts[key]
        while queue and queue[0] <= now - window:
            queue.popleft()
        if len(queue) >= limit:
            return True
        queue.append(now)
        return False

    def _put(
        self,
        kind: str,
        raw_key: str,
        data: dict[str, Any],
        expires: float,
        client_id: str | None = None,
        family: str | None = None,
    ) -> None:
        encrypted = self._cipher.encrypt(json.dumps(data, separators=(",", ":")).encode())
        self._db.execute(
            "INSERT OR REPLACE INTO state VALUES (?, ?, ?, ?, ?, ?)",
            (kind, _digest(raw_key), client_id, family, expires, encrypted),
        )

    def _get(self, kind: str, raw_key: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT expires, payload FROM state WHERE kind=? AND key=?", (kind, _digest(raw_key))
        ).fetchone()
        if not row or row[0] <= time.time():
            return None
        try:
            return json.loads(self._cipher.decrypt(row[1]))
        except (InvalidToken, ValueError):
            return None

    def _delete(self, kind: str, raw_key: str) -> None:
        self._db.execute("DELETE FROM state WHERE kind=? AND key=?", (kind, _digest(raw_key)))

    def _prune(self) -> None:
        self._db.execute("DELETE FROM state WHERE expires<=?", (time.time(),))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id or not client_info.redirect_uris or len(client_info.redirect_uris) > 8:
            raise RegistrationError("invalid_client_metadata", "One to eight redirect URIs are required")
        if client_info.client_name and len(client_info.client_name) > 200:
            raise RegistrationError("invalid_client_metadata", "Client name is too long")
        for redirect in client_info.redirect_uris:
            value = str(redirect)
            uri = urlsplit(value)
            try:
                _callback_origin(value)
            except ValueError:
                raise RegistrationError(
                    "invalid_redirect_uri", "Callback must have a valid HTTPS or loopback origin"
                ) from None
            allowed = uri.scheme == "https" or (uri.scheme == "http" and _loopback(uri.hostname))
            if (
                not allowed
                or not uri.hostname
                or uri.username
                or uri.password
                or uri.fragment
                or len(value) > 2048
            ):
                raise RegistrationError(
                    "invalid_redirect_uri", "Use HTTPS callbacks or an HTTP loopback callback"
                )
        if set((client_info.scope or SCOPE).split()) != {SCOPE}:
            raise RegistrationError("invalid_client_metadata", "Only plms:read is supported")
        with self._lock:
            self._prune()
            if self._db.execute("SELECT count(*) FROM state WHERE kind='client'").fetchone()[0] >= 1000:
                raise RegistrationError("invalid_client_metadata", "Client registration capacity reached")
            self._put(
                "client",
                client_info.client_id,
                client_info.model_dump(mode="json"),
                time.time() + CLIENT_TTL,
                client_info.client_id,
            )

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource != self.resource_url:
            raise AuthorizeError("invalid_request", "Resource must match this PLMS MCP server")
        if set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_scope", "Only plms:read is supported")
        ticket = _random()
        with self._lock:
            self._prune()
            if self._db.execute("SELECT count(*) FROM state WHERE kind='pending'").fetchone()[0] >= 200:
                raise AuthorizeError("temporarily_unavailable", "Too many pending connections")
            self._put(
                "pending",
                ticket,
                {"client_id": client.client_id, "params": params.model_dump(mode="json")},
                time.time() + CODE_TTL,
                client.client_id,
            )
        return self.issuer_url + "/connect?" + urlencode({"ticket": ticket})

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self._get("code", authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **data)

    def _mint(self, client_id: str, scopes: list[str], *, family: str | None = None) -> OAuthToken:
        # Established clients can refresh forever without registering/authorizing
        # again. Expired replay markers must also be pruned on that path.
        self._prune()
        family = family or _random()
        access, refresh = _random(), _random()
        now = int(time.time())
        common = {
            "client_id": client_id,
            "scopes": scopes,
            "resource": self.resource_url,
            "subject": "instance-owner",
        }
        self._put(
            "access", access, {**common, "expires_at": now + ACCESS_TTL}, now + ACCESS_TTL, client_id, family
        )
        self._put(
            "refresh",
            refresh,
            {**common, "expires_at": now + REFRESH_TTL},
            now + REFRESH_TTL,
            client_id,
            family,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                data = self._get("code", authorization_code.code)
                if not data or data["client_id"] != client.client_id:
                    raise TokenError("invalid_grant", "Authorization code is invalid or already used")
                self._delete("code", authorization_code.code)
                result = self._mint(data["client_id"], data["scopes"])
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._get("refresh", refresh_token)
        if not data or data["client_id"] != client.client_id:
            # Reuse of a rotated credential indicates theft: invalidate its entire
            # family, but never let a different client revoke another client's grant.
            spent = self._get("spent_refresh", refresh_token)
            if spent and spent["client_id"] == client.client_id:
                self._db.execute("DELETE FROM state WHERE family=?", (spent["family"],))
            return None
        return RefreshToken(token=refresh_token, **data)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                data = self._get("refresh", refresh_token.token)
                if not data or data["client_id"] != client.client_id:
                    raise TokenError("invalid_grant", "Refresh token is invalid or already used")
                if not set(scopes).issubset(data["scopes"]):
                    raise TokenError("invalid_scope", "Cannot widen scopes")
                row = self._db.execute(
                    "SELECT family FROM state WHERE kind='refresh' AND key=?", (_digest(refresh_token.token),)
                ).fetchone()
                family = row[0]
                self._db.execute(
                    "DELETE FROM state WHERE family=? AND kind IN ('access','refresh')", (family,)
                )
                self._put(
                    "spent_refresh",
                    refresh_token.token,
                    {"client_id": client.client_id, "family": family},
                    data["expires_at"],
                    client.client_id,
                    family,
                )
                result = self._mint(data["client_id"], scopes, family=family)
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = self._get("access", token)
        if not data or data.get("resource") != self.resource_url:
            return None
        return AccessToken(token=token, **data)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Also satisfy the SDK TokenVerifier protocol for FastMCP integration."""
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self._lock:
            row = self._db.execute(
                "SELECT family FROM state WHERE key=? AND kind IN ('access','refresh')",
                (_digest(token.token),),
            ).fetchone()
            if row:
                self._db.execute("DELETE FROM state WHERE family=?", (row[0],))

    def _html(
        self,
        ticket: str,
        csrf: str,
        client: OAuthClientInformationFull,
        error: str = "",
        status: int = 200,
        callback: str | None = None,
    ) -> HTMLResponse:
        callback = callback or str(client.redirect_uris[0])
        # Some browsers apply form-action to the 303 callback as well as the
        # initial POST. Permit only this request's already validated callback.
        callback_origin = _callback_origin(callback)
        # No script, remote assets, fonts, or tracking pixels on the secret-entry page.
        body = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PLMS MCP 연결 승인</title><main><h1>PLMS MCP 연결 승인</h1>
<p>연결 앱: <strong>{escape(client.client_name or "MCP client")}</strong></p>
<p>등록된 콜백: <code>{escape(callback)}</code></p>
<p>이 앱은 이 서버에 연결된 학생 계정의 강의, 과제, 공지, 출석과 첨부파일을 읽을 수 있습니다.</p>
<p>본인이 시작한 연결인지 앱 이름과 콜백 주소를 확인하세요. 별도로 설정한 서버 연결 암호를 입력하세요.</p>
<p role="alert">{escape(error)}</p><form method="post" action="/connect">
<input type="hidden" name="ticket" value="{escape(ticket)}"><input type="hidden" name="csrf" value="{escape(csrf)}">
<label>서버 연결 암호 <input type="password" name="password" autocomplete="current-password" maxlength="1024" required></label>
<button type="submit" name="decision" value="allow">읽기 권한 연결 승인</button>
<button type="submit" name="decision" value="deny" formnovalidate>거절</button></form></main></html>'''
        return HTMLResponse(
            body,
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
                # no-referrer makes native form POSTs send Origin: null, which
                # would correctly fail our strict Origin check. Cross-origin
                # callback redirects still explicitly use no-referrer below.
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": f"default-src 'none'; form-action 'self' {callback_origin}; base-uri 'none'; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    def _redirect(self, params: dict[str, Any], **values: str) -> RedirectResponse:
        parsed = urlsplit(params["redirect_uri"])
        query = parse_qs(parsed.query, keep_blank_values=True)
        query.update({key: [value] for key, value in values.items()})
        if params.get("state") is not None:
            query["state"] = [params["state"]]
        url = urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))
        return RedirectResponse(
            url, status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        )

    async def consent(self, request: Request) -> Response:
        form = await request.form() if request.method == "POST" else request.query_params
        ticket = str(form.get("ticket", ""))
        pending = self._get("pending", ticket)
        client = await self.get_client(pending["client_id"]) if pending else None
        if not pending or not client:
            return HTMLResponse(
                "연결 요청이 만료되었습니다. 앱에서 다시 연결하세요.",
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        cookie = request.cookies.get(self.cookie_name, "")
        if request.method == "GET":
            if not cookie or len(cookie) != 43:
                cookie = _random()
            csrf = _random()
            pending.update(session_hash=_digest(cookie), csrf_hash=_digest(csrf))
            # Do not extend the original five-minute authorization lifetime.
            row = self._db.execute(
                "SELECT expires FROM state WHERE kind='pending' AND key=?", (_digest(ticket),)
            ).fetchone()
            self._put("pending", ticket, pending, row[0], client.client_id)
            response = self._html(ticket, csrf, client, callback=pending["params"]["redirect_uri"])
            response.set_cookie(
                self.cookie_name,
                cookie,
                max_age=CODE_TTL,
                httponly=True,
                secure=self.secure_cookie,
                samesite="lax",
                path="/",
            )
            return response
        csrf = str(form.get("csrf", ""))
        if (
            request.headers.get("origin") != self.origin
            or not cookie
            or not csrf
            or not hmac.compare_digest(pending.get("session_hash", ""), _digest(cookie))
            or not hmac.compare_digest(pending.get("csrf_hash", ""), _digest(csrf))
        ):
            return HTMLResponse(
                "잘못된 연결 요청입니다. 앱에서 다시 시작하세요.",
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )
        if form.get("decision") == "deny":
            self._delete("pending", ticket)
            return self._redirect(pending["params"], error="access_denied")
        peer = request.client.host if request.client else "unknown"
        if self._limited("login:" + peer, 10, 300) or self._limited("login-global", 30, 300):
            return Response(
                "Too many attempts; retry in five minutes",
                status_code=429,
                headers={"Retry-After": "300", "Cache-Control": "no-store"},
            )
        if form.get("decision") != "allow" or not await self._password_matches(str(form.get("password", ""))):
            return self._html(
                ticket,
                csrf,
                client,
                "서버 연결 암호가 맞지 않습니다.",
                401,
                callback=pending["params"]["redirect_uri"],
            )
        code = _random()
        params = AuthorizationParams.model_validate(pending["params"])
        data = AuthorizationCode(
            code=code,
            client_id=client.client_id,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource_url,
            subject="instance-owner",
        ).model_dump(mode="json", exclude={"code"})
        with self._lock:
            # The password KDF yields to other requests; recheck one-time state.
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self._get("pending", ticket)
                if not current or current.get("csrf_hash") != pending.get("csrf_hash"):
                    self._db.execute("ROLLBACK")
                    return Response("Connection request already used", status_code=400)
                self._delete("pending", ticket)
                self._put("code", code, data, data["expires_at"], client.client_id)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return self._redirect(pending["params"], code=code)


class _OAuthRouteGuard:
    """Bound bodies/rates and reject token-request resource substitution.

    SDK 1.x validates PKCE and callback binding; resource validation at the token
    endpoint is enforced here because its request model ignores unknown fields.
    """

    def __init__(self, app: Any, provider: PersonalOAuthProvider, path: str):
        self.app, self.provider, self.path = app, provider, path

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if len(scope.get("query_string", b"")) > MAX_BODY:
            await Response("Request too large", status_code=413)(scope, receive, send)
            return
        if request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return
        peer = request.client.host if request.client else "unknown"
        limits = {
            "/register": (15, 3600),
            "/authorize": (40, 300),
            "/token": (100, 300),
            "/revoke": (100, 300),
            "/connect": (80, 300),
        }
        if self.path in limits:
            limit, window = limits[self.path]
            if self.provider._limited(self.path + ":" + peer, limit, window):
                await JSONResponse(
                    {"error": "temporarily_unavailable"},
                    status_code=429,
                    headers={"Retry-After": str(window), "Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                await Response("Request too large", status_code=413)(scope, receive, send)
                return
        if self.path in {"/token", "/revoke"}:
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/x-www-form-urlencoded"
            ):
                await JSONResponse(
                    {
                        "error": "invalid_request",
                        "error_description": "OAuth token and revocation requests require application/x-www-form-urlencoded",
                    },
                    status_code=415,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            resources = values.get("resource")
            if resources is not None and resources != [self.provider.resource_url]:
                await JSONResponse(
                    {"error": "invalid_target", "error_description": "Resource must match this MCP server"},
                    status_code=400,
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
            if self.path == "/revoke" and "client_secret" not in values:
                # SDK 1.x models this optional public-client field as required.
                # The authenticator still independently enforces secrets for
                # confidential clients; an empty value never bypasses that check.
                values["client_secret"] = [""]
                body = bytearray(urlencode(values, doseq=True).encode())
        sent = False

        async def replay() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_auth(
    issuer_url: str, connect_password: str, *, db_path: str | Path | None = None
) -> tuple[PersonalOAuthProvider, list[Route]]:
    """Return provider plus complete guarded OAuth + consent routes.

    Construct FastMCP with ``token_verifier=provider, auth=provider.auth_settings``
    (NOT auth_server_provider), then prepend these routes to its Starlette app.
    That retains MCP protected-resource metadata and its bearer-token middleware
    while using these guarded authorization-server endpoints exactly once.
    """
    provider = PersonalOAuthProvider(issuer_url, connect_password, db_path=db_path)
    routes = create_auth_routes(
        provider,
        AnyHttpUrl(provider.issuer_url),
        client_registration_options=provider.registration_options,
        revocation_options=provider.revocation_options,
    )
    routes.append(Route("/connect", provider.consent, methods=["GET", "POST"]))
    for route in routes:
        route.app = _OAuthRouteGuard(route.app, provider, route.path)
    return provider, routes

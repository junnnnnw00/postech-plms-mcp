"""POSTECH's observed SSO browser protocol, implemented without executing remote JS.

Only official HTTPS origins receive credentials; MFA/password-change is a handoff.
A failed login is never silently retried. The cryptographic envelope is compatibility
with the school's existing SEED-CBC flow, in addition to verified TLS.
"""

from __future__ import annotations

import json
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from Crypto.PublicKey import ECC
from cryptography.hazmat.decrepit.ciphers.algorithms import SEED
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import ANSIX923

from .errors import LoginFailed, MFARequired, UnsafeURL

BASE = "https://plms.postech.ac.kr"
SSO = "https://sso.postech.ac.kr"
AUTH_HOSTS = {"plms.postech.ac.kr", "sso.postech.ac.kr"}


def auth_url(url: str) -> str:
    try:
        p = urlsplit(url)
        allowed = (
            p.scheme == "https"
            and p.hostname in AUTH_HOSTS
            and p.port in (None, 443)
            and p.username is None
            and p.password is None
        )
    except (ValueError, TypeError):
        allowed = False
    if not allowed:
        raise UnsafeURL("Authentication redirect left the official POSTECH origins.")
    return url


async def auth_request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    for _ in range(12):
        response = await client.request(method, auth_url(url), follow_redirects=False, **kwargs)
        if not response.is_redirect:
            response.raise_for_status()
            return response
        url = urljoin(str(response.url), response.headers["location"])
        if response.status_code in (301, 302, 303):
            method, kwargs = "GET", {}
    raise LoginFailed("Too many SSO redirects.")


def hidden_fields(form) -> dict:
    return {x["name"]: x.get("value", "") for x in form.select('input[type="hidden"][name]')}


def logged_in(html: str) -> bool:
    s = BeautifulSoup(html, "html.parser")
    return bool(s.select('a[href*="/login/logout.php"]')) and not bool(s.select("body.notloggedin"))


def make_envelope(private: int, server_x: str, server_y: str, username: str, password: str) -> str:
    try:
        if any(not isinstance(value, str) or not 1 <= len(value) <= 66 for value in (server_x, server_y)):
            raise ValueError("Invalid coordinate encoding")
        # EccPoint validates curve membership in the maintained native backend.
        # The backend also represents infinity as (0, 0), which is not a valid
        # peer key for this exchange and must be rejected explicitly.
        peer = ECC.EccPoint(int(server_x, 16), int(server_y, 16), curve="P-256")
        if peer.is_point_at_infinity():
            raise ValueError("Point at infinity")
        scalar = ECC.construct(curve="P-256", d=private).d
        shared = peer * scalar
        if shared.is_point_at_infinity():
            raise ValueError("Invalid shared point")
    except (ValueError, TypeError, OverflowError):
        raise LoginFailed("SSO supplied invalid key-exchange parameters.") from None
    # The published client takes the first 128 bits of shared X for SEED,
    # and the first 128 bits of shared Y for its IV.
    key = int(shared.x).to_bytes(32, "big")[:16]
    iv = int(shared.y).to_bytes(32, "big")[:16]
    plain = json.dumps(
        {"login_id": username, "login_pwd": password}, separators=(",", ":"), ensure_ascii=False
    ).encode()
    pad = ANSIX923(128).padder()
    enc = Cipher(SEED(key), modes.CBC(iv)).encryptor()
    return (enc.update(pad.update(plain) + pad.finalize()) + enc.finalize()).hex()


async def login_sso(client: httpx.AsyncClient, username: str, password: str) -> dict:
    r = await auth_request(client, "GET", BASE + "/passni/sso/spLogin.php")
    s = BeautifulSoup(r.text, "html.parser")
    f = s.select_one("form#frmSSO")
    if f:
        r = await auth_request(
            client, "POST", urljoin(str(r.url), f.get("action", "")), data=hidden_fields(f)
        )
    if logged_in(r.text):
        return {"authenticated": True, "method": "sso"}
    s = BeautifulSoup(r.text, "html.parser")
    key_node = s.select_one('input[name="login_key"]')
    if not key_node:
        raise LoginFailed("SSO login form changed; use a browser session import.")
    ephemeral = ECC.generate(curve="P-256")
    private = int(ephemeral.d)
    public = ephemeral.pointQ
    r_init = await auth_request(
        client,
        "POST",
        SSO + "/sso/usr/postech/login/init",
        data={"user_ec_publickey": f"{int(public.x):064x}{int(public.y):064x}"},
    )
    result = r_init.json()
    if result.get("code") != "SS0001":
        raise LoginFailed("SSO initialization failed.")
    envelope = make_envelope(private, result["svr_qx"], result["svr_qy"], username, password)
    r_auth = await auth_request(
        client,
        "POST",
        SSO + "/sso/usr/postech/login/auth",
        data={"user_data": envelope, "login_key": key_node.get("value", "")},
    )
    result = r_auth.json()
    if result.get("code") == "SS0030":
        raise MFARequired("POSTECH requires StonePASS MFA. Complete browser login, then import PLMS cookies.")
    if result.get("code") != "SS0001":
        # Do not relay remote errors that could echo identifiers or credentials.
        raise LoginFailed("POSTECH SSO rejected the login or requires browser action. No retry was made.")
    if result.get("privacyResult") not in (None, "Y", "N", "X"):
        raise LoginFailed("POSTECH requires a privacy choice in the browser before continuing.")
    r = await auth_request(
        client,
        "POST",
        SSO + "/sso/usr/postech/login/link",
        data={"login_key": key_node.get("value", ""), "user_data": ""},
    )
    for _ in range(8):
        if logged_in(r.text):
            return {"authenticated": True, "method": "sso"}
        s = BeautifulSoup(r.text, "html.parser")
        if (
            urlsplit(str(r.url)).path == "/passni/sso/spLoginData.php"
            and "'/passni/coursemos/loginBeforeProc.php'" in r.text
        ):
            import re

            error = re.search(r"var err_code\s*=\s*['\"]([^'\"]*)", r.text)
            if error and error.group(1):
                raise LoginFailed("SSO assertion was rejected by PLMS.")
            r = await auth_request(client, "GET", BASE + "/passni/coursemos/loginBeforeProc.php")
            if "/sso/api/cors/get/ip" not in r.text:
                raise LoginFailed("PLMS SSO completion flow changed.")
            ip_response = await auth_request(client, "POST", SSO + "/sso/api/cors/get/ip")
            import ipaddress

            client_ip = str(ipaddress.ip_address(ip_response.json()["client_ip"]))
            r = await auth_request(
                client, "POST", BASE + "/passni/coursemos/loginProc.php", data={"pni_client_ip": client_ip}
            )
            continue
        f = s.find("form")
        if not f or f.select('input[type="password"]'):
            break
        target = auth_url(urljoin(str(r.url), f.get("action", "")))
        # Only hidden assertion/relay forms emitted by the official SSO flow.
        if not f.select('input[type="hidden"]'):
            break
        r = await auth_request(client, f.get("method", "post").upper(), target, data=hidden_fields(f))
    r = await auth_request(client, "GET", BASE + "/")
    if not logged_in(r.text):
        raise LoginFailed("SSO did not establish a PLMS session; browser action is required.")
    return {"authenticated": True, "method": "sso"}


async def login_direct(client: httpx.AsyncClient, username: str, password: str) -> dict:
    r = await auth_request(client, "GET", BASE + "/login/index.php")
    f = BeautifulSoup(r.text, "html.parser").select_one("form.form-login")
    if not f:
        raise LoginFailed("PLMS login form not found.")
    data = hidden_fields(f)
    data.update(username=username, password=password)
    r = await auth_request(client, "POST", BASE + "/login/index.php", data=data)
    if not logged_in(r.text):
        raise LoginFailed("PLMS rejected the login. POSTECH members usually need --method sso.")
    return {"authenticated": True, "method": "direct"}


def export_cookies(client: httpx.AsyncClient) -> list[dict]:
    return [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "expires": c.expires}
        for c in client.cookies.jar
        if c.domain.lstrip(".") == "plms.postech.ac.kr"
    ]


def import_cookies(client: httpx.AsyncClient, cookies: list[dict]) -> None:
    import time

    for c in cookies:
        if c.get("domain", "").lstrip(".") != "plms.postech.ac.kr":
            continue
        expires = c.get("expires") or c.get("expirationDate")
        if expires and expires > 0 and expires < time.time():
            continue
        client.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))

"""Local administration; passwords and cookies never become MCP tool arguments."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from pathlib import Path

import httpx

from . import auth
from .errors import PLMSError
from .storage import Vault


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="plms-mcp", description="Personal read-only POSTECH PLMS MCP")
    result.add_argument("--data-dir", type=Path, help="Private state directory (or PLMS_DATA_DIR)")
    commands = result.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Sign in to PLMS and save an encrypted session")
    login.add_argument("--method", choices=["sso", "direct"], default="sso")
    login.add_argument("--username", help="POSTECH account ID (or PLMS_USERNAME; otherwise prompt)")
    login.add_argument(
        "--save-password", action="store_true", help="Explicitly permit encrypted credential storage"
    )
    cookies = commands.add_parser(
        "import-cookies", help="Import and verify a browser's exported PLMS cookies"
    )
    cookies.add_argument("path", type=Path, help="Cookie JSON list or Playwright storage-state JSON")
    commands.add_parser("doctor", help="Verify the saved session without logging in again")
    commands.add_parser("logout", help="Delete this instance's local credentials, cookies and snapshots")
    serve = commands.add_parser("serve", help="Run the MCP server")
    serve.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    return result


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": "postech-plms-mcp/0.1 personal-read-only"},
    )


async def _verify(client: httpx.AsyncClient) -> bool:
    response = await auth.auth_request(client, "GET", auth.BASE + "/")
    return auth.logged_in(response.text)


async def _login(vault: Vault, args: argparse.Namespace, username: str, password: str) -> int:
    async with _http_client() as client:
        function = auth.login_sso if args.method == "sso" else auth.login_direct
        await function(client, username, password)
        if not await _verify(client):
            print(
                "Login did not establish a PLMS session. Complete browser login and import cookies.",
                file=sys.stderr,
            )
            return 1
        cookies = auth.export_cookies(client)
        if not cookies:
            print("No PLMS session cookie was issued. Nothing was saved.", file=sys.stderr)
            return 1
        # A failed authentication never overwrites a working local session.
        vault.write("cookies", cookies)
        vault.delete("snapshots")
        if args.save_password:
            vault.write("credentials", {"username": username, "password": password, "method": args.method})
        else:
            # An old account/password must not silently survive a new login.
            vault.delete("credentials")
    print(
        "PLMS session verified and saved encrypted. Password "
        + ("saved by explicit request." if args.save_password else "not saved.")
    )
    return 0


def _read_cookie_file(path: Path) -> list[dict]:
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("Cookie export is too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    cookies = value.get("cookies") if isinstance(value, dict) else value
    if not isinstance(cookies, list) or len(cookies) > 5000:
        raise ValueError("Expected a cookie list or storage-state object")
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise TypeError("Invalid cookie entry")
        if not all(isinstance(cookie.get(field), str) for field in ["name", "value", "domain"]):
            raise ValueError("Cookie name, value and domain are required")
        for field in ["expires", "expirationDate"]:
            if cookie.get(field) is not None and not isinstance(cookie[field], (int, float)):
                raise ValueError("Invalid cookie expiry")
        if "path" in cookie and not isinstance(cookie["path"], str):
            raise ValueError("Invalid cookie path")
    return cookies


async def _import(vault: Vault, path: Path) -> int:
    cookies = _read_cookie_file(path)
    async with _http_client() as client:
        auth.import_cookies(client, cookies)
        if not auth.export_cookies(client) or not await _verify(client):
            print(
                "Imported cookies are not an active PLMS session. No local state was changed.",
                file=sys.stderr,
            )
            return 1
        vault.write("cookies", auth.export_cookies(client))
        vault.delete("credentials")
        vault.delete("snapshots")
    print("Imported PLMS session verified and saved encrypted. Protect or delete the original cookie export.")
    return 0


async def _doctor(vault: Vault) -> int:
    cookies = vault.read("cookies")
    if not isinstance(cookies, list) or not cookies:
        print("No saved session. Run plms-mcp login or plms-mcp import-cookies PATH.", file=sys.stderr)
        return 1
    async with _http_client() as client:
        auth.import_cookies(client, cookies)
        if not await _verify(client):
            print("Saved PLMS session expired. Run login or import a fresh browser session.", file=sys.stderr)
            return 1
        vault.write("cookies", auth.export_cookies(client))
    print("OK: PLMS session is active; encrypted state is readable.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        vault = Vault(args.data_dir.expanduser() if args.data_dir else None)
        if args.command == "login":
            username = args.username or os.environ.get("PLMS_USERNAME") or input("POSTECH account ID: ")
            password = os.environ.get("PLMS_PASSWORD") or getpass.getpass("POSTECH password: ")
            if not username.strip() or not password:
                print("Account ID and password are required.", file=sys.stderr)
                return 2
            return asyncio.run(_login(vault, args, username.strip(), password))
        if args.command == "import-cookies":
            return asyncio.run(_import(vault, args.path.expanduser()))
        if args.command == "doctor":
            return asyncio.run(_doctor(vault))
        if args.command == "logout":
            for record in ["cookies", "credentials", "snapshots"]:
                vault.delete(record)
            print(
                "Local PLMS session, credentials and snapshots deleted. School browser sessions are unchanged."
            )
            return 0
        if not 1 <= args.port <= 65535:
            print("Port must be between 1 and 65535.", file=sys.stderr)
            return 2
        from .server import create_http_app, create_server

        if args.transport == "stdio":
            create_server(vault).run(transport="stdio")
            return 0
        issuer = os.environ.get("PLMS_ISSUER_URL", "")
        connect_password = os.environ.get("PLMS_CONNECT_PASSWORD", "")
        if not issuer or len(connect_password) < 20:
            print(
                "HTTP requires PLMS_ISSUER_URL and a separate random PLMS_CONNECT_PASSWORD of at least 20 characters.",
                file=sys.stderr,
            )
            return 2
        import uvicorn

        app = create_http_app(vault, issuer, connect_password)
        # Do not log authorization/consent URLs. One worker preserves rate limits.
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            access_log=False,
            workers=1,
            proxy_headers=False,
            log_level="warning",
        )
        return 0
    except PLMSError as error:
        messages = {
            "mfa_required": "POSTECH requires browser authentication/MFA. Complete it, then import PLMS cookies.",
            "login_failed": "PLMS login failed or needs browser action. No automatic retry was made.",
            "unsafe_url": "Authentication stopped because a redirect left the allowed POSTECH origins.",
        }
        print(
            messages.get(error.code, "PLMS request could not be completed. Run doctor or reconnect."),
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError:
        print("Could not reach POSTECH securely. Check the network and retry manually.", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # Exception strings and response bodies can contain credential material.
        print(
            "Configuration or private state is invalid or inaccessible. Check the data path, cookie JSON and OAuth settings.",
            file=sys.stderr,
        )
        return 2
    except (EOFError, KeyboardInterrupt):
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 - final credential-redacting CLI boundary
        print(
            "An unexpected local error stopped the command. No credentials or server response were printed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

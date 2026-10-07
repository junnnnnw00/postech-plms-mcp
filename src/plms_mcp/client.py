"""Bounded, serialized, read-only PLMS HTTP access with private session storage."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import httpx

from .auth import BASE, auth_request, export_cookies, import_cookies, logged_in, login_direct, login_sso
from .errors import AuthenticationRequired, UnsafeURL, UpstreamError
from .storage import Vault

READ_PATHS = {
    "/",
    "/my/",
    "/course/view.php",
    "/local/ubion/user/",
    "/local/ubion/course/lists.php",
    "/local/ubion/course/lists_view.php",
    "/local/ubion/course/syllabus.php",
    "/local/ubion/course/syllabusV.php",
    "/grade/report/user/index.php",
    "/local/ubonattend/index.php",
    "/local/ubonattend/my_status.php",
    "/local/uboffattend/ereport.php",
    "/local/uboffattend/ereport_user.php",
    "/local/uboffattend/index.php",
    "/local/uboffattend/user.php",
    "/mod/assign/index.php",
    "/mod/assign/view.php",
    "/mod/ubboard/index.php",
    "/mod/ubboard/view.php",
    "/mod/ubboard/article.php",
    "/mod/vod/index.php",
    "/mod/forum/index.php",
    "/mod/forum/view.php",
    "/mod/forum/discuss.php",
    "/mod/quiz/index.php",
    "/mod/quiz/view.php",
    "/mod/resource/index.php",
    "/mod/resource/view.php",
    "/mod/ubfile/index.php",
    "/mod/ubfile/view.php",
    "/mod/folder/view.php",
    "/mod/page/view.php",
    "/mod/url/index.php",
    "/mod/url/view.php",
    "/calendar/view.php",
}
READ_KEYS = {
    "id",
    "course",
    "courseid",
    "page",
    "bwid",
    "d",
    "lang",
    "section",
    "redirect",
    "view",
    "time",
    "year",
    "semester",
    "term",
    "category",
    "search",
    "searchkey",
    "searchval",
    "searchtext",
    "searchtype",
    "perpage",
    "userid",
    "user",
    "ubpage",
    "pageid",
    "pagegroup",
}


def safe_url(url: str, *, attachment: bool = False) -> str:
    url = urljoin(BASE + "/", url)
    p = urlsplit(url)
    if (
        p.scheme != "https"
        or p.hostname != "plms.postech.ac.kr"
        or p.port not in (None, 443)
        or p.username
        or p.password
        or p.fragment
    ):
        raise UnsafeURL("Only official HTTPS PLMS read URLs are allowed.")
    path = unquote(p.path)
    if path != p.path or "\\" in path or any(x in {"..", "."} for x in path.split("/")):
        # File names often contain percent-encoded Korean, but never encoded routing separators.
        if (
            not attachment
            or any(x in path for x in ("/../", "/./", "\\"))
            or "%2f" in p.path.lower()
            or "%5c" in p.path.lower()
        ):
            raise UnsafeURL("Encoded or ambiguous paths are not allowed.")
    qs = parse_qs(p.query, keep_blank_values=True)
    if any(len(values) != 1 for values in qs.values()):
        raise UnsafeURL("Duplicate query parameters are not allowed.")
    if attachment:
        if not (path.startswith("/pluginfile.php/") or path.startswith("/mod/ubboard/download.php")):
            raise UnsafeURL("This is not a supported PLMS attachment URL.")
        if set(qs) - {"forcedownload", "id", "bwid", "fileid", "num", "fid"}:
            raise UnsafeURL("Unsupported attachment query.")
    else:
        if path not in READ_PATHS or set(qs) - READ_KEYS:
            raise UnsafeURL("This URL is not an allowed read-only PLMS page.")
        if qs.get("redirect", ["0"])[0] != "0":
            raise UnsafeURL("External redirects are disabled.")
    if len(url) > 4096:
        raise UnsafeURL("URL is too long.")
    return url


class PLMSClient:
    def __init__(self, vault: Vault, *, transport=None, cache_seconds: float = 90):
        self.vault = vault
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(30),
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "postech-plms-mcp/0.1 (personal read-only client)"},
        )
        self._saved_cookies = vault.read("cookies") or []
        import_cookies(self.http, self._saved_cookies)
        self._request_lock = asyncio.Lock()
        self._login_lock = asyncio.Lock()
        self._login_generation = 0
        self._last_request = 0.0
        self._cache: dict[str, tuple[float, str, str]] = {}
        self.source_fetched_at: dict[str, str] = {}
        self.cache_seconds = cache_seconds
        self._closed = False

    async def close(self):
        if not self._closed:
            await self.http.aclose()
            self._closed = True

    def _sync_saved_cookies(self) -> None:
        """Apply CLI login/logout before serving even a cached page."""
        saved = self.vault.read("cookies") or []
        if saved != self._saved_cookies:
            self.http.cookies.clear()
            import_cookies(self.http, saved)
            self._saved_cookies = saved
            self._cache.clear()
            self.source_fetched_at.clear()

    def _save_cookies(self) -> None:
        # Do not overwrite a CLI login/logout which occurred during a request.
        if (self.vault.read("cookies") or []) != self._saved_cookies:
            self._sync_saved_cookies()
            return
        self._saved_cookies = export_cookies(self.http)
        self.vault.write("cookies", self._saved_cookies)

    async def _request(self, url: str, *, attachment: bool = False, max_bytes: int = 4_000_000):
        url = safe_url(url, attachment=attachment)
        for _ in range(8):
            async with self._request_lock:
                delay = 0.15 - (time.monotonic() - self._last_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_request = time.monotonic()
                try:
                    async with self.http.stream("GET", url) as response:
                        if response.is_redirect:
                            next_url = urljoin(url, response.headers.get("location", ""))
                            if "/login/" in next_url or "sso.postech.ac.kr" in next_url:
                                raise AuthenticationRequired("PLMS session expired; run plms-mcp login.")
                            # Moodle resource view may redirect to an authenticated attachment.
                            file_redirect = urlsplit(next_url).path.startswith("/pluginfile.php/")
                            url = safe_url(next_url, attachment=attachment or file_redirect)
                            attachment = attachment or file_redirect
                            continue
                        if response.status_code == 401:
                            raise AuthenticationRequired(
                                "PLMS denied this account access to the requested page."
                            )
                        if response.status_code == 403:
                            raise UpstreamError(
                                "This PLMS page is not accessible to the current account. No re-login was attempted."
                            )
                        if response.status_code == 429:
                            raise UpstreamError("PLMS rate limit reached. Try again later.")
                        if response.status_code != 200:
                            raise UpstreamError(f"PLMS returned HTTP {response.status_code}.")
                        try:
                            declared_size = int(response.headers.get("content-length", "0"))
                        except ValueError:
                            raise UpstreamError("PLMS returned an invalid response length.") from None
                        if declared_size < 0:
                            raise UpstreamError("PLMS returned an invalid response length.")
                        if declared_size > max_bytes:
                            raise UpstreamError("PLMS response exceeds the configured size limit.")
                        chunks, size = [], 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > max_bytes:
                                raise UpstreamError("PLMS response exceeds the configured size limit.")
                            chunks.append(chunk)
                        if len(self.source_fetched_at) >= 512:
                            self.source_fetched_at.pop(next(iter(self.source_fetched_at)))
                        self.source_fetched_at[url] = datetime.now(ZoneInfo("Asia/Seoul")).isoformat(
                            timespec="seconds"
                        )
                        return b"".join(chunks), dict(response.headers), url
                except httpx.HTTPError:
                    raise UpstreamError(
                        "PLMS network request failed; credentials and response bodies were not logged."
                    ) from None
        raise UpstreamError("Too many PLMS redirects.")

    async def refresh_session(self, *, expected_cookies=None, expected_generation=None) -> bool:
        async with self._login_lock:
            # Re-read cookies so CLI re-login takes effect without restarting the server.
            self._sync_saved_cookies()
            if (
                expected_generation is not None and self._login_generation != expected_generation
            ) or (expected_cookies is not None and self._saved_cookies != expected_cookies):
                # Another queued request already refreshed this session.
                return True
            credentials = self.vault.read("credentials")
            if not credentials or credentials.get("disabled"):
                return False
            if not self.vault.begin_login(credentials):
                return False
            login = login_sso if credentials.get("method", "sso") == "sso" else login_direct
            try:
                await login(self.http, credentials["username"], credentials["password"])
                response = await auth_request(self.http, "GET", BASE + "/")
                if not logged_in(response.text) or not export_cookies(self.http):
                    raise AuthenticationRequired("Re-login did not establish a verified PLMS session")
            except Exception:
                raise AuthenticationRequired(
                    "Automatic PLMS re-login failed and is disabled. Run plms-mcp login again."
                ) from None
            self._save_cookies()
            self.vault.complete_login(credentials)
            self._login_generation += 1
            self._cache.clear()
            return True

    async def page(self, url: str, *, fresh: bool = False) -> tuple[str, str]:
        url = safe_url(url)
        self._sync_saved_cookies()
        now = time.monotonic()
        if not fresh and url in self._cache and now - self._cache[url][0] < self.cache_seconds:
            return self._cache[url][1:]

        async def fetch():
            data, headers, final = await self._request(url)
            if "html" not in headers.get("content-type", "").lower():
                raise UpstreamError("This URL returned a file. Use read_attachment with its source page.")
            text = data.decode("utf-8", errors="replace")
            if 'id="page-login-index"' in text or 'id="login_pwd"' in text:
                raise AuthenticationRequired("PLMS session expired; run plms-mcp login.")
            return text, final

        expected_cookies = self._saved_cookies
        expected_generation = self._login_generation
        try:
            result = await fetch()
        except AuthenticationRequired:
            if not await self.refresh_session(
                expected_cookies=expected_cookies, expected_generation=expected_generation
            ):
                raise
            result = await fetch()
        if len(self._cache) >= 256:
            self._cache.pop(next(iter(self._cache)))
        self._cache[url] = (time.monotonic(), *result)
        self._save_cookies()
        return result

    async def download(self, url: str):
        self._sync_saved_cookies()
        data, headers, final = await self._request(url, attachment=True, max_bytes=25 * 1024 * 1024)
        if "html" in headers.get("content-type", "").lower() and b"page-login-index" in data[:200000]:
            raise AuthenticationRequired("PLMS attachment session expired.")
        return data, headers, final

    def clear_cache(self):
        self._cache.clear()

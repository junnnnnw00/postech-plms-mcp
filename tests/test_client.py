import httpx
import pytest

from plms_mcp import client as client_module
from plms_mcp.auth import BASE
from plms_mcp.client import PLMSClient, safe_url
from plms_mcp.errors import AuthenticationRequired, UnsafeURL, UpstreamError
from plms_mcp.storage import Vault


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/course/view.php?id=1",
        "http://plms.postech.ac.kr/course/view.php?id=1",
        "https://plms.postech.ac.kr.evil.example/course/view.php?id=1",
        "https://plms.postech.ac.kr@evil.example/course/view.php?id=1",
        "https://user:pass@plms.postech.ac.kr/course/view.php?id=1",
        "https://plms.postech.ac.kr:444/course/view.php?id=1",
        "file:///etc/passwd",
        "/mod/assign/view.php?id=1&action=editsubmission",
        "/mod/assign/view.php?id=1&action=submit",
        "/mod/assign/view.php?id=1&sesskey=secret",
        "/login/logout.php?sesskey=secret",
        "/mod/vod/view.php?id=1",
        "/mod/vod/player.php?id=1",
        "/mod/vod/progress.php?id=1",
        "/course/%76iew.php?id=1",
        "/mod/url/view.php?id=1&redirect=1",
        "/mod/url/view.php?id=1&redirect=0&redirect=1",
        "/course/view.php?id=1#secret",
    ],
)
def test_reject_mutations_playback_and_untrusted_urls(url):
    with pytest.raises(UnsafeURL):
        safe_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "/course/view.php?id=1",
        "/mod/assign/view.php?id=2",
        "/local/ubonattend/index.php?id=3",
        "/mod/url/view.php?id=4&redirect=0",
    ],
)
def test_allow_documented_read_pages(url):
    assert safe_url(url) == BASE + url


@pytest.mark.parametrize(
    "url",
    [
        "/pluginfile.php/1/mod_resource/content/0/notes.pdf",
        "/pluginfile.php/1/mod_resource/content/0/%EA%B0%95%EC%9D%98.pdf",
        "/mod/ubboard/download.php?id=1&bwid=2&fileid=3",
    ],
)
def test_allow_official_attachment_locations(url):
    assert safe_url(url, attachment=True) == BASE + url


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/notes.pdf",
        "/draftfile.php/123/file.pdf",
        "/pluginfile.php/1/%2fetc/passwd",
        "/pluginfile.php/1/%5cfile.pdf",
        "/pluginfile.php/1/file.pdf?sesskey=secret",
        "/course/view.php?id=1",
    ],
)
def test_attachment_route_restrictions(url):
    with pytest.raises(UnsafeURL):
        safe_url(url, attachment=True)


@pytest.mark.asyncio
async def test_unsafe_target_never_reaches_network(vault):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, text="unexpected")

    client = PLMSClient(vault, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UnsafeURL):
            await client.page("/mod/assign/view.php?id=1&action=submit")
        with pytest.raises(UnsafeURL):
            await client.download("https://evil.example/private.pdf")
        assert not requests
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["https://evil.example/private", "/mod/vod/view.php?id=1"])
async def test_redirect_is_checked_before_next_network_request(vault, target):
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    client = PLMSClient(vault, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UnsafeURL):
            await client.page("/course/view.php?id=1")
        assert requests == [BASE + "/course/view.php?id=1"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_failed_auto_login_is_disabled_and_not_repeated(vault, monkeypatch):
    attempts = []
    vault.write("credentials", {"username": "synthetic", "password": "never-print-this", "method": "sso"})

    async def login(*args):
        attempts.append(True)
        raise RuntimeError("never-print-this")

    monkeypatch.setattr(client_module, "login_sso", login)
    client = PLMSClient(
        vault,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": "/login/index.php"})
        ),
    )
    try:
        for _ in range(2):
            with pytest.raises(AuthenticationRequired) as error:
                await client.page("/", fresh=True)
            assert "never-print-this" not in str(error.value)
        assert len(attempts) == 1
        assert vault.read("credentials")["disabled"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cli_cookie_refresh_works_on_the_next_request(vault):
    def cookie(value):
        return [{"name": "MoodleSession", "value": value, "domain": "plms.postech.ac.kr", "path": "/"}]

    vault.write("cookies", cookie("old"))
    requests = []

    def handler(request):
        requests.append(request)
        if "MoodleSession=new" not in request.headers.get("cookie", ""):
            return httpx.Response(302, headers={"location": "/login/index.php"})
        return httpx.Response(
            200, text='<a href="/login/logout.php">Logout</a>', headers={"content-type": "text/html"}
        )

    client = PLMSClient(vault, transport=httpx.MockTransport(handler))
    vault.write("cookies", cookie("new"))
    try:
        html, _ = await client.page("/", fresh=True)
        assert "Logout" in html
        assert len(requests) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cache_and_fresh_bypass(vault):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, text=f"<p>Version {len(requests)}</p>", headers={"content-type": "text/html"}
        )

    client = PLMSClient(vault, transport=httpx.MockTransport(handler))
    try:
        first, _ = await client.page("/course/view.php?id=1")
        second, _ = await client.page("/course/view.php?id=1")
        fresh, _ = await client.page("/course/view.php?id=1", fresh=True)
        assert first == second
        assert first != fresh
        assert len(requests) == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cli_logout_invalidates_running_server_cookies_and_cache(vault):
    cookie = [{"name": "MoodleSession", "value": "active", "domain": "plms.postech.ac.kr", "path": "/"}]
    vault.write("cookies", cookie)
    seen = []

    def handler(request):
        seen.append(request)
        if "MoodleSession=active" in request.headers.get("cookie", ""):
            return httpx.Response(200, text="<p>Private page</p>", headers={"content-type": "text/html"})
        return httpx.Response(302, headers={"location": "/login/index.php"})

    client = PLMSClient(vault, transport=httpx.MockTransport(handler))
    try:
        await client.page("/course/view.php?id=1")
        vault.delete("cookies")
        with pytest.raises(AuthenticationRequired):
            await client.page("/course/view.php?id=1")
        assert len(seen) == 2
        assert "MoodleSession" not in seen[-1].headers.get("cookie", "")
        assert vault.read("cookies") is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forbidden_page_does_not_trigger_reauthentication(vault, monkeypatch):
    vault.write("credentials", {"username": "synthetic", "password": "secret"})
    calls = []

    async def login(*args):
        calls.append(True)

    monkeypatch.setattr(client_module, "login_sso", login)
    client = PLMSClient(vault, transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    try:
        with pytest.raises(UpstreamError):
            await client.page("/course/view.php?id=1")
        assert not calls
    finally:
        await client.close()


class Chunks(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"123456"
        yield b"789012"


@pytest.mark.asyncio
async def test_stream_and_declared_size_limits(vault):
    client = PLMSClient(
        vault, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Chunks()))
    )
    try:
        with pytest.raises(UpstreamError, match="size limit"):
            await client._request(BASE + "/", max_bytes=10)
    finally:
        await client.close()
    client = PLMSClient(
        vault,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-length": str(26 * 1024 * 1024)}, content=b"tiny"
            )
        ),
    )
    try:
        with pytest.raises(UpstreamError, match="size limit"):
            await client.download(BASE + "/pluginfile.php/1/file.pdf")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_rate_limit_and_network_errors_are_safe(vault):
    client = PLMSClient(
        vault, transport=httpx.MockTransport(lambda request: httpx.Response(429, text="private remote error"))
    )
    try:
        with pytest.raises(UpstreamError, match="rate limit") as error:
            await client.page("/")
        assert "private remote error" not in str(error.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_file_redirect_cannot_be_misread_as_html(vault):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/mod/resource/view.php":
            return httpx.Response(302, headers={"location": "/pluginfile.php/1/notes.pdf"})
        return httpx.Response(200, content=b"%PDF-synthetic", headers={"content-type": "application/pdf"})

    client = PLMSClient(vault, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(UpstreamError, match="returned a file"):
            await client.page("/mod/resource/view.php?id=1")
        assert len(seen) == 2
    finally:
        await client.close()

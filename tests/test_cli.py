import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from plms_mcp import cli
from plms_mcp.errors import LoginFailed
from plms_mcp.storage import Vault

ACTIVE = '<html><a href="/login/logout.php">Logout</a></html>'
COOKIE = {
    "name": "MoodleSession",
    "value": "synthetic-cookie-secret",
    "domain": "plms.postech.ac.kr",
    "path": "/",
}


@pytest.fixture
def local(tmp_path, monkeypatch):
    for name in ["PLMS_USERNAME", "PLMS_PASSWORD", "PLMS_CONNECT_PASSWORD", "PLMS_ISSUER_URL"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLMS_DATA_DIR", str(tmp_path))
    return Vault(tmp_path)


def mock_http(monkeypatch, *, active=True, redirect=None):
    requests = []

    def handler(request):
        requests.append(request)
        if redirect:
            return httpx.Response(302, headers={"location": redirect})
        return httpx.Response(200, text=ACTIVE if active else '<body class="notloggedin">Login</body>')

    monkeypatch.setattr(
        cli, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return requests


def mock_login(monkeypatch, *, fail=False):
    calls = []

    async def login(client, username, password):
        calls.append((username, password))
        if fail:
            raise LoginFailed("Never print synthetic-password-secret")
        client.cookies.set(COOKIE["name"], COOKIE["value"], domain=COOKIE["domain"])
        return {"authenticated": True}

    monkeypatch.setattr(cli.auth, "login_sso", login)
    monkeypatch.setattr(cli.auth, "login_direct", login)
    monkeypatch.setenv("PLMS_USERNAME", "test-student")
    monkeypatch.setenv("PLMS_PASSWORD", "synthetic-password-secret")
    return calls


def test_login_verifies_and_does_not_save_password_by_default(local, monkeypatch, capsys):
    requests = mock_http(monkeypatch)
    calls = mock_login(monkeypatch)
    local.write("credentials", {"username": "old", "password": "old"})
    local.write("snapshots", {"old": True})
    assert cli.main(["login"]) == 0
    assert calls == [("test-student", "synthetic-password-secret")]
    assert str(requests[0].url) == "https://plms.postech.ac.kr/"
    assert local.read("cookies")[0]["value"] == COOKIE["value"]
    assert local.read("credentials") is None
    assert local.read("snapshots") is None
    output = capsys.readouterr()
    assert "synthetic-password-secret" not in output.out + output.err
    assert COOKIE["value"] not in output.out + output.err


def test_save_password_requires_explicit_flag(local, monkeypatch):
    mock_http(monkeypatch)
    mock_login(monkeypatch)
    assert cli.main(["login", "--method", "direct", "--save-password"]) == 0
    assert local.read("credentials") == {
        "username": "test-student",
        "password": "synthetic-password-secret",
        "method": "direct",
    }
    assert b"synthetic-password-secret" not in (local.directory / "credentials.enc").read_bytes()


def test_failed_login_keeps_previous_session_and_redacts_errors(local, monkeypatch, capsys):
    mock_http(monkeypatch)
    mock_login(monkeypatch, fail=True)
    local.write("cookies", [COOKIE])
    assert cli.main(["login", "--save-password"]) == 1
    assert local.read("cookies") == [COOKIE]
    assert local.read("credentials") is None
    assert "synthetic-password-secret" not in capsys.readouterr().err


def test_login_result_needs_independent_session_verification(local, monkeypatch):
    mock_http(monkeypatch, active=False)
    mock_login(monkeypatch)
    assert cli.main(["login", "--save-password"]) == 1
    assert local.read("cookies") is None
    assert local.read("credentials") is None


def test_doctor_and_logout_only_local_state(local, monkeypatch):
    requests = mock_http(monkeypatch)
    local.write("cookies", [COOKIE])
    local.write("credentials", {"username": "test", "password": "secret"})
    local.write("snapshots", {"events": []})
    assert cli.main(["doctor"]) == 0
    assert len(requests) == 1
    assert cli.main(["logout"]) == 0
    assert len(requests) == 1
    assert all(local.read(name) is None for name in ["cookies", "credentials", "snapshots"])
    assert cli.main(["doctor"]) == 1
    assert len(requests) == 1


def test_expired_session_does_not_trigger_password_login(local, monkeypatch):
    requests = mock_http(monkeypatch, active=False)
    calls = mock_login(monkeypatch)
    local.write("cookies", [COOKIE])
    local.write("credentials", {"username": "test", "password": "secret"})
    assert cli.main(["doctor"]) == 1
    assert len(requests) == 1
    assert calls == []


def test_cookie_import_filters_other_domains_and_verifies(local, monkeypatch, tmp_path):
    requests = mock_http(monkeypatch)
    path = tmp_path / "export.json"
    path.write_text(
        json.dumps({"cookies": [COOKIE, {**COOKIE, "domain": "evil.example", "value": "foreign"}]})
    )
    local.write("credentials", {"password": "old"})
    assert cli.main(["import-cookies", str(path)]) == 0
    assert len(requests) == 1
    assert local.read("cookies")[0]["domain"] == COOKIE["domain"]
    assert len(local.read("cookies")) == 1
    assert local.read("credentials") is None


def test_failed_import_preserves_previous_state(local, monkeypatch, tmp_path):
    mock_http(monkeypatch, active=False)
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps([COOKIE]))
    local.write("cookies", [{**COOKIE, "value": "previous"}])
    assert cli.main(["import-cookies", str(path)]) == 1
    assert local.read("cookies")[0]["value"] == "previous"


def test_doctor_rejects_untrusted_redirect(local, monkeypatch, capsys):
    requests = mock_http(monkeypatch, redirect="https://evil.example/steal")
    local.write("cookies", [COOKIE])
    assert cli.main(["doctor"]) == 1
    assert len(requests) == 1
    assert "evil.example" not in capsys.readouterr().err


def test_stdio_server_start_has_no_stdout(local, monkeypatch, capsys):
    calls = []
    server = SimpleNamespace(run=lambda **kwargs: calls.append(kwargs))
    monkeypatch.setitem(
        sys.modules,
        "plms_mcp.server",
        SimpleNamespace(create_server=lambda vault: server, create_http_app=None),
    )
    assert cli.main(["serve"]) == 0
    assert calls == [{"transport": "stdio"}]
    assert capsys.readouterr().out == ""


def test_http_requires_configuration_and_disables_proxy_and_access_logs(local, monkeypatch):
    import uvicorn

    calls = []
    app = object()
    monkeypatch.setitem(
        sys.modules, "plms_mcp.server", SimpleNamespace(create_server=None, create_http_app=lambda *args: app)
    )
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert cli.main(["serve", "--transport", "http"]) == 2
    assert calls == []
    monkeypatch.setenv("PLMS_ISSUER_URL", "https://personal.example")
    monkeypatch.setenv("PLMS_CONNECT_PASSWORD", "independent-random-connection-secret")
    assert cli.main(["serve", "--transport", "http"]) == 0
    assert calls[0][0] == (app,)
    assert calls[0][1]["access_log"] is False
    assert calls[0][1]["proxy_headers"] is False
    assert calls[0][1]["workers"] == 1
    assert calls[0][1]["host"] == "127.0.0.1"


def test_explicit_data_dir_wins_over_environment(local, tmp_path):
    other = tmp_path / "explicit"
    assert cli.main(["--data-dir", str(other), "logout"]) == 0
    assert (other / "vault.key").is_file()

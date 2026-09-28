"""Independent regression checks for parser boundaries and persistent auth state."""

import asyncio
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from test_remote_auth import (
    CALLBACK,
    ORIGIN,
    PASSWORD,
    VERIFIER,
    authorize,
    exchange,
    refresh,
    register,
)

from plms_mcp.remote_auth import PersonalOAuthProvider, create_auth


@pytest.fixture
def reviewed_auth(tmp_path):
    provider, routes = create_auth(ORIGIN, PASSWORD, db_path=tmp_path / "oauth.sqlite3")
    with TestClient(Starlette(routes=routes), base_url=ORIGIN, follow_redirects=False) as client:
        yield provider, client
    provider.close()


def test_multipart_cannot_bypass_token_resource_validation(reviewed_auth):
    _, client = reviewed_auth
    registration = register(client).json()
    code = authorize(client, registration)
    fields = {
        "grant_type": "authorization_code",
        "client_id": registration["client_id"],
        "code": code,
        "code_verifier": VERIFIER,
        "redirect_uri": CALLBACK,
        "resource": "https://wrong.example/mcp",
    }
    response = client.post("/token", files={key: (None, value) for key, value in fields.items()})
    assert response.status_code == 415
    assert "access_token" not in response.json()
    # The malformed request must not consume an otherwise valid authorization.
    assert exchange(client, registration, code).status_code == 200


def test_expired_state_pruned_during_refresh_without_new_registration(reviewed_auth):
    provider, client = reviewed_auth
    registration = register(client).json()
    tokens = exchange(client, registration, authorize(client, registration)).json()
    provider._put(
        "spent_refresh", "expired-marker", {"client_id": registration["client_id"]}, time.time() - 10
    )
    assert provider._db.execute("SELECT count(*) FROM state WHERE kind='spent_refresh'").fetchone()[0] == 1
    assert refresh(client, registration, tokens["refresh_token"]).status_code == 200
    assert (
        provider._db.execute("SELECT count(*) FROM state WHERE expires<=?", (time.time(),)).fetchone()[0] == 0
    )


def test_password_kdf_concurrency_is_bounded(tmp_path, monkeypatch):
    provider = PersonalOAuthProvider(ORIGIN, PASSWORD, db_path=tmp_path / "oauth.sqlite3")
    lock = threading.Lock()
    active = maximum = 0

    def slow_derive(password):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.015)
        with lock:
            active -= 1
        return provider._password_hash + b"x" * 32

    monkeypatch.setattr(provider, "_derive", slow_derive)

    async def run_checks():
        return await asyncio.gather(*(provider._password_matches(PASSWORD) for _ in range(12)))

    assert all(asyncio.run(run_checks()))
    assert maximum <= 2
    provider.close()


def test_oauth_database_symlink_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "keep.txt"
    target.write_text("preserve this file")
    link = tmp_path / "oauth.sqlite3"
    link.symlink_to(target)
    with pytest.raises(OSError):
        PersonalOAuthProvider(ORIGIN, PASSWORD, db_path=link)
    assert target.read_text() == "preserve this file"


def test_cancelled_requests_do_not_release_running_kdf_slots(tmp_path, monkeypatch):
    provider = PersonalOAuthProvider(ORIGIN, PASSWORD, db_path=tmp_path / "oauth.sqlite3")
    release = threading.Event()
    lock = threading.Lock()
    active = maximum = 0

    def held_derive(password):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        release.wait(timeout=1)
        with lock:
            active -= 1
        return provider._password_hash + b"x" * 32

    monkeypatch.setattr(provider, "_derive", held_derive)

    async def run_checks():
        original = [asyncio.create_task(provider._password_matches(PASSWORD)) for _ in range(2)]
        for _ in range(100):
            if active == 2:
                break
            await asyncio.sleep(0.001)
        for task in original:
            task.cancel()
        await asyncio.gather(*original, return_exceptions=True)
        newer = [asyncio.create_task(provider._password_matches(PASSWORD)) for _ in range(4)]
        await asyncio.sleep(0.02)
        release.set()
        assert all(await asyncio.gather(*newer))

    asyncio.run(run_checks())
    assert maximum <= 2
    provider.close()


def test_cross_client_revocation_does_not_revoke_owner_grant(reviewed_auth):
    provider, client = reviewed_auth
    owner = register(client).json()
    tokens = exchange(client, owner, authorize(client, owner)).json()
    other = register(client).json()
    response = client.post("/revoke", data={"client_id": other["client_id"], "token": tokens["access_token"]})
    assert response.status_code == 200
    assert asyncio.run(provider.load_access_token(tokens["access_token"])) is not None

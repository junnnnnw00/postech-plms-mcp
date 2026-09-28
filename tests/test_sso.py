"""Synthetic compatibility vectors and offline POSTECH SSO transport checks."""

import json
from urllib.parse import parse_qs

import httpx
import pytest
from Crypto.PublicKey import ECC
from cryptography.hazmat.decrepit.ciphers.algorithms import SEED
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.padding import ANSIX923

from plms_mcp.auth import BASE, SSO, auth_request, auth_url, login_sso, make_envelope
from plms_mcp.errors import LoginFailed, MFARequired, UnsafeURL

# Captured from the prior python-ecdsa implementation before replacement. All
# inputs here are invented; these expected ciphertexts require both X and Y to
# match the original P-256 -> SEED-CBC / ANSI X9.23 browser protocol exactly.
VECTORS = [
    {
        "private": 1,
        "server_x": "7cf27b188d034f7e8a52380304b51ac3c08969e277f21b35a60b48fc47669978",
        "server_y": "07775510db8ed040293d9ac69f7430dbba7dade63ce982299e04b79d227873d1",
        "username": "synthetic-user",
        "password": "synthetic-password",
        "envelope": "1485a430c02487cf465fd41fa8e514cebcf43872a7656155a227fd9d1159b8d5643fca1f0c1c11f94553bd215018f81fc6fc71e58a13c5178b177c6de13da53d",
    },
    {
        "private": 0x123456789ABCDEF0123456789ABCDEF,
        "server_x": "6cd54fb4b96cb0409ccc1d8b032801ad2dba1b5aa81c741dc36dc61491227b20",
        "server_y": "b7ab134246d125d4f2ee4d0b1f4b814fc837daa297235b5fc37f1c2d49f151db",
        "username": "학생테스트",
        "password": "가상 비밀번호 123!",
        "envelope": "478bad5d41601561c87577ecb854ea6e6fd2b8e8835d3952f0caa0f5c2a1653e39c86a5af6325492f732a3ab528c4f4eaf55c7a957995b53f3396fe2b5df54c722af1cd3ccc7a04bd01f9ad2760cd866",
    },
]


@pytest.mark.parametrize("vector", VECTORS)
def test_envelope_matches_pre_migration_synthetic_vector(vector):
    expected = vector["envelope"]
    inputs = {key: value for key, value in vector.items() if key != "envelope"}
    assert make_envelope(**inputs) == expected


@pytest.mark.parametrize(
    "x,y",
    [
        ("0", "0"),
        ("1", "1"),
        ("-1", "1"),
        ("1" * 67, "1"),
        ("not-hex", "1"),
        (None, "1"),
        ("", "1"),
        ("f" * 64, "f" * 64),
    ],
)
def test_invalid_server_point_is_rejected(x, y):
    with pytest.raises(LoginFailed, match="invalid key-exchange"):
        make_envelope(1, x, y, "synthetic", "synthetic")


@pytest.mark.parametrize("private", [0, -1, 2**256])
def test_invalid_private_scalar_is_rejected(private):
    with pytest.raises(LoginFailed, match="invalid key-exchange"):
        make_envelope(private, VECTORS[0]["server_x"], VECTORS[0]["server_y"], "synthetic", "synthetic")


@pytest.mark.parametrize(
    "url",
    [
        "http://plms.postech.ac.kr/",
        "https://plms.postech.ac.kr.evil.example/",
        "https://evil.example/",
        "https://plms.postech.ac.kr:444/",
        "https://user@plms.postech.ac.kr/",
        "https://:pass@plms.postech.ac.kr/",
        "https://plms.postech.ac.kr:invalid/",
        "https://[invalid/",
        "file:///etc/passwd",
    ],
)
def test_auth_url_blocks_non_official_origins_and_userinfo(url):
    with pytest.raises(UnsafeURL):
        auth_url(url)


@pytest.mark.parametrize("status", [302, 307, 308])
async def test_redirect_cannot_send_assertion_or_credentials_off_origin(status):
    requested = []

    def transport(request):
        requested.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://evil.example/collect"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(UnsafeURL):
            await auth_request(
                client, "POST", SSO + "/sso/usr/postech/login/auth", data={"assertion": "synthetic"}
            )
    assert requested == [SSO + "/sso/usr/postech/login/auth"]


async def test_redirect_loop_is_bounded():
    attempts = 0

    def transport(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(302, headers={"Location": "/again"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(LoginFailed, match="Too many"):
            await auth_request(client, "GET", BASE + "/again")
    assert attempts == 12


@pytest.mark.parametrize("mfa", [False, True])
async def test_synthetic_sso_exchange_decrypts_and_preserves_completion_flow(mfa):
    server = ECC.construct(curve="P-256", d=0xABCDEF123456789ABCDEF123456789)
    shared = None
    requested = []

    def transport(request):
        nonlocal shared
        path = request.url.path
        requested.append(path)
        fields = parse_qs(request.content.decode())
        if path == "/passni/sso/spLogin.php":
            return httpx.Response(
                200,
                text=f'<form id="frmSSO" action="{SSO}/entry"><input type="hidden" name="relay" value="synthetic"></form>',
            )
        if path == "/entry":
            assert fields == {"relay": ["synthetic"]}
            return httpx.Response(200, text='<input name="login_key" value="synthetic-key">')
        if path == "/sso/usr/postech/login/init":
            public = fields["user_ec_publickey"][0]
            assert len(public) == 128
            peer = ECC.EccPoint(int(public[:64], 16), int(public[64:], 16), curve="P-256")
            assert not peer.is_point_at_infinity()
            shared = peer * server.d
            return httpx.Response(
                200,
                json={
                    "code": "SS0001",
                    "svr_qx": f"{int(server.pointQ.x):064x}",
                    "svr_qy": f"{int(server.pointQ.y):064x}",
                },
            )
        if path == "/sso/usr/postech/login/auth":
            key = int(shared.x).to_bytes(32, "big")[:16]
            iv = int(shared.y).to_bytes(32, "big")[:16]
            decryptor = Cipher(SEED(key), modes.CBC(iv)).decryptor()
            padded = decryptor.update(bytes.fromhex(fields["user_data"][0])) + decryptor.finalize()
            unpadder = ANSIX923(128).unpadder()
            plain = unpadder.update(padded) + unpadder.finalize()
            assert json.loads(plain) == {"login_id": "synthetic-student", "login_pwd": "가상 비밀번호"}
            assert fields["login_key"] == ["synthetic-key"]
            return httpx.Response(200, json={"code": "SS0030" if mfa else "SS0001"})
        if path == "/sso/usr/postech/login/link":
            return httpx.Response(
                200,
                text=f'<form action="{BASE}/passni/sso/spLoginData.php"><input type="hidden" name="assertion" value="synthetic"></form>',
            )
        if path == "/passni/sso/spLoginData.php":
            return httpx.Response(
                200,
                text="<script>var err_code = ''; var next = '/passni/coursemos/loginBeforeProc.php';</script>",
            )
        if path == "/passni/coursemos/loginBeforeProc.php":
            return httpx.Response(200, text="<script>var endpoint = '/sso/api/cors/get/ip';</script>")
        if path == "/sso/api/cors/get/ip":
            return httpx.Response(200, json={"client_ip": "192.0.2.1"})
        if path == "/passni/coursemos/loginProc.php":
            assert fields == {"pni_client_ip": ["192.0.2.1"]}
            return httpx.Response(200, text='<a href="/login/logout.php">logout</a>')
        raise AssertionError(f"Unexpected synthetic request: {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        if mfa:
            with pytest.raises(MFARequired):
                await login_sso(client, "synthetic-student", "가상 비밀번호")
            assert requested[-1] == "/sso/usr/postech/login/auth"
        else:
            assert await login_sso(client, "synthetic-student", "가상 비밀번호") == {
                "authenticated": True,
                "method": "sso",
            }
            assert requested[-1] == "/passni/coursemos/loginProc.php"

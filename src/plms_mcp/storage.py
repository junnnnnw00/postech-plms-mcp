"""Private, encrypted state. Keys never go into the repository or MCP responses."""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def data_directory() -> Path:
    return Path(os.environ.get("PLMS_DATA_DIR", "~/.local/share/plms-mcp")).expanduser()


class Vault:
    def __init__(self, directory: Path | str | None = None):
        self.directory = Path(directory) if directory else data_directory()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        key_path = self.directory / "vault.key"
        if not key_path.exists():
            try:
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
            except FileExistsError:
                pass
        key_path.chmod(0o600)
        self.cipher = Fernet(key_path.read_bytes())

    def read(self, name: str) -> dict | list | None:
        self._check(name)
        p = self.directory / (name + ".enc")
        if not p.exists():
            return None
        try:
            return json.loads(self.cipher.decrypt(p.read_bytes()))
        except (InvalidToken, ValueError):
            raise ValueError(
                "Private state cannot be decrypted; restore its matching key or reconnect."
            ) from None

    def write(self, name: str, value: dict | list) -> None:
        self._check(name)
        import tempfile

        fd, tmp = tempfile.mkstemp(prefix=".vault-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode()))
            os.replace(tmp, self.directory / (name + ".enc"))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def delete(self, name: str) -> None:
        self._check(name)
        (self.directory / (name + ".enc")).unlink(missing_ok=True)

    def begin_login(self, credentials: dict) -> bool:
        credentials["disabled"] = True
        self.write("credentials", credentials)
        return True

    def complete_login(self, credentials: dict) -> None:
        credentials["disabled"] = False
        self.write("credentials", credentials)

    @staticmethod
    def _check(name: str) -> None:
        if name not in {"credentials", "cookies", "snapshots"}:
            raise ValueError("Unknown private record")

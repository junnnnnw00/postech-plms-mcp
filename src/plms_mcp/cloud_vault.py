"""Explicit opt-in environment credentials and ephemeral PLMS session records.

Only the generation and failed/in-progress attempt identity reach PostgreSQL.
School credentials, cookies and student snapshots stay in process memory.
"""

from __future__ import annotations

import os
import re
import secrets
from copy import deepcopy
from pathlib import Path

from .oauth_store import OAuthStore, StateStoreError
from .storage import Vault


class CloudVault:
    def __init__(self, store, *, username, password, generation, method="sso"):
        if not username.strip() or not password or method not in {"sso", "direct"}:
            raise ValueError("Cloud login credentials or method are invalid")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", generation):
            raise ValueError("PLMS_CREDENTIAL_GENERATION must be a stable, unique non-secret label")
        self.directory = Path("/tmp/plms-mcp-ephemeral")
        self.store = store
        self.generation = generation
        self._credentials = {"username": username.strip(), "password": password, "method": method}
        self._records = {}
        self._attempt = None
        # Repeated boots never overwrite or re-enable an existing generation.
        self.store.initialize_latch(generation)

    @classmethod
    def from_environment(cls):
        if os.environ.get("PLMS_ENV_LOGIN") != "1" or not os.environ.get("DATABASE_URL"):
            raise ValueError("Cloud environment login requires explicit opt-in and persistent PostgreSQL")
        values = {name: os.environ.get(name, "") for name in (
            "PLMS_USERNAME", "PLMS_PASSWORD", "PLMS_CREDENTIAL_GENERATION"
        )}
        if not all(values.values()):
            raise ValueError("Cloud login environment settings are incomplete")
        store = OAuthStore(database_url=os.environ["DATABASE_URL"])
        try:
            return cls(
                store,
                username=values["PLMS_USERNAME"], password=values["PLMS_PASSWORD"],
                generation=values["PLMS_CREDENTIAL_GENERATION"],
                method=os.environ.get("PLMS_LOGIN_METHOD", "sso"),
            )
        except BaseException:
            store.close()
            raise

    def read(self, name):
        Vault._check(name)
        if name == "credentials":
            return {**self._credentials, "disabled": self.store.login_disabled(self.generation)}
        return deepcopy(self._records.get(name))

    def write(self, name, value):
        Vault._check(name)
        if name == "credentials":
            raise ValueError("Cloud credentials must be changed in the host's approved secret settings")
        self._records[name] = deepcopy(value)

    def delete(self, name):
        Vault._check(name)
        if name == "credentials":
            raise ValueError("Cloud credentials must be removed in the host's secret settings")
        self._records.pop(name, None)

    def begin_login(self, credentials):
        attempt = secrets.token_urlsafe(24)
        # This transaction is committed BEFORE any school authentication request.
        # Failure/cancellation/crash never clears the durable claim automatically.
        if not self.store.claim_login(self.generation, attempt):
            return False
        self._attempt = attempt
        return True

    def complete_login(self, credentials):
        if not self._attempt or not self.store.complete_login(self.generation, self._attempt):
            raise StateStoreError("Automatic login remains disabled; reconnect explicitly")
        self._attempt = None

    def close(self):
        self._credentials.clear()
        self._records.clear()
        self.store.close()

"""Where refresh tokens live.

Preference order is the OS keyring, falling back to an owner-only file. The
fallback is not a nicety: headless servers, containers, and plain SSH sessions
routinely have no keyring daemon, and those are exactly the places a CLI earns
its keep. Which backend is live is always reported by ``gmail auth status``.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..config import (
    SECRET_MODE,
    accounts_dir,
    data_dir,
    ensure_secure_dir,
    token_path,
    write_secret_file,
)
from ..errors import AuthError

KEYRING_SERVICE = "gmcli"

# The account index is metadata, not a secret: the keyring cannot enumerate the
# accounts it holds, so we track the list ourselves.
_INDEX_NAME = "accounts.json"


def _index_path() -> Path:
    return data_dir() / _INDEX_NAME


def _read_index() -> dict[str, Any]:
    try:
        return json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"accounts": {}}


def _write_index(index: dict[str, Any]) -> None:
    path = _index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
    tmp.replace(path)


class TokenStore(ABC):
    """Load, save, and delete one account's serialized credentials."""

    name = "unknown"

    @abstractmethod
    def load(self, account: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def save(self, account: str, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    def delete(self, account: str) -> bool: ...

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable location, for ``auth status``."""


class KeyringStore(TokenStore):
    name = "keyring"

    def __init__(self, backend_name: str) -> None:
        self.backend_name = backend_name

    @property
    def description(self) -> str:
        return f"OS keyring ({self.backend_name})"

    def load(self, account: str) -> dict[str, Any] | None:
        import keyring

        raw = keyring.get_password(KEYRING_SERVICE, account)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AuthError(
                f"Stored credentials for {account} are corrupt.",
                hint=f"Run `gmail auth logout --account {account}` then log in again.",
            ) from exc

    def save(self, account: str, payload: dict[str, Any]) -> None:
        import keyring

        keyring.set_password(KEYRING_SERVICE, account, json.dumps(payload))

    def delete(self, account: str) -> bool:
        import keyring
        import keyring.errors

        try:
            keyring.delete_password(KEYRING_SERVICE, account)
            return True
        except keyring.errors.PasswordDeleteError:
            return False


class FileStore(TokenStore):
    name = "file"

    @property
    def description(self) -> str:
        return f"file ({accounts_dir()}, mode 0600)"

    def load(self, account: str) -> dict[str, Any] | None:
        path = token_path(account)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AuthError(
                f"Stored credentials for {account} are corrupt.",
                hint=f"Delete {path} and log in again.",
            ) from exc

    def save(self, account: str, payload: dict[str, Any]) -> None:
        ensure_secure_dir(accounts_dir())
        write_secret_file(token_path(account), json.dumps(payload, indent=2))

    def delete(self, account: str) -> bool:
        path = token_path(account)
        if path.exists():
            path.unlink()
            return True
        return False


_cached_store: TokenStore | None = None


def _keyring_works() -> tuple[bool, str]:
    """Round-trip a probe value to see whether a keyring is genuinely usable.

    Inspecting the backend class is not enough — a backend can be importable
    and still fail at runtime (locked wallet, no D-Bus session). Actually
    writing and reading a value is the only honest test.
    """
    try:
        import keyring
        from keyring.backends import fail as fail_backend

        backend = keyring.get_keyring()
        if isinstance(backend, fail_backend.Keyring):
            return False, "none available"

        probe = "__gmcli_probe__"
        keyring.set_password(KEYRING_SERVICE, probe, "ok")
        value = keyring.get_password(KEYRING_SERVICE, probe)
        try:
            keyring.delete_password(KEYRING_SERVICE, probe)
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        if value != "ok":
            return False, "backend did not return the stored value"
        # Class name alone is ambiguous — several backends are called
        # "Keyring" — so keep enough of the module path to identify it.
        cls = type(backend)
        module = cls.__module__.removeprefix("keyring.backends.")
        return True, f"{module}.{cls.__name__}" if module else cls.__name__
    except Exception as exc:  # noqa: BLE001 - any failure means unusable
        return False, f"{type(exc).__name__}"


def get_store(*, force: str | None = None) -> TokenStore:
    """Return the token store to use, detecting the keyring once per process.

    ``GMCLI_TOKEN_STORE=file|keyring|auto`` overrides detection; ``force``
    overrides that in turn.
    """
    global _cached_store

    choice = (force or os.environ.get("GMCLI_TOKEN_STORE", "auto")).lower()
    if choice == "file":
        return FileStore()
    if choice == "keyring":
        ok, detail = _keyring_works()
        if not ok:
            raise AuthError(
                f"GMCLI_TOKEN_STORE=keyring but no usable keyring was found ({detail}).",
                hint="Unset the variable to fall back to an owner-only file.",
            )
        return KeyringStore(detail)

    if _cached_store is None:
        ok, detail = _keyring_works()
        _cached_store = KeyringStore(detail) if ok else FileStore()
    return _cached_store


# -- account registry --------------------------------------------------------


def register_account(account: str, backend: str) -> None:
    index = _read_index()
    index.setdefault("accounts", {})[account] = {"backend": backend}
    _write_index(index)


def unregister_account(account: str) -> None:
    index = _read_index()
    index.get("accounts", {}).pop(account, None)
    _write_index(index)


def list_accounts() -> list[str]:
    """Every account we have credentials for, sorted.

    The index is the source of truth, but a token file present on disk without
    an index entry still counts — that covers a hand-copied token from another
    machine, which is a documented headless workflow.
    """
    accounts = set(_read_index().get("accounts", {}))
    directory = accounts_dir()
    if directory.exists():
        for path in directory.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            email = payload.get("account") or path.stem
            accounts.add(email)
    return sorted(accounts)


def account_backend(account: str) -> str | None:
    entry = _read_index().get("accounts", {}).get(account)
    return entry.get("backend") if entry else None


def token_file_mode(account: str) -> int | None:
    """Permission bits of the token file, or None when there isn't one."""
    path = token_path(account)
    if not path.exists():
        return None
    return path.stat().st_mode & 0o777


__all__ = [
    "FileStore",
    "KEYRING_SERVICE",
    "KeyringStore",
    "SECRET_MODE",
    "TokenStore",
    "account_backend",
    "get_store",
    "list_accounts",
    "register_account",
    "token_file_mode",
    "unregister_account",
]

"""Token storage: backend selection and file permissions."""

from __future__ import annotations

import stat

import pytest

from gmcli.auth import store as store_mod
from gmcli.config import token_path
from gmcli.errors import AuthError


def test_file_store_roundtrip(isolated_dirs):
    fs = store_mod.FileStore()
    fs.save("me@example.com", {"token": "abc", "refresh_token": "xyz"})
    assert fs.load("me@example.com") == {"token": "abc", "refresh_token": "xyz"}


def test_file_store_writes_owner_only(isolated_dirs):
    fs = store_mod.FileStore()
    fs.save("me@example.com", {"token": "secret"})

    mode = token_path("me@example.com").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600, "token file must not be group/world readable"


def test_file_store_parent_directory_is_owner_only(isolated_dirs):
    fs = store_mod.FileStore()
    fs.save("me@example.com", {"token": "secret"})

    mode = token_path("me@example.com").parent.stat().st_mode
    assert stat.S_IMODE(mode) & 0o077 == 0


def test_file_store_missing_account_returns_none(isolated_dirs):
    assert store_mod.FileStore().load("nobody@example.com") is None


def test_file_store_corrupt_token_is_explained(isolated_dirs):
    fs = store_mod.FileStore()
    fs.save("me@example.com", {"token": "abc"})
    token_path("me@example.com").write_text("{not json")

    with pytest.raises(AuthError, match="corrupt"):
        fs.load("me@example.com")


def test_file_store_delete(isolated_dirs):
    fs = store_mod.FileStore()
    fs.save("me@example.com", {"token": "abc"})
    assert fs.delete("me@example.com") is True
    assert fs.delete("me@example.com") is False


def test_env_var_forces_file_backend(isolated_dirs, monkeypatch):
    monkeypatch.setenv("GMCLI_TOKEN_STORE", "file")
    assert isinstance(store_mod.get_store(), store_mod.FileStore)


def test_falls_back_to_file_when_keyring_unusable(isolated_dirs, monkeypatch):
    monkeypatch.setenv("GMCLI_TOKEN_STORE", "auto")
    monkeypatch.setattr(store_mod, "_cached_store", None)
    monkeypatch.setattr(
        store_mod, "_keyring_works", lambda: (False, "none available")
    )
    assert isinstance(store_mod.get_store(), store_mod.FileStore)


def test_uses_keyring_when_available(isolated_dirs, monkeypatch):
    monkeypatch.setenv("GMCLI_TOKEN_STORE", "auto")
    monkeypatch.setattr(store_mod, "_cached_store", None)
    monkeypatch.setattr(store_mod, "_keyring_works", lambda: (True, "SecretService"))

    resolved = store_mod.get_store()
    assert isinstance(resolved, store_mod.KeyringStore)
    assert "SecretService" in resolved.description


def test_explicit_keyring_request_fails_loudly_when_absent(isolated_dirs, monkeypatch):
    monkeypatch.setattr(store_mod, "_keyring_works", lambda: (False, "no D-Bus"))
    with pytest.raises(AuthError, match="no usable keyring"):
        store_mod.get_store(force="keyring")


def test_account_registry_roundtrip(isolated_dirs):
    store_mod.register_account("a@example.com", "file")
    store_mod.register_account("b@example.com", "keyring")

    assert store_mod.list_accounts() == ["a@example.com", "b@example.com"]
    assert store_mod.account_backend("b@example.com") == "keyring"

    store_mod.unregister_account("a@example.com")
    assert store_mod.list_accounts() == ["b@example.com"]


def test_hand_copied_token_file_is_discovered(isolated_dirs):
    """A token scp'd from another machine counts, index entry or not.

    This is the documented headless workflow, so it has to work.
    """
    store_mod.FileStore().save("copied@example.com", {"token": "t"})
    assert "copied@example.com" in store_mod.list_accounts()

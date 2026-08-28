"""OAuth client resolution: precedence, provenance, and rejections."""

from __future__ import annotations

import json
import time

import pytest

from gmcli.auth import client_config as cc
from gmcli.config import Config, client_secret_path, is_in_download_dir
from gmcli.errors import AuthError


def desktop_payload(client_id: str = "111-abc.apps.googleusercontent.com") -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": "shh",
            "auth_uri": cc.DEFAULT_AUTH_URI,
            "token_uri": cc.DEFAULT_TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def install(payload: dict) -> None:
    path = client_secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# -- parsing -----------------------------------------------------------------


def test_parses_a_desktop_client():
    client = cc.parse_client_file(desktop_payload(), "test")
    assert client.client_id == "111-abc.apps.googleusercontent.com"
    assert client.client_secret == "shh"


def test_web_client_is_rejected_with_an_explanation():
    payload = {"web": {"client_id": "x", "client_secret": "y"}}
    with pytest.raises(AuthError, match="Web application"):
        cc.parse_client_file(payload, "test")


def test_service_account_key_is_rejected():
    payload = {"type": "service_account", "private_key": "..."}
    with pytest.raises(AuthError, match="service-account"):
        cc.parse_client_file(payload, "test")


def test_unrecognised_json_is_rejected():
    with pytest.raises(AuthError, match="does not look like"):
        cc.parse_client_file({"something": "else"}, "test")


def test_missing_client_id_is_rejected():
    with pytest.raises(AuthError, match="no client_id"):
        cc.parse_client_file({"installed": {"client_secret": "y"}}, "test")


def test_secretless_desktop_client_is_allowed():
    """Recent desktop clients can be public, with no secret at all."""
    client = cc.parse_client_file({"installed": {"client_id": "abc"}}, "test")
    assert client.client_secret == ""


def test_flow_config_shape_matches_what_google_expects():
    config = cc.parse_client_file(desktop_payload(), "test").to_flow_config()
    assert set(config) == {"installed"}
    assert config["installed"]["redirect_uris"] == ["http://localhost"]
    assert config["installed"]["token_uri"] == cc.DEFAULT_TOKEN_URI


def test_project_hint_extracts_the_project_number():
    client = cc.parse_client_file(
        desktop_payload("948273610455-xyz.apps.googleusercontent.com"), "t"
    )
    assert client.project_hint == "948273610455"


def test_project_hint_is_blank_for_an_odd_client_id():
    client = cc.parse_client_file(desktop_payload("not-a-google-id"), "t")
    assert client.project_hint == ""


# -- resolution order --------------------------------------------------------


def test_no_client_points_at_the_wizard(isolated_dirs):
    with pytest.raises(AuthError, match="No OAuth client") as excinfo:
        cc.resolve_client()
    assert "gmail auth setup" in excinfo.value.hint


def test_explicit_path_wins(isolated_dirs, tmp_path, monkeypatch):
    monkeypatch.setenv("GMCLI_CLIENT_ID", "env-id")
    install(desktop_payload("installed-id"))

    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps(desktop_payload("explicit-id")))

    assert cc.resolve_client(explicit).client_id == "explicit-id"


def test_env_var_beats_installed_file(isolated_dirs, monkeypatch):
    install(desktop_payload("installed-id"))
    monkeypatch.setenv("GMCLI_CLIENT_ID", "env-id")
    monkeypatch.setenv("GMCLI_CLIENT_SECRET", "env-secret")

    client = cc.resolve_client()
    assert client.client_id == "env-id"
    assert client.client_secret == "env-secret"
    assert "environment" in client.source


def test_installed_file_beats_config(isolated_dirs, monkeypatch):
    monkeypatch.delenv("GMCLI_CLIENT_ID", raising=False)
    install(desktop_payload("installed-id"))

    config = Config()
    config.oauth.client_id = "config-id"

    assert cc.resolve_client(config=config).client_id == "installed-id"


def test_config_is_used_when_no_file_exists(isolated_dirs, monkeypatch):
    monkeypatch.delenv("GMCLI_CLIENT_ID", raising=False)
    config = Config()
    config.oauth.client_id = "config-id"
    config.oauth.client_secret = "config-secret"

    client = cc.resolve_client(config=config)
    assert client.client_id == "config-id"
    assert "config.toml" in client.source


def test_bundled_client_is_the_last_resort(isolated_dirs, monkeypatch):
    monkeypatch.delenv("GMCLI_CLIENT_ID", raising=False)
    monkeypatch.setattr(cc, "BUNDLED_CLIENT_ID", "bundled-id")
    monkeypatch.setattr(cc, "BUNDLED_CLIENT_SECRET", "bundled-secret")

    client = cc.resolve_client(config=Config())
    assert client.client_id == "bundled-id"
    assert "bundled" in client.source


def test_no_client_is_bundled_by_default():
    """A public build must not ship credentials.

    Shipping one would require Google OAuth verification plus a paid annual
    security assessment for the restricted gmail.modify scope, and would cap
    the package at ~100 users. If this ever fails, that decision was made
    deliberately — update the README's setup section to match.
    """
    assert cc.BUNDLED_CLIENT_ID is None
    assert cc.BUNDLED_CLIENT_SECRET is None


def test_config_round_trips_the_oauth_section(isolated_dirs):
    config = Config()
    config.oauth.client_id = "abc"
    config.oauth.client_secret = "def"
    config.save()

    assert Config.load().oauth.client_id == "abc"
    assert Config.load().oauth.client_secret == "def"


# -- download detection ------------------------------------------------------


def test_finds_a_freshly_downloaded_client(tmp_path, monkeypatch):
    from gmcli.commands import setup

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = downloads / "client_secret_123-abc.apps.googleusercontent.com.json"
    target.write_text(json.dumps(desktop_payload()))

    monkeypatch.setattr(setup, "DOWNLOAD_DIRS", (str(downloads),))
    assert setup.find_downloaded_client() == [target.resolve()]


def test_prefers_the_newest_download(tmp_path, monkeypatch):
    from gmcli.commands import setup

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    old = downloads / "client_secret_old.json"
    new = downloads / "client_secret_new.json"
    old.write_text("{}")
    new.write_text("{}")

    now = time.time()
    import os

    os.utime(old, (now - 600, now - 600))
    os.utime(new, (now - 10, now - 10))

    monkeypatch.setattr(setup, "DOWNLOAD_DIRS", (str(downloads),))
    assert setup.find_downloaded_client()[0] == new.resolve()


def test_stale_downloads_are_ignored(tmp_path, monkeypatch):
    from gmcli.commands import setup

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    stale = downloads / "client_secret_ancient.json"
    stale.write_text("{}")

    import os

    old = time.time() - (setup.FRESH_SECONDS + 60)
    os.utime(stale, (old, old))

    monkeypatch.setattr(setup, "DOWNLOAD_DIRS", (str(downloads),))
    assert setup.find_downloaded_client() == []
    # ...unless we explicitly ask for everything.
    assert setup.find_downloaded_client(fresh_only=False) == [stale.resolve()]


def test_unrelated_files_are_not_picked_up(tmp_path, monkeypatch):
    from gmcli.commands import setup

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "invoice.json").write_text("{}")
    (downloads / "notes.txt").write_text("hi")

    monkeypatch.setattr(setup, "DOWNLOAD_DIRS", (str(downloads),))
    assert setup.find_downloaded_client() == []


# -- a downloaded client is moved, not copied --------------------------------


def downloaded(tmp_path, payload: dict | None = None, name: str = "client_secret_1.json"):
    """A client JSON sitting in ``~/Downloads``, as a browser would leave it."""
    downloads = tmp_path / "Downloads"
    downloads.mkdir(exist_ok=True)
    path = downloads / name
    path.write_text(json.dumps(payload or desktop_payload()), encoding="utf-8")
    return path


def test_a_client_in_downloads_is_moved_out_of_it(isolated_dirs):
    from gmcli.auth import flow

    source = downloaded(isolated_dirs)
    result = flow.install_client_secret(source)

    assert result.moved is True
    assert not source.exists()  # the browser's copy is gone
    assert client_secret_path().exists()
    assert cc.load_client_file(client_secret_path()).client_secret == "shh"


def test_the_installed_copy_is_private(isolated_dirs):
    from gmcli.auth import flow

    flow.install_client_secret(downloaded(isolated_dirs))
    assert client_secret_path().stat().st_mode & 0o777 == 0o600


def test_a_client_kept_elsewhere_is_only_copied(isolated_dirs, tmp_path):
    """A path someone chose is a path they meant; deleting it is a surprise."""
    from gmcli.auth import flow

    kept = tmp_path / "dotfiles" / "client.json"
    kept.parent.mkdir()
    kept.write_text(json.dumps(desktop_payload()), encoding="utf-8")

    result = flow.install_client_secret(kept)
    assert result.moved is False
    assert kept.exists()
    assert client_secret_path().exists()


def test_keep_source_leaves_the_download_alone(isolated_dirs):
    from gmcli.auth import flow

    source = downloaded(isolated_dirs)
    result = flow.install_client_secret(source, keep_source=True)
    assert result.moved is False
    assert source.exists()
    assert client_secret_path().exists()


def test_an_invalid_client_is_rejected_before_anything_is_deleted(isolated_dirs):
    """Nothing is removed on a path that never installed anything."""
    from gmcli.auth import flow

    source = downloaded(isolated_dirs, {"web": {"client_id": "x"}})
    with pytest.raises(AuthError):
        flow.install_client_secret(source)
    assert source.exists()
    assert not client_secret_path().exists()


def test_an_unremovable_download_is_reported_not_swallowed(isolated_dirs, monkeypatch):
    """The client still installs; the point is that the user is told."""
    from gmcli.auth import flow

    source = downloaded(isolated_dirs)

    def refuse(self):
        raise OSError("Permission denied")

    monkeypatch.setattr("pathlib.Path.unlink", refuse)
    result = flow.install_client_secret(source)

    assert result.moved is False
    assert result.left_behind == "Permission denied"
    assert client_secret_path().exists()
    levels = [level for level, _ in flow.install_notes(result, "111-abc")]
    assert "warn" in levels


def test_a_second_copy_of_the_same_client_is_reported(isolated_dirs):
    """Browsers name a re-download `… (1).json`, and one left behind is the
    same live credential the move was meant to get rid of."""
    from gmcli.auth import flow

    twin = downloaded(isolated_dirs, name="client_secret_1 (1).json")
    source = downloaded(isolated_dirs)

    result = flow.install_client_secret(source)
    notes = flow.install_notes(result, "111-abc.apps.googleusercontent.com")

    assert twin.exists()  # reported, never deleted
    assert any(level == "warn" and str(twin) in text for level, text in notes)


def test_a_different_client_in_downloads_is_not_mentioned(isolated_dirs):
    from gmcli.auth import flow

    other = downloaded(
        isolated_dirs,
        desktop_payload("999-zzz.apps.googleusercontent.com"),
        name="client_secret_other.json",
    )
    result = flow.install_client_secret(downloaded(isolated_dirs))
    notes = flow.install_notes(result, "111-abc.apps.googleusercontent.com")

    assert other.exists()
    assert not any(str(other) in text for _, text in notes)


def test_the_current_directory_is_not_a_download_folder(isolated_dirs, tmp_path):
    """`.` is searched for a client but never emptied of one."""
    from gmcli.commands import setup

    assert "." in setup.DOWNLOAD_DIRS
    assert not is_in_download_dir(tmp_path / "here" / "client_secret.json")

"""Filesystem locations and the TOML config file.

Paths follow the XDG spec via ``platformdirs`` so config, state, and cache are
separable — the cache directory can be deleted at any time without losing
credentials or settings.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs
import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib

from .errors import ConfigError

APP_NAME = "gmcli"

# Permissions for anything holding secrets: owner read/write only.
SECRET_MODE = 0o600
SECRET_DIR_MODE = 0o700


# Where a browser lands a file. These are transit, not storage: nothing in
# them is where the user meant to keep it, and a directory every download in
# the world passes through is the wrong home for an OAuth client. Anything
# gmcli installs from here it takes *out* of here — see
# ``auth/flow.py:install_client_secret``. The current directory is
# deliberately absent: setup looks there for a downloaded client, but a file
# someone chose to run the command next to is not in transit.
DOWNLOAD_DIRS = ("~/Downloads", "~/Desktop")


def is_in_download_dir(path: Path) -> bool:
    """Whether ``path`` sits directly in a browser's download directory."""
    try:
        parent = path.expanduser().resolve().parent
    except OSError:  # pragma: no cover - unresolvable path
        return False
    for name in DOWNLOAD_DIRS:
        try:
            if parent == Path(name).expanduser().resolve():
                return True
        except OSError:  # pragma: no cover - unresolvable path
            continue
    return False


def config_dir() -> Path:
    return Path(platformdirs.user_config_dir(APP_NAME))


def data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME))


def cache_dir() -> Path:
    return Path(platformdirs.user_cache_dir(APP_NAME))


def config_path() -> Path:
    return config_dir() / "config.toml"


def client_secret_path() -> Path:
    """Where we stash the user's OAuth client so later logins need no flag."""
    return data_dir() / "client_secret.json"


def accounts_dir() -> Path:
    return data_dir() / "accounts"


def token_path(account: str) -> Path:
    return accounts_dir() / f"{_slug(account)}.json"


def _slug(account: str) -> str:
    """Make an email safe to use as a filename without losing distinctness."""
    return "".join(c if c.isalnum() or c in "@.-_" else "_" for c in account)


def ensure_secure_dir(path: Path) -> Path:
    """Create ``path`` (and parents) restricted to the owner."""
    path.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)
    try:
        path.chmod(SECRET_DIR_MODE)
    except OSError:  # pragma: no cover - unusual filesystems
        pass
    return path


def write_secret_file(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` such that it is never briefly world-readable.

    ``os.open`` with the mode set at creation time closes the window that
    ``open()`` + ``chmod()`` would leave open.
    """
    ensure_secure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SECRET_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, SECRET_MODE)


@dataclass
class OutputConfig:
    color: bool = True
    max_results: int = 20


@dataclass
class SendConfig:
    signature: str | None = None
    default_from: str | None = None


@dataclass
class OAuthConfig:
    """An OAuth client kept in the config file rather than a JSON download.

    Handy for containers and dotfile-managed setups, where carrying one TOML
    file is easier than placing a downloaded client_secret.json.
    """

    client_id: str | None = None
    client_secret: str | None = None


@dataclass
class UpdateConfig:
    """Whether gmcli may ask PyPI, once a day, if a newer release exists."""

    check: bool = True


@dataclass
class Config:
    default_account: str | None = None
    output: OutputConfig = field(default_factory=OutputConfig)
    send: SendConfig = field(default_factory=SendConfig)
    oauth: OAuthConfig = field(default_factory=OAuthConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(
                f"{path} is not valid TOML: {exc}",
                hint="Fix the syntax, or delete the file to start from defaults.",
            ) from exc
        out = raw.get("output", {})
        snd = raw.get("send", {})
        oauth = raw.get("oauth", {})
        upd = raw.get("update", {})
        return cls(
            default_account=raw.get("default_account"),
            output=OutputConfig(
                color=bool(out.get("color", True)),
                max_results=int(out.get("max_results", 20)),
            ),
            send=SendConfig(
                signature=snd.get("signature"),
                default_from=snd.get("default_from"),
            ),
            oauth=OAuthConfig(
                client_id=oauth.get("client_id"),
                client_secret=oauth.get("client_secret"),
            ),
            update=UpdateConfig(check=bool(upd.get("check", True))),
            aliases=dict(raw.get("aliases", {})),
        )

    def save(self) -> None:
        data: dict[str, Any] = {}
        if self.default_account:
            data["default_account"] = self.default_account
        data["output"] = {
            "color": self.output.color,
            "max_results": self.output.max_results,
        }
        send = {k: v for k, v in
                {"signature": self.send.signature,
                 "default_from": self.send.default_from}.items() if v}
        if send:
            data["send"] = send
        oauth = {
            k: v
            for k, v in {
                "client_id": self.oauth.client_id,
                "client_secret": self.oauth.client_secret,
            }.items()
            if v
        }
        if oauth:
            data["oauth"] = oauth
        if not self.update.check:
            data["update"] = {"check": False}
        if self.aliases:
            data["aliases"] = self.aliases

        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomli_w.dumps(data), encoding="utf-8")

"""Where the OAuth client comes from.

An OAuth client is not a secret in the usual sense — RFC 8252 states plainly
that an installed app cannot keep one confidential — but it *is* an identity,
and Google ties quota, branding, and abuse reports to it. gmcli therefore
supports several sources and always reports which one is in use, rather than
silently falling back.

Resolution order (first match wins):

1. ``--credentials PATH`` passed on this run
2. ``GMCLI_CLIENT_ID`` / ``GMCLI_CLIENT_SECRET`` in the environment
3. the client installed by ``gmail auth setup`` or a previous ``--credentials``
4. ``[oauth]`` in ``config.toml``
5. a client bundled by whoever built this package (see :data:`BUNDLED_CLIENT_ID`)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Config, client_secret_path
from ..errors import AuthError

# Google's well-known installed-app endpoints. A downloaded client JSON carries
# these too, but a client supplied by env var or config needs the defaults.
DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"

# --------------------------------------------------------------------------
# Bundled client (unset by default)
# --------------------------------------------------------------------------
#
# Filling these in makes `gmail auth login` work with no setup at all. Before
# you do, understand exactly what it commits you to, because gmail.modify is a
# *restricted* scope:
#
#   * Distributing a client that requests restricted scopes to the public
#     requires Google OAuth verification AND a paid, annually-renewed
#     third-party security assessment (CASA).
#   * Until that is complete, the client is capped at roughly 100 users total
#     and every user sees a "Google hasn't verified this app" interstitial.
#   * In "Testing" publishing status, refresh tokens expire after 7 days.
#   * The values below are extractable from any install. Anyone can build a
#     convincing phishing app with your branding, and the abuse reports land
#     on your Cloud project.
#   * All users share one project's API quota.
#
# For a personal build, or an internal Workspace tool where you control the
# org, filling these in is entirely reasonable. For a public PyPI release it
# is not, until verification is done. `gmail auth setup` exists so that the
# per-user alternative takes about two minutes.
BUNDLED_CLIENT_ID: str | None = None
BUNDLED_CLIENT_SECRET: str | None = None


@dataclass(frozen=True)
class ClientConfig:
    """An OAuth client, plus where it came from."""

    client_id: str
    client_secret: str
    source: str
    auth_uri: str = DEFAULT_AUTH_URI
    token_uri: str = DEFAULT_TOKEN_URI

    @property
    def project_hint(self) -> str:
        """The Cloud project number embedded in a Google client id, if present.

        Google client ids look like ``123456789012-abc....apps.googleusercontent.com``
        where the leading digits are the project number. Useful for telling the
        user which project they are actually authenticating against.
        """
        head = self.client_id.split("-", 1)[0]
        return head if head.isdigit() else ""

    def to_flow_config(self) -> dict[str, Any]:
        """The dict shape ``InstalledAppFlow.from_client_config`` expects."""
        return {
            "installed": {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "auth_uri": self.auth_uri,
                "token_uri": self.token_uri,
                # Loopback only. Google removed the out-of-band flow in 2022.
                "redirect_uris": ["http://localhost"],
            }
        }


def parse_client_file(payload: dict[str, Any], source: str) -> ClientConfig:
    """Read a downloaded Google client JSON.

    Accepts the ``installed`` shape and rejects ``web`` and service-account
    keys with an explanation, since picking the wrong client type in the
    console is the single most common setup mistake.
    """
    if payload.get("type") == "service_account":
        raise AuthError(
            f"{source} is a service-account key, not an OAuth client.",
            hint="Service accounts cannot read a personal Gmail mailbox. "
            "Create a client of type 'Desktop app' instead, or run "
            "`gmail auth setup`.",
        )
    if "web" in payload and "installed" not in payload:
        raise AuthError(
            f"{source} is a 'Web application' OAuth client.",
            hint="A web client cannot use the loopback redirect this CLI needs. "
            "Create one of type 'Desktop app' instead, or run `gmail auth setup`.",
        )

    block = payload.get("installed")
    if not isinstance(block, dict):
        raise AuthError(
            f"{source} does not look like an OAuth client file.",
            hint="Download the JSON from your Desktop client in the Google Cloud "
            "Console, or run `gmail auth setup` to be walked through it.",
        )

    client_id = block.get("client_id")
    if not client_id:
        raise AuthError(f"{source} has no client_id.")

    return ClientConfig(
        client_id=client_id,
        # Desktop clients created recently may legitimately have no secret;
        # google-auth accepts an empty one for a public native client.
        client_secret=block.get("client_secret", "") or "",
        source=source,
        auth_uri=block.get("auth_uri") or DEFAULT_AUTH_URI,
        token_uri=block.get("token_uri") or DEFAULT_TOKEN_URI,
    )


def load_client_file(path: Path) -> ClientConfig:
    path = path.expanduser()
    if not path.exists():
        raise AuthError(
            f"No such credentials file: {path}",
            hint="Run `gmail auth setup` to create and install one.",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthError(f"{path} is not valid JSON.") from exc
    return parse_client_file(payload, str(path))


NO_CLIENT_HINT = (
    "Run `gmail auth setup` — it walks you through creating one in about two "
    "minutes, opening each console page for you and picking up the downloaded "
    "file automatically."
)


def resolve_client(
    explicit: Path | None = None, *, config: Config | None = None
) -> ClientConfig:
    """Find an OAuth client, or explain how to get one."""
    if explicit is not None:
        return load_client_file(explicit)

    env_id = os.environ.get("GMCLI_CLIENT_ID")
    if env_id:
        return ClientConfig(
            client_id=env_id,
            client_secret=os.environ.get("GMCLI_CLIENT_SECRET", ""),
            source="GMCLI_CLIENT_ID environment variable",
        )

    installed = client_secret_path()
    if installed.exists():
        client = load_client_file(installed)
        return ClientConfig(
            client_id=client.client_id,
            client_secret=client.client_secret,
            source="installed client (gmail auth setup)",
            auth_uri=client.auth_uri,
            token_uri=client.token_uri,
        )

    config = config if config is not None else Config.load()
    if config.oauth.client_id:
        return ClientConfig(
            client_id=config.oauth.client_id,
            client_secret=config.oauth.client_secret or "",
            source="[oauth] in config.toml",
        )

    if BUNDLED_CLIENT_ID:
        return ClientConfig(
            client_id=BUNDLED_CLIENT_ID,
            client_secret=BUNDLED_CLIENT_SECRET or "",
            source="client bundled with this build",
        )

    raise AuthError("No OAuth client configured.", hint=NO_CLIENT_HINT)


def describe_client_source(config: Config | None = None) -> str:
    """Provenance string for ``auth status``, or a marker when there is none."""
    try:
        return resolve_client(config=config).source
    except AuthError:
        return ""


def has_client(config: Config | None = None) -> bool:
    try:
        resolve_client(config=config)
        return True
    except AuthError:
        return False

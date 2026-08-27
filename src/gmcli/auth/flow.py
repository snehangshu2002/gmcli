"""The OAuth2 installed-app flow.

Scope policy: gmcli requests exactly one scope,

    https://www.googleapis.com/auth/gmail.modify

which covers read, search, send, drafts, labels, archive, trash, and untrash —
and deliberately *cannot* permanently delete mail. Permanent deletion needs
``https://mail.google.com/``, which this tool never asks for. The safety
property is structural rather than a prompt we might later weaken.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..config import client_secret_path, write_secret_file
from ..errors import AuthError
from .client_config import ClientConfig, parse_client_file, resolve_client
from .store import TokenStore, get_store, register_account

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Refresh tokens issued while the Cloud project's consent screen is in
# "Testing" expire after seven days, and the failure surfaces much later as an
# opaque invalid_grant. Detecting the window lets us explain it instead.
TESTING_TOKEN_LIFETIME_DAYS = 7

PUBLISHING_STATUS_HINT = (
    "If your OAuth consent screen is still in 'Testing', Google expires refresh "
    "tokens after 7 days. Set the consent screen to 'In production' in the Google "
    "Cloud Console (an unverified app is fine for personal use — click through the "
    "'Advanced' warning), then run `gmail auth login` again."
)


def install_client_secret(source: Path) -> Path:
    """Copy the user's OAuth client into our data dir so later logins are flagless.

    Validation goes through the same parser the resolver uses, so a web client
    or a service-account key is rejected here with the same explanation rather
    than failing later inside the consent flow.
    """
    source = source.expanduser()
    if not source.exists():
        raise AuthError(
            f"No such credentials file: {source}",
            hint="Run `gmail auth setup` to create and install one.",
        )
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthError(f"{source} is not valid JSON.") from exc

    parse_client_file(payload, str(source))

    dest = client_secret_path()
    write_secret_file(dest, json.dumps(payload, indent=2))
    return dest


def _serialize(creds: Any, account: str) -> dict[str, Any]:
    payload = json.loads(creds.to_json())
    payload["account"] = account
    payload.setdefault("issued_at", time.time())
    return payload


def _deserialize(payload: dict[str, Any]) -> Any:
    from google.oauth2.credentials import Credentials

    data = {k: v for k, v in payload.items() if k not in ("account", "issued_at")}
    return Credentials.from_authorized_user_info(data, SCOPES)


def fetch_account_email(creds: Any) -> str:
    """Ask Gmail who we just authenticated as.

    ``users.getProfile`` is covered by gmail.modify, so this needs no extra
    scope — unlike the userinfo endpoint, which would require adding one.
    """
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    profile = service.users().getProfile(userId="me").execute()
    email = profile.get("emailAddress")
    if not email:
        raise AuthError("Authenticated, but Gmail did not return an address.")
    return email


def login(
    *,
    credentials: Path | None = None,
    port: int = 0,
    store: TokenStore | None = None,
    open_browser: bool = True,
) -> tuple[str, TokenStore, ClientConfig]:
    """Run the loopback consent flow and persist the resulting credentials.

    Google removed the out-of-band (copy-paste) flow in October 2022, so a
    local redirect server is the only option for an installed app. ``port=0``
    picks an ephemeral port; pin one with ``--port`` when tunnelling over SSH.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    if credentials is not None:
        install_client_secret(credentials)

    # Resolving after installing means an explicit --credentials becomes the
    # stored client, and every later login finds it without the flag.
    client = resolve_client()

    flow = InstalledAppFlow.from_client_config(client.to_flow_config(), SCOPES)
    try:
        creds = flow.run_local_server(
            port=port,
            open_browser=open_browser,
            success_message=(
                "gmcli is authorized. You can close this tab and return to the "
                "terminal."
            ),
        )
    except OSError as exc:
        raise AuthError(
            f"Could not start the local redirect server: {exc}",
            hint="Pass --port with a free port, and make sure that exact "
            "http://localhost:PORT URI is listed on the OAuth client.",
        ) from exc

    email = fetch_account_email(creds)
    store = store or get_store()
    store.save(email, _serialize(creds, email))
    register_account(email, store.name)
    return email, store, client


def load_credentials(account: str, *, store: TokenStore | None = None) -> Any:
    """Load stored credentials, refreshing (and re-saving) when expired."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request

    store = store or get_store()
    payload = store.load(account)
    if payload is None:
        raise AuthError(
            f"Not logged in as {account}.",
            hint="Run `gmail auth login`, or `gmail auth list` to see "
            "which accounts are available.",
        )

    creds = _deserialize(payload)
    if creds.valid:
        return creds

    if not creds.refresh_token:
        raise AuthError(
            f"Credentials for {account} cannot be refreshed.",
            hint="Run `gmail auth login` to re-authorize.",
        )

    try:
        creds.refresh(Request())
    except RefreshError as exc:
        raise AuthError(
            f"Could not refresh credentials for {account}: {exc}",
            hint=_refresh_failure_hint(payload),
        ) from exc

    updated = _serialize(creds, account)
    # Preserve the original issue time so age heuristics stay meaningful.
    updated["issued_at"] = payload.get("issued_at", time.time())
    store.save(account, updated)
    return creds


def _refresh_failure_hint(payload: dict[str, Any]) -> str:
    """Explain an invalid_grant rather than dumping it raw.

    Far and away the most common cause is a consent screen left in "Testing".
    A token that dies at roughly seven days old is that bug, not a coincidence.
    """
    issued_at = payload.get("issued_at")
    if issued_at:
        age_days = (time.time() - float(issued_at)) / 86400
        if age_days >= TESTING_TOKEN_LIFETIME_DAYS - 0.5:
            return (
                f"This token is {age_days:.1f} days old. " + PUBLISHING_STATUS_HINT
            )
    return (
        "The token was revoked, the password changed, or the OAuth client was "
        "deleted. Run `gmail auth login` to re-authorize. " + PUBLISHING_STATUS_HINT
    )


def token_age_days(payload: dict[str, Any]) -> float | None:
    issued_at = payload.get("issued_at")
    if not issued_at:
        return None
    return (time.time() - float(issued_at)) / 86400


def logout(account: str, *, store: TokenStore | None = None) -> bool:
    """Forget one account's credentials locally.

    This does not revoke the grant at Google — that is done at
    https://myaccount.google.com/permissions, which ``auth logout`` prints.
    """
    from .store import unregister_account

    store = store or get_store()
    removed = store.delete(account)
    # Clear both backends: a token may predate a keyring becoming available.
    from .store import FileStore

    if not isinstance(store, FileStore):
        removed = FileStore().delete(account) or removed
    unregister_account(account)
    return removed


__all__ = [
    "PUBLISHING_STATUS_HINT",
    "SCOPES",
    "TESTING_TOKEN_LIFETIME_DAYS",
    "fetch_account_email",
    "install_client_secret",
    "load_credentials",
    "login",
    "logout",
    "token_age_days",
]

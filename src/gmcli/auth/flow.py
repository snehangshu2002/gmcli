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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import (
    DOWNLOAD_DIRS,
    client_secret_path,
    is_in_download_dir,
    write_secret_file,
)
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


@dataclass(frozen=True)
class Installed:
    """Where the client went, and what happened to the file it came from."""

    path: Path
    source: Path
    moved: bool = False
    #: Set when the source was in a download directory but could not be removed.
    left_behind: str | None = None

    def __fspath__(self) -> str:
        """So an ``Installed`` is still usable anywhere the path was."""
        return str(self.path)


def install_client_secret(source: Path, *, keep_source: bool = False) -> Installed:
    """Install the user's OAuth client into our data dir, at ``0600``.

    Validation goes through the same parser the resolver uses, so a web client
    or a service-account key is rejected here with the same explanation rather
    than failing later inside the consent flow.

    A client that came out of a download directory is *moved* rather than
    copied: the browser wrote it world-readable into a directory that syncs,
    backs up, and gets shared out of, and a copy left there is a live
    credential nobody is looking after. It is safe to lose because it is not
    lost — the destination is written and fsynced-into-place first, and the
    source is only unlinked once that has succeeded, so a failure at any point
    leaves at least one readable copy. Anywhere else the file is left exactly
    where it is: a path the user typed or keeps in a repo is a path they
    chose, and deleting it would be a surprise rather than a tidy-up.
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

    if keep_source or not is_in_download_dir(source) or source.resolve() == dest:
        return Installed(path=dest, source=source)
    try:
        source.unlink()
    except OSError as exc:
        # Not fatal: the client is installed and works. Say so, because the
        # whole point was that the copy in Downloads should stop existing.
        return Installed(path=dest, source=source, left_behind=str(exc))
    return Installed(path=dest, source=source, moved=True)


def install_notes(installed: "Installed", client_id: str) -> list[tuple[str, str]]:
    """What a front end should say after an install, as ``(level, message)``.

    The text lives here rather than in the commands because both ``auth login``
    and ``auth setup`` install a client and must say the same thing about it;
    the levels keep ``output.py`` in charge of how it looks.
    """
    notes: list[tuple[str, str]] = []
    if installed.moved:
        notes.append(
            (
                "info",
                f"Moved {installed.source} out of your downloads — "
                f"it now lives only at {installed.path} (0600).",
            )
        )
    elif installed.left_behind:
        notes.append(
            (
                "warn",
                f"Could not remove {installed.source} ({installed.left_behind}). "
                "It is a working OAuth client — delete it yourself.",
            )
        )

    leftovers = [
        p for p in other_downloaded_copies(client_id) if p != installed.source
    ]
    if leftovers:
        listed = ", ".join(str(p) for p in leftovers)
        notes.append(
            (
                "warn",
                f"Another copy of this same client is still in your downloads: "
                f"{listed}. gmcli will not delete a file it was not given.",
            )
        )
    return notes


def other_downloaded_copies(client_id: str) -> list[Path]:
    """Client files in the download directories carrying this same client id.

    Re-downloading gives you ``client_secret_… (1).json`` next to the original,
    and moving one of them out solves nothing while its twin is still there.
    These are only reported, never removed: gmcli takes responsibility for the
    file it was handed, not for tidying a directory it does not own.
    """
    found: list[Path] = []
    for name in DOWNLOAD_DIRS:
        base = Path(name).expanduser()
        if not base.is_dir():
            continue
        for path in sorted(base.glob("client_secret*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            block = payload.get("installed")
            if isinstance(block, dict) and block.get("client_id") == client_id:
                found.append(path)
    return found


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
    "Installed",
    "fetch_account_email",
    "install_client_secret",
    "install_notes",
    "load_credentials",
    "login",
    "logout",
    "other_downloaded_copies",
    "token_age_days",
]

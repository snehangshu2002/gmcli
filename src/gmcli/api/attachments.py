"""Attachment download, including the filename safety rules.

Filenames arrive from whoever sent the mail, so they are treated as hostile
input: a part claiming to be ``../../.bashrc`` must land inside the output
directory or not at all.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Iterable

from ..errors import ApiError, NotFoundError
from ..models import Attachment
from .client import GmailClient

# Characters that are illegal or dangerous in a filename on some platform.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f<>:"/\\|?*]')
# Windows reserves these regardless of extension.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_NAME_LEN = 200


def sanitize_filename(name: str, *, fallback: str = "attachment") -> str:
    """Reduce an arbitrary string to a single safe path component.

    Strips directory separators and traversal, control characters, and leading
    dots, and refuses to return anything that is still a path.
    """
    # Take the last component under either separator, so `a/b/../c.txt` -> `c.txt`.
    candidate = name.replace("\\", "/").split("/")[-1]
    candidate = _UNSAFE_CHARS.sub("_", candidate).strip().strip(".")
    candidate = candidate.replace("..", "_")

    if not candidate:
        return fallback
    stem = candidate.split(".")[0].upper()
    if stem in _RESERVED:
        candidate = f"_{candidate}"
    if len(candidate) > MAX_NAME_LEN:
        suffix = Path(candidate).suffix[:16]
        candidate = candidate[: MAX_NAME_LEN - len(suffix)] + suffix
    return candidate


def unique_path(directory: Path, filename: str) -> Path:
    """Return a free path, adding ``(1)``, ``(2)``… rather than overwriting."""
    target = directory / filename
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(1, 1000):
        candidate = directory / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    raise ApiError(f"Could not find a free filename for {filename!r} in {directory}.")


def resolve_output_path(directory: Path, filename: str) -> Path:
    """Sanitize, de-duplicate, and confirm the result stays inside ``directory``."""
    directory = directory.expanduser().resolve()
    safe = sanitize_filename(filename)
    target = unique_path(directory, safe)
    # Belt and braces: even after sanitizing, verify containment.
    if directory not in target.resolve().parents and target.resolve() != directory:
        raise ApiError(
            f"Refusing to write {filename!r}: resolved outside {directory}."
        )
    return target


def fetch_attachment(client: GmailClient, attachment: Attachment) -> bytes:
    """Download one attachment's bytes."""
    response = client.execute(
        client.service.users()
        .messages()
        .attachments()
        .get(
            userId="me",
            messageId=attachment.message_id,
            id=attachment.attachment_id,
        )
    )
    data = response.get("data")
    if data is None:
        raise NotFoundError(f"Gmail returned no data for {attachment.filename!r}.")
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def download(
    client: GmailClient,
    attachments: Iterable[Attachment],
    directory: Path,
) -> list[tuple[Attachment, Path]]:
    """Download attachments into ``directory``, returning what was written."""
    directory = directory.expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    written: list[tuple[Attachment, Path]] = []
    for attachment in attachments:
        payload = fetch_attachment(client, attachment)
        target = resolve_output_path(directory, attachment.filename)
        target.write_bytes(payload)
        written.append((attachment, target))
    return written

"""Resolution of the ``#N`` shorthand into Gmail resource ids.

Gmail ids are 16 hex characters — fine for scripts, hostile to type. Every
listing prints an index column and records the ids it showed, so the next
command can say ``#3`` instead. Full ids always work, so a script is never
coupled to that state.

Accepted forms: ``#3``, ``3``, ``#1-5``, ``#1,3,7``, ``#2-4,9``, and any
literal id. Ranges expand in listing order.
"""

from __future__ import annotations

import re
from typing import Sequence

from .cache import Cache
from .errors import UsageError

# A Gmail message/thread id: lowercase hex, comfortably long.
_ID_RE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
# Anything that is purely digits, commas, hyphens, and an optional leading '#'.
_REF_RE = re.compile(r"^#?[\d]+(?:\s*[-,]\s*[\d]+)*$")


def looks_like_id(token: str) -> bool:
    return bool(_ID_RE.match(token))


def _parse_indices(token: str) -> list[int]:
    """Expand ``1-5,9`` into ``[1,2,3,4,5,9]``, preserving written order."""
    body = token.lstrip("#").strip()
    indices: list[int] = []
    for chunk in body.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start_s, _, end_s = chunk.partition("-")
            try:
                start, end = int(start_s), int(end_s)
            except ValueError as exc:
                raise UsageError(f"Bad reference range: {chunk!r}") from exc
            if start < 1 or end < 1:
                raise UsageError("References start at #1.")
            step = 1 if end >= start else -1
            indices.extend(range(start, end + step, step))
        else:
            try:
                indices.append(int(chunk))
            except ValueError as exc:
                raise UsageError(f"Bad reference: {chunk!r}") from exc
    if any(i < 1 for i in indices):
        raise UsageError("References start at #1.")
    return indices


def resolve(tokens: Sequence[str], cache: Cache) -> list[str]:
    """Turn user-supplied tokens into concrete ids, de-duplicated in order.

    Raises ``UsageError`` with an actionable message when a ``#N`` is used with
    no prior listing, or points past its end — guessing here would silently act
    on the wrong mail.
    """
    if not tokens:
        raise UsageError("No message or thread specified.")

    listing: list[str] | None = None
    resolved: list[str] = []

    for token in tokens:
        token = token.strip()
        if not token:
            continue

        if _REF_RE.match(token) and not looks_like_id(token):
            if listing is None:
                entry = cache.get_listing()
                if entry is None:
                    raise UsageError(
                        f"Cannot resolve {token!r}: no previous listing.",
                        hint="Run `gmail ls` or `gmail search` first, "
                        "or pass the full id.",
                    )
                _, listing = entry
            for index in _parse_indices(token):
                if index > len(listing):
                    raise UsageError(
                        f"#{index} is past the end of the last listing "
                        f"({len(listing)} item{'s' if len(listing) != 1 else ''}).",
                        hint="Re-run the listing to refresh the references.",
                    )
                resolved.append(listing[index - 1])
        else:
            resolved.append(token)

    # De-duplicate while preserving order, so `#1,1` acts once.
    seen: dict[str, None] = {}
    for item in resolved:
        seen.setdefault(item, None)
    return list(seen)


def resolve_one(token: str, cache: Cache) -> str:
    """Resolve a single reference, rejecting anything that expands to many."""
    ids = resolve([token], cache)
    if len(ids) != 1:
        raise UsageError(
            f"{token!r} refers to {len(ids)} items; this command takes one."
        )
    return ids[0]

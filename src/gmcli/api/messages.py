"""Message listing, retrieval, and modification."""

from __future__ import annotations

from typing import Any, Sequence

from ..cache import Cache
from ..models import Message
from .client import BATCH_CHUNK, GmailClient

# Only the headers a listing actually renders. Asking for `format=metadata`
# with an explicit header list keeps a 20-row table from downloading 20 bodies.
LIST_HEADERS = ["From", "To", "Subject", "Date"]


def list_message_ids(
    client: GmailClient,
    *,
    query: str | None = None,
    label_ids: Sequence[str] | None = None,
    limit: int | None = 20,
    include_spam_trash: bool = False,
) -> list[str]:
    ids, _ = list_message_ids_page(
        client,
        query=query,
        label_ids=label_ids,
        limit=limit,
        include_spam_trash=include_spam_trash,
    )
    return ids


def list_message_ids_page(
    client: GmailClient,
    *,
    query: str | None = None,
    label_ids: Sequence[str] | None = None,
    limit: int | None = 20,
    include_spam_trash: bool = False,
    page_token: str | None = None,
) -> tuple[list[str], str | None]:
    """One page of ids, plus the token that fetches the page after it."""
    params: dict[str, Any] = {"includeSpamTrash": include_spam_trash}
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = list(label_ids)

    items, next_token = client.paginate_page(
        client.service.users().messages().list,
        limit=limit,
        items_key="messages",
        page_token=page_token,
        **params,
    )
    return [item["id"] for item in items], next_token


def get_messages_metadata(
    client: GmailClient, message_ids: Sequence[str]
) -> list[Message]:
    """Batch-fetch listing metadata, preserving the requested order."""
    results = client.batch_get(
        lambda mid: client.service.users()
        .messages()
        .get(
            userId="me",
            id=mid,
            format="metadata",
            metadataHeaders=LIST_HEADERS,
        ),
        message_ids,
        chunk_size=BATCH_CHUNK,
    )
    return [Message.from_api(results[mid]) for mid in message_ids if mid in results]


def get_message(
    client: GmailClient,
    message_id: str,
    *,
    fmt: str = "full",
    cache: Cache | None = None,
) -> Message:
    """Fetch one message in full.

    Bodies are immutable once delivered, so a full fetch is cached; label state
    is not, which is why only ``format=full`` payloads are cached and the
    caller refreshes labels separately when it needs them fresh.
    """
    if cache is not None and fmt == "full":
        cached = cache.get_message(message_id)
        if cached is not None:
            return Message.from_api(cached)

    payload = client.execute(
        client.service.users().messages().get(userId="me", id=message_id, format=fmt)
    )
    if cache is not None and fmt == "full":
        cache.set_message(message_id, payload)
    return Message.from_api(payload)


def modify(
    client: GmailClient,
    message_ids: Sequence[str],
    *,
    add: Sequence[str] = (),
    remove: Sequence[str] = (),
) -> int:
    """Add and/or remove labels across many messages.

    ``batchModify`` applies the same change to up to 1000 ids in one request
    and returns no body, so the count is what we report.
    """
    ids = list(message_ids)
    if not ids or (not add and not remove):
        return 0

    body: dict[str, Any] = {"ids": ids}
    if add:
        body["addLabelIds"] = list(add)
    if remove:
        body["removeLabelIds"] = list(remove)

    for i in range(0, len(ids), 1000):
        chunk = dict(body, ids=ids[i : i + 1000])
        client.execute(
            client.service.users().messages().batchModify(userId="me", body=chunk)
        )
    return len(ids)


def trash(client: GmailClient, message_ids: Sequence[str]) -> int:
    """Move messages to Trash — recoverable for 30 days.

    There is no permanent-delete counterpart anywhere in gmcli: the scope we
    request cannot perform one.
    """
    count = 0
    for mid in message_ids:
        client.execute(client.service.users().messages().trash(userId="me", id=mid))
        count += 1
    return count


def untrash(client: GmailClient, message_ids: Sequence[str]) -> int:
    count = 0
    for mid in message_ids:
        client.execute(client.service.users().messages().untrash(userId="me", id=mid))
        count += 1
    return count


def send_raw(
    client: GmailClient, raw: str, *, thread_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"raw": raw}
    if thread_id:
        body["threadId"] = thread_id
    return client.execute(
        client.service.users().messages().send(userId="me", body=body)
    )


def create_draft(
    client: GmailClient, raw: str, *, thread_id: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    return client.execute(
        client.service.users().drafts().create(userId="me", body={"message": message})
    )


def list_drafts(client: GmailClient, *, limit: int | None = 20) -> list[dict[str, Any]]:
    return client.paginate(
        client.service.users().drafts().list, limit=limit, items_key="drafts"
    )


def get_draft(client: GmailClient, draft_id: str) -> dict[str, Any]:
    return client.execute(
        client.service.users().drafts().get(userId="me", id=draft_id, format="full")
    )


def send_draft(client: GmailClient, draft_id: str) -> dict[str, Any]:
    return client.execute(
        client.service.users().drafts().send(userId="me", body={"id": draft_id})
    )


def delete_draft(client: GmailClient, draft_id: str) -> None:
    """Discard a draft. This is a draft, not delivered mail — nothing is lost."""
    client.execute(client.service.users().drafts().delete(userId="me", id=draft_id))

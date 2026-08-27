"""Thread (conversation) listing and modification.

Listings are thread-centric by default because that is how mail actually reads
— a twelve-message back-and-forth is one row, not twelve.
"""

from __future__ import annotations

from typing import Any, Sequence

from ..cache import Cache
from ..models import Message, Thread
from .client import GmailClient
from .messages import LIST_HEADERS


def list_thread_ids(
    client: GmailClient,
    *,
    query: str | None = None,
    label_ids: Sequence[str] | None = None,
    limit: int | None = 20,
    include_spam_trash: bool = False,
) -> list[str]:
    ids, _ = list_thread_ids_page(
        client,
        query=query,
        label_ids=label_ids,
        limit=limit,
        include_spam_trash=include_spam_trash,
    )
    return ids


def list_thread_ids_page(
    client: GmailClient,
    *,
    query: str | None = None,
    label_ids: Sequence[str] | None = None,
    limit: int | None = 20,
    include_spam_trash: bool = False,
    page_token: str | None = None,
) -> tuple[list[str], str | None]:
    """One page of ids, plus the token that fetches the page after it.

    The token is what a browsing front end needs and a one-shot listing does
    not: ``gmail ls -n 50`` wants the newest fifty, the UI wants to be able to
    walk past them.
    """
    params: dict[str, Any] = {"includeSpamTrash": include_spam_trash}
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = list(label_ids)

    items, next_token = client.paginate_page(
        client.service.users().threads().list,
        limit=limit,
        items_key="threads",
        page_token=page_token,
        **params,
    )
    return [item["id"] for item in items], next_token


def get_threads_metadata(
    client: GmailClient, thread_ids: Sequence[str]
) -> list[Thread]:
    """Batch-fetch each thread's messages as metadata only.

    ``threads.get`` with ``format=metadata`` returns every message in the
    conversation with just the requested headers — enough to render
    participants, subject, count, and flags without downloading any bodies.
    """
    results = client.batch_get(
        lambda tid: client.service.users()
        .threads()
        .get(
            userId="me",
            id=tid,
            format="metadata",
            metadataHeaders=LIST_HEADERS,
        ),
        thread_ids,
    )
    return [Thread.from_api(results[tid]) for tid in thread_ids if tid in results]


def get_thread(
    client: GmailClient,
    thread_id: str,
    *,
    fmt: str = "full",
    cache: Cache | None = None,
) -> Thread:
    payload = client.execute(
        client.service.users().threads().get(userId="me", id=thread_id, format=fmt)
    )
    return Thread.from_api(payload)


def modify_threads(
    client: GmailClient,
    thread_ids: Sequence[str],
    *,
    add: Sequence[str] = (),
    remove: Sequence[str] = (),
) -> int:
    """Apply a label change to whole conversations.

    ``threads.modify`` has no batch endpoint, so this issues one request per
    thread — still one per conversation rather than one per message.
    """
    if not thread_ids or (not add and not remove):
        return 0
    body: dict[str, Any] = {}
    if add:
        body["addLabelIds"] = list(add)
    if remove:
        body["removeLabelIds"] = list(remove)

    count = 0
    for tid in thread_ids:
        client.execute(
            client.service.users().threads().modify(userId="me", id=tid, body=body)
        )
        count += 1
    return count


def trash_threads(client: GmailClient, thread_ids: Sequence[str]) -> int:
    count = 0
    for tid in thread_ids:
        client.execute(client.service.users().threads().trash(userId="me", id=tid))
        count += 1
    return count


def untrash_threads(client: GmailClient, thread_ids: Sequence[str]) -> int:
    count = 0
    for tid in thread_ids:
        client.execute(client.service.users().threads().untrash(userId="me", id=tid))
        count += 1
    return count


def latest_message(client: GmailClient, thread_id: str) -> Message | None:
    """The most recent message in a thread — the one a reply should answer."""
    thread = get_thread(client, thread_id, fmt="full")
    return thread.latest

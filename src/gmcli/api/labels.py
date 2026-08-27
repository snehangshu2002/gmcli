"""Label listing, resolution, and mutation.

Gmail's API speaks label *ids*; humans speak label *names*. Every command that
takes a label goes through :class:`LabelIndex`, which caches the mapping (short
TTL — names can be renamed out from under us) and resolves case-insensitively.
"""

from __future__ import annotations

from typing import Any

from ..cache import Cache
from ..errors import NotFoundError, UsageError
from ..models import SYSTEM_LABELS, Label
from .client import GmailClient


def fetch_labels(client: GmailClient, cache: Cache | None = None) -> list[Label]:
    """All labels for the account, system first then user labels A-Z."""
    payloads: list[dict[str, Any]] | None = cache.get_labels() if cache else None
    if payloads is None:
        response = client.execute(client.service.users().labels().list(userId="me"))
        payloads = response.get("labels", []) or []
        if cache:
            cache.set_labels(payloads)

    labels = [Label.from_api(p) for p in payloads]
    labels.sort(key=lambda lb: (lb.type != "system", lb.name.lower()))
    return labels


def fetch_label_details(client: GmailClient, label_ids: list[str]) -> list[Label]:
    """Label list with message/unread counts, which ``labels.list`` omits."""
    results = client.batch_get(
        lambda lid: client.service.users().labels().get(userId="me", id=lid),
        label_ids,
    )
    return [Label.from_api(results[lid]) for lid in label_ids if lid in results]


class LabelIndex:
    """Bidirectional name↔id map with forgiving lookup."""

    def __init__(self, labels: list[Label]) -> None:
        self.labels = labels
        self._by_id = {lb.id: lb for lb in labels}
        self._by_lower_name = {lb.name.lower(): lb for lb in labels}

    @classmethod
    def load(cls, client: GmailClient, cache: Cache | None = None) -> "LabelIndex":
        return cls(fetch_labels(client, cache))

    def name_for(self, label_id: str) -> str:
        label = self._by_id.get(label_id)
        return label.name if label else label_id

    def names_for(self, label_ids: list[str]) -> list[str]:
        return [self.name_for(lid) for lid in label_ids]

    def get(self, name: str) -> Label | None:
        return self._by_lower_name.get(name.strip().lower())

    def resolve(self, name: str) -> str:
        """Name (or id, or system label in any case) to a label id.

        Raises ``NotFoundError`` listing near-misses rather than silently
        creating a label the user did not ask for.
        """
        token = name.strip()
        if not token:
            raise UsageError("Empty label name.")

        label = self._by_lower_name.get(token.lower())
        if label:
            return label.id
        if token in self._by_id:
            return token
        # Bare system names like `inbox` or `unread`.
        if token.upper() in SYSTEM_LABELS:
            return token.upper()

        close = [
            lb.name
            for lb in self.labels
            if token.lower() in lb.name.lower() and lb.type == "user"
        ][:5]
        hint = (
            f"Did you mean: {', '.join(close)}?"
            if close
            else "Run `gmail labels list` to see the available labels, "
            f"or `gmail labels create {token!r}` to make it."
        )
        raise NotFoundError(f"No label named {token!r}.", hint=hint)

    def user_labels(self) -> list[Label]:
        return [lb for lb in self.labels if lb.type == "user"]


def create_label(client: GmailClient, name: str, cache: Cache | None = None) -> Label:
    """Create a user label. ``a/b`` nests ``b`` under ``a``, as in the web UI."""
    body = {
        "name": name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    response = client.execute(
        client.service.users().labels().create(userId="me", body=body)
    )
    if cache:
        cache.invalidate_labels()
    return Label.from_api(response)


def rename_label(
    client: GmailClient, label_id: str, new_name: str, cache: Cache | None = None
) -> Label:
    response = client.execute(
        client.service.users()
        .labels()
        .patch(userId="me", id=label_id, body={"name": new_name})
    )
    if cache:
        cache.invalidate_labels()
    return Label.from_api(response)


def delete_label(
    client: GmailClient, label_id: str, cache: Cache | None = None
) -> None:
    """Remove a label. Messages that carried it are untouched."""
    client.execute(client.service.users().labels().delete(userId="me", id=label_id))
    if cache:
        cache.invalidate_labels()

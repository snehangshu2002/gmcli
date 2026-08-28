"""Shared fixtures.

Everything here is offline. ``FakeService`` mimics the shape of a
googleapiclient resource — ``service.users().messages().get(...).execute()`` —
closely enough that the real code paths run unmodified, including batching.
"""

from __future__ import annotations

import base64
from typing import Any, Callable

import pytest

from gmcli.api.client import GmailClient


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def headers(**kwargs: str) -> list[dict[str, str]]:
    """Build the API's name/value header list from keyword arguments."""
    names = {
        "from_": "From",
        "to": "To",
        "cc": "Cc",
        "subject": "Subject",
        "date": "Date",
        "message_id": "Message-ID",
        "references": "References",
        "reply_to": "Reply-To",
    }
    return [
        {"name": names.get(k, k), "value": v} for k, v in kwargs.items() if v is not None
    ]


def make_message(
    msg_id: str = "abc123def456",
    *,
    thread_id: str | None = None,
    subject: str = "Test subject",
    sender: str = "Dana Whitfield <dana@example.com>",
    to: str = "me@example.com",
    cc: str | None = None,
    body: str = "Hello there.",
    html: str | None = None,
    labels: list[str] | None = None,
    attachments: list[tuple[str, str, int]] | None = None,
    date: str = "Wed, 04 Mar 2026 09:14:00 +0000",
    message_id_header: str = "<parent@mail.example.com>",
    references: str | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    text_part = {
        "mimeType": "text/plain",
        "body": {"data": b64(body), "size": len(body)},
    }
    parts: list[dict[str, Any]] = [text_part]
    if html:
        parts.append(
            {"mimeType": "text/html", "body": {"data": b64(html), "size": len(html)}}
        )
    for i, (filename, mime, size) in enumerate(attachments or [], start=1):
        parts.append(
            {
                "mimeType": mime,
                "filename": filename,
                "body": {"attachmentId": f"att{i}", "size": size},
            }
        )

    # Gmail shapes a single-part message as a bare text/plain payload with the
    # data on the payload itself; only multipart messages carry `parts`.
    payload: dict[str, Any] = {
        "headers": headers(
            from_=sender,
            to=to,
            cc=cc,
            subject=subject,
            date=date,
            message_id=message_id_header,
            references=references,
            reply_to=reply_to,
        ),
    }
    if len(parts) == 1:
        payload["mimeType"] = "text/plain"
        payload["body"] = text_part["body"]
    else:
        payload["mimeType"] = "multipart/mixed"
        payload["body"] = {}
        payload["parts"] = parts

    return {
        "id": msg_id,
        "threadId": thread_id or msg_id,
        "labelIds": labels if labels is not None else ["INBOX", "UNREAD"],
        "snippet": body[:80],
        "internalDate": "1772614440000",
        "sizeEstimate": 4096,
        "payload": payload,
    }


class FakeRequest:
    """Stands in for an HttpRequest. Records the call for assertions."""

    def __init__(self, service: "FakeService", path: str, kwargs: dict[str, Any]):
        self.service = service
        self.path = path
        self.kwargs = kwargs

    def execute(self, num_retries: int = 0) -> Any:
        self.service.calls.append((self.path, self.kwargs))
        handler = self.service.handlers.get(self.path)
        if handler is None:
            raise AssertionError(f"FakeService has no handler for {self.path!r}")
        return handler(self.kwargs) if callable(handler) else handler


class FakeBatch:
    """Mimics BatchHttpRequest: collects requests, runs them on execute()."""

    def __init__(self) -> None:
        self.items: list[tuple[str, FakeRequest, Callable]] = []

    def add(self, request: FakeRequest, request_id: str, callback: Callable) -> None:
        self.items.append((request_id, request, callback))

    def execute(self) -> None:
        for request_id, request, callback in self.items:
            try:
                callback(request_id, request.execute(), None)
            except Exception as exc:  # noqa: BLE001 - mirrors real batch behaviour
                callback(request_id, None, exc)


class _Node:
    """Chainable resource node; leaf calls produce a FakeRequest."""

    def __init__(self, service: "FakeService", path: str) -> None:
        self._service = service
        self._path = path

    def __getattr__(self, name: str) -> Any:
        child = f"{self._path}.{name}" if self._path else name

        def call(**kwargs: Any) -> Any:
            # A node with registered children is a sub-resource; otherwise the
            # call is a leaf method and becomes a request.
            if any(k.startswith(child + ".") for k in self._service.handlers):
                return _Node(self._service, child)
            if child in self._service.handlers:
                return FakeRequest(self._service, child, kwargs)
            return _Node(self._service, child)

        return call


class FakeService:
    """Root of the fake resource tree.

    Register responses by dotted path, e.g.
    ``handlers["users.messages.get"] = lambda kwargs: payload``.
    """

    def __init__(self, handlers: dict[str, Any] | None = None) -> None:
        self.handlers: dict[str, Any] = handlers or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batches: list[FakeBatch] = []

    def users(self) -> _Node:
        return _Node(self, "users")

    def new_batch_http_request(self) -> FakeBatch:
        batch = FakeBatch()
        self.batches.append(batch)
        return batch


@pytest.fixture
def fake_service() -> FakeService:
    return FakeService()


@pytest.fixture
def client(fake_service: FakeService) -> GmailClient:
    return GmailClient(fake_service)


@pytest.fixture
def isolated_dirs(tmp_path, monkeypatch):
    """Point every XDG path at a temp dir so tests never touch real state."""
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        target = tmp_path / sub
        target.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(target))
    # Never let a test reach the developer's real keyring.
    monkeypatch.setenv("GMCLI_TOKEN_STORE", "file")
    # …nor their real home. `~/Downloads` is a path gmcli both reads and
    # *deletes from* (see `auth/flow.py:install_client_secret`), and it does
    # not come from XDG, so redirecting HOME is the only thing between the
    # suite and a developer's actual download folder.
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path

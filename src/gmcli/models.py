"""Typed views over Gmail API resources.

The API returns deeply nested dicts with headers as a list of name/value pairs.
Everything downstream (rendering, JSON output, tests) works with these
dataclasses instead, so payload-shape knowledge lives in exactly one place.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Iterable

# Labels Gmail defines itself; everything else is user-created.
SYSTEM_LABELS = {
    "INBOX", "SENT", "DRAFT", "SPAM", "TRASH", "UNREAD", "STARRED",
    "IMPORTANT", "CHAT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL",
    "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


def _decode(data: str | None) -> bytes:
    """Decode Gmail's base64url payload data, tolerating missing padding."""
    if not data:
        return b""
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _headers_to_dict(headers: Iterable[dict[str, str]]) -> dict[str, str]:
    """Fold the header list into a dict, lowercasing names for lookup.

    Duplicate headers keep the first occurrence, which is what matters for the
    ones we care about (Subject, From, Date).
    """
    out: dict[str, str] = {}
    for h in headers or []:
        name = h.get("name", "").lower()
        if name and name not in out:
            out[name] = h.get("value", "")
    return out


@dataclass(frozen=True)
class Attachment:
    """A non-inline part of a message."""

    message_id: str
    attachment_id: str
    filename: str
    mime_type: str
    size: int
    index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size": self.size,
            "attachment_id": self.attachment_id,
            "message_id": self.message_id,
        }


@dataclass(frozen=True)
class Label:
    id: str
    name: str
    type: str = "user"
    messages_total: int | None = None
    messages_unread: int | None = None

    @property
    def is_system(self) -> bool:
        return self.type == "system"

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "Label":
        return cls(
            id=payload["id"],
            name=payload.get("name", payload["id"]),
            type=payload.get("type", "user"),
            messages_total=payload.get("messagesTotal"),
            messages_unread=payload.get("messagesUnread"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "messages_total": self.messages_total,
            "messages_unread": self.messages_unread,
        }


@dataclass
class Message:
    id: str
    thread_id: str
    label_ids: list[str] = field(default_factory=list)
    snippet: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    internal_date: datetime | None = None
    size_estimate: int | None = None
    raw: bytes | None = None

    # -- header conveniences -------------------------------------------------

    @property
    def subject(self) -> str:
        return self.headers.get("subject", "(no subject)")

    @property
    def sender(self) -> str:
        return self.headers.get("from", "")

    @property
    def sender_name(self) -> str:
        """Display name if the From header has one, else the bare address."""
        name, addr = parseaddr(self.sender)
        return name or addr or self.sender

    @property
    def to(self) -> str:
        return self.headers.get("to", "")

    @property
    def cc(self) -> str:
        return self.headers.get("cc", "")

    @property
    def message_id_header(self) -> str:
        return self.headers.get("message-id", "")

    @property
    def date(self) -> datetime | None:
        """Prefer the Date header; fall back to Gmail's own receipt time."""
        raw = self.headers.get("date")
        if raw:
            try:
                return parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                pass
        return self.internal_date

    # -- label conveniences --------------------------------------------------

    @property
    def is_unread(self) -> bool:
        return "UNREAD" in self.label_ids

    @property
    def is_starred(self) -> bool:
        return "STARRED" in self.label_ids

    @property
    def in_inbox(self) -> bool:
        return "INBOX" in self.label_ids

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "Message":
        msg = cls(
            id=payload["id"],
            thread_id=payload.get("threadId", payload["id"]),
            label_ids=list(payload.get("labelIds", [])),
            snippet=_unescape_snippet(payload.get("snippet", "")),
            size_estimate=payload.get("sizeEstimate"),
        )
        if "internalDate" in payload:
            try:
                msg.internal_date = datetime.fromtimestamp(
                    int(payload["internalDate"]) / 1000, tz=timezone.utc
                )
            except (TypeError, ValueError):
                pass
        if payload.get("raw"):
            msg.raw = _decode(payload["raw"])

        body = payload.get("payload")
        if body:
            msg.headers = _headers_to_dict(body.get("headers", []))
            text, html, atts = _walk_parts(body, msg.id)
            msg.body_text = text
            msg.body_html = html
            msg.attachments = atts
        return msg

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "thread_id": self.thread_id,
            "subject": self.subject,
            "from": self.sender,
            "to": self.to,
            "date": self.date.isoformat() if self.date else None,
            "snippet": self.snippet,
            "labels": self.label_ids,
            "unread": self.is_unread,
            "starred": self.is_starred,
            "has_attachments": self.has_attachments,
        }
        if self.cc:
            data["cc"] = self.cc
        if self.attachments:
            data["attachments"] = [a.to_dict() for a in self.attachments]
        if include_body:
            data["body_text"] = self.body_text
            data["body_html"] = self.body_html
        return data


@dataclass
class Thread:
    """A conversation. ``messages`` may hold only the latest for listings."""

    id: str
    messages: list[Message] = field(default_factory=list)
    snippet: str = ""
    message_count: int = 0

    @property
    def latest(self) -> Message | None:
        return self.messages[-1] if self.messages else None

    @property
    def first(self) -> Message | None:
        return self.messages[0] if self.messages else None

    @property
    def subject(self) -> str:
        m = self.first or self.latest
        return m.subject if m else "(no subject)"

    @property
    def date(self) -> datetime | None:
        m = self.latest
        return m.date if m else None

    @property
    def is_unread(self) -> bool:
        return any(m.is_unread for m in self.messages)

    @property
    def is_starred(self) -> bool:
        return any(m.is_starred for m in self.messages)

    @property
    def has_attachments(self) -> bool:
        return any(m.has_attachments for m in self.messages)

    @property
    def label_ids(self) -> list[str]:
        """Union of labels across the conversation, order preserved."""
        seen: dict[str, None] = {}
        for m in self.messages:
            for lid in m.label_ids:
                seen.setdefault(lid, None)
        return list(seen)

    @property
    def participants(self) -> list[str]:
        """Distinct sender display names, oldest first."""
        seen: dict[str, None] = {}
        for m in self.messages:
            name = m.sender_name
            if name:
                seen.setdefault(name, None)
        return list(seen)

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "Thread":
        msgs = [Message.from_api(m) for m in payload.get("messages", [])]
        return cls(
            id=payload["id"],
            messages=msgs,
            snippet=_unescape_snippet(payload.get("snippet", "")),
            message_count=len(msgs) or int(payload.get("messagesTotal", 0) or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "participants": self.participants,
            "message_count": self.message_count or len(self.messages),
            "date": self.date.isoformat() if self.date else None,
            "snippet": self.snippet,
            "labels": self.label_ids,
            "unread": self.is_unread,
            "starred": self.is_starred,
            "has_attachments": self.has_attachments,
        }


def _unescape_snippet(text: str) -> str:
    """Gmail HTML-escapes snippets; undo the handful of entities it uses."""
    if not text:
        return ""
    import html

    return html.unescape(text)


def _walk_parts(
    part: dict[str, Any], message_id: str
) -> tuple[str | None, str | None, list[Attachment]]:
    """Depth-first walk of the MIME tree.

    Collects the first text/plain and text/html bodies plus every part that
    carries an attachmentId. Inline images referenced by cid: are skipped —
    they are part of the HTML body, not something the user asked to download.

    A node only claims the text slot if it actually carries data: a container
    can share a ``text/*`` mimeType with an empty body, and letting that win
    would mask the real part nested beneath it.
    """
    text: str | None = None
    html_body: str | None = None
    attachments: list[Attachment] = []
    counter = [0]

    def visit(node: dict[str, Any]) -> None:
        nonlocal text, html_body
        mime = (node.get("mimeType") or "").lower()
        body = node.get("body") or {}
        filename = node.get("filename") or ""

        if body.get("attachmentId") and filename:
            headers = _headers_to_dict(node.get("headers", []))
            disposition = headers.get("content-disposition", "")
            is_inline = "inline" in disposition.lower() and "content-id" in headers
            if not is_inline:
                counter[0] += 1
                attachments.append(
                    Attachment(
                        message_id=message_id,
                        attachment_id=body["attachmentId"],
                        filename=filename,
                        mime_type=node.get("mimeType", "application/octet-stream"),
                        size=int(body.get("size", 0) or 0),
                        index=counter[0],
                    )
                )
        elif mime == "text/plain" and text is None and not filename and body.get("data"):
            text = _decode(body["data"]).decode("utf-8", errors="replace")
        elif mime == "text/html" and html_body is None and not filename and body.get("data"):
            html_body = _decode(body["data"]).decode("utf-8", errors="replace")

        for child in node.get("parts", []) or []:
            visit(child)

    visit(part)
    return text, html_body, attachments


# Lines that begin a quoted reply trailer. Matching these lets `gmail read`
# fold the history that every reply drags along.
_QUOTE_MARKERS = (
    re.compile(r"^On .{5,120}\bwrote:\s*$"),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^_{10,}\s*$"),
    re.compile(r"^From:\s.+$"),
)


def split_quoted(body: str) -> tuple[str, str]:
    """Split a plain-text body into (new content, quoted trailer).

    Returns the trailer as an empty string when nothing looks quoted. This is
    heuristic by nature — it errs toward keeping text in the visible half.
    """
    lines = body.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(rx.match(stripped) for rx in _QUOTE_MARKERS):
            # A quote marker only counts if quoted lines actually follow it.
            tail = lines[i:]
            quoted = sum(1 for ln in tail if ln.lstrip().startswith(">"))
            if quoted or stripped.startswith(("--", "__", "From:")):
                return "\n".join(lines[:i]).rstrip(), "\n".join(tail)
    # Fall back to a run of >-prefixed lines reaching the end of the body.
    for i, line in enumerate(lines):
        if line.lstrip().startswith(">"):
            rest = [ln for ln in lines[i:] if ln.strip()]
            if all(ln.lstrip().startswith(">") for ln in rest):
                return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:])
    return body.rstrip(), ""

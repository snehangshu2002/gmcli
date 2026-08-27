"""Outgoing message construction.

Built on :class:`email.message.EmailMessage` rather than the legacy
``MIMEMultipart`` dance, which gets RFC 2047 header encoding and UTF-8 bodies
right without hand-rolling either.

Threading correctness matters more than it looks: if ``In-Reply-To`` and
``References`` are wrong, the reply still arrives but detaches from the
conversation in the recipient's client, and nothing in the send path would tell
you. ``tests/test_compose.py`` covers it directly.
"""

from __future__ import annotations

import base64
import mimetypes
import re
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from pathlib import Path
from typing import Iterable, Sequence

from ..errors import UsageError
from ..models import Message

# "Re:", "RE :", "re:" — but not a subject that merely starts with the word.
_RE_PREFIX = re.compile(r"^\s*re\s*:", re.IGNORECASE)
_FWD_PREFIX = re.compile(r"^\s*fwd?\s*:", re.IGNORECASE)


def split_addresses(values: Sequence[str] | None) -> list[str]:
    """Flatten repeated ``--to`` flags and comma-separated lists into one list.

    ``--to a@x --to "b@x, c@x"`` and ``--to a@x,b@x,c@x`` are equivalent.
    """
    if not values:
        return []
    pairs = getaddresses(list(values))
    out: list[str] = []
    for name, addr in pairs:
        addr = addr.strip()
        if not addr:
            continue
        out.append(str(Address(display_name=name, addr_spec=addr)) if name else addr)
    return out


def validate_addresses(addresses: Iterable[str], *, field: str) -> list[str]:
    """Reject anything that clearly is not an address before we hit the API."""
    checked: list[str] = []
    for entry in addresses:
        _, addr = parseaddr(entry)
        if "@" not in addr or addr.startswith("@") or addr.endswith("@"):
            raise UsageError(f"{field}: {entry!r} is not a valid email address.")
        checked.append(entry)
    return checked


def attach_files(msg: EmailMessage, paths: Sequence[Path]) -> None:
    """Attach files, guessing each part's MIME type from its name."""
    for path in paths:
        path = Path(path).expanduser()
        if not path.exists():
            raise UsageError(f"Attachment not found: {path}")
        if path.is_dir():
            raise UsageError(f"Attachment is a directory: {path}")

        guessed, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype or "octet-stream",
            filename=path.name,
        )


def build_message(
    *,
    to: Sequence[str],
    subject: str,
    body_text: str = "",
    body_html: str | None = None,
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    sender: str | None = None,
    attachments: Sequence[Path] = (),
    in_reply_to: str | None = None,
    references: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> EmailMessage:
    """Assemble a complete outgoing message.

    A message with both text and HTML becomes ``multipart/alternative`` with the
    plain part first, which is what every client expects.
    """
    if not to and not cc and not bcc:
        raise UsageError(
            "No recipients.", hint="Pass at least one --to, --cc, or --bcc."
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    if sender:
        msg["From"] = sender
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if bcc:
        # Gmail strips Bcc before delivery but still uses it for routing.
        msg["Bcc"] = ", ".join(bcc)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    for key, value in (extra_headers or {}).items():
        msg[key] = value

    msg.set_content(body_text or "")
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    if attachments:
        attach_files(msg, list(attachments))
    return msg


def encode(msg: EmailMessage) -> str:
    """base64url-encode a message for the Gmail API's ``raw`` field."""
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


# -- replies -----------------------------------------------------------------


def reply_subject(subject: str) -> str:
    return subject if _RE_PREFIX.match(subject) else f"Re: {subject}"


def forward_subject(subject: str) -> str:
    return subject if _FWD_PREFIX.match(subject) else f"Fwd: {subject}"


def build_references(parent: Message) -> str | None:
    """Chain this reply onto the conversation's existing References list.

    RFC 5322 says References is the parent's References plus the parent's
    Message-ID. Clients use it to rebuild the tree when In-Reply-To alone is
    ambiguous, so both headers get set.
    """
    parent_id = parent.message_id_header.strip()
    existing = parent.headers.get("references", "").strip()
    parts = [p for p in (existing, parent_id) if p]
    return " ".join(parts) or None


def reply_recipients(
    parent: Message, *, reply_all: bool, self_address: str | None
) -> tuple[list[str], list[str]]:
    """Work out To and Cc for a reply.

    Reply-To wins over From when present. ``--all`` additionally carries over
    the original To and Cc, minus our own address so we do not self-copy.
    """
    primary = parent.headers.get("reply-to") or parent.sender
    to = split_addresses([primary])

    cc: list[str] = []
    if reply_all:
        others = split_addresses([parent.to, parent.cc])
        mine = (self_address or "").lower()
        seen = {parseaddr(a)[1].lower() for a in to}
        for entry in others:
            addr = parseaddr(entry)[1].lower()
            if not addr or addr == mine or addr in seen:
                continue
            seen.add(addr)
            cc.append(entry)
    return to, cc


def quote_body(parent: Message, *, body: str | None = None) -> str:
    """Build the ``On <date>, <who> wrote:`` block a reply trails."""
    source = body if body is not None else (parent.body_text or parent.snippet or "")
    quoted = "\n".join(
        f"> {line}" if line else ">" for line in source.strip().splitlines()
    )
    date = parent.date
    when = date.strftime("%a, %d %b %Y at %H:%M") if date else "an earlier date"
    who = parent.sender_name or parent.sender or "someone"
    return f"On {when}, {who} wrote:\n{quoted}"


def build_reply(
    parent: Message,
    *,
    body: str,
    sender: str | None,
    reply_all: bool = False,
    attachments: Sequence[Path] = (),
    quote: bool = True,
    extra_to: Sequence[str] = (),
    extra_cc: Sequence[str] = (),
) -> EmailMessage:
    to, cc = reply_recipients(parent, reply_all=reply_all, self_address=sender)
    to.extend(split_addresses(list(extra_to)))
    cc.extend(split_addresses(list(extra_cc)))

    full_body = body.rstrip()
    if quote:
        full_body = f"{full_body}\n\n{quote_body(parent)}"

    return build_message(
        to=to,
        cc=cc,
        subject=reply_subject(parent.subject),
        body_text=full_body,
        sender=sender,
        attachments=attachments,
        in_reply_to=parent.message_id_header or None,
        references=build_references(parent),
    )


def build_forward(
    parent: Message,
    *,
    to: Sequence[str],
    body: str = "",
    cc: Sequence[str] = (),
    sender: str | None = None,
    attachments: Sequence[Path] = (),
) -> EmailMessage:
    """Forward a message, inlining the original as a quoted block.

    The original's own attachments are not re-attached — re-uploading them
    would need a full download first, and the Gmail web UI's behaviour here is
    not reproducible through the API's ``raw`` field alone. ``gmail attachments
    download`` plus ``-a`` covers that case explicitly.
    """
    date = parent.date
    when = date.strftime("%a, %d %b %Y at %H:%M") if date else ""
    header_block = "\n".join(
        line
        for line in (
            "---------- Forwarded message ----------",
            f"From: {parent.sender}",
            f"Date: {when}" if when else "",
            f"Subject: {parent.subject}",
            f"To: {parent.to}" if parent.to else "",
            f"Cc: {parent.cc}" if parent.cc else "",
        )
        if line
    )
    original = parent.body_text or parent.snippet or ""
    text = f"{body.rstrip()}\n\n{header_block}\n\n{original}".lstrip()

    return build_message(
        to=list(to),
        cc=list(cc),
        subject=forward_subject(parent.subject),
        body_text=text,
        sender=sender,
        attachments=attachments,
    )


def render_preview(msg: EmailMessage) -> str:
    """The exact bytes we would send, as text — what ``--dry-run`` prints."""
    return msg.as_string()


def describe(msg: EmailMessage) -> dict[str, object]:
    """Structured summary of an outgoing message, for ``--json``."""
    attachments = [
        part.get_filename()
        for part in msg.walk()
        if part.get_content_disposition() == "attachment"
    ]
    return {
        "to": msg.get("To", ""),
        "cc": msg.get("Cc", ""),
        "bcc": msg.get("Bcc", ""),
        "from": msg.get("From", ""),
        "subject": msg.get("Subject", ""),
        "in_reply_to": msg.get("In-Reply-To", ""),
        "references": msg.get("References", ""),
        "attachments": [a for a in attachments if a],
        "size_bytes": len(msg.as_bytes()),
    }

"""The only module permitted to write to stdout.

Two contracts hold everywhere:

* Under ``--json`` exactly one JSON document goes to stdout and *all* human
  messaging goes to stderr, so ``gmail search ... --json | jq`` is always clean.
* The JSON key names are public API. ``tests/test_output.py`` locks them.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .models import Attachment, Label, Message, Thread, split_quoted


def format_date(dt: datetime | None, *, now: datetime | None = None) -> str:
    """Compact, column-friendly date.

    Today collapses to a time, this year drops the year, older keeps it — the
    same compression a mail client uses to keep the column narrow.
    """
    if dt is None:
        return ""
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    local_now = now.astimezone()
    if local.date() == local_now.date():
        return local.strftime("%H:%M")
    if local.year == local_now.year:
        return local.strftime("%b %d")
    return local.strftime("%Y-%m-%d")


def format_size(num: int | None) -> str:
    if not num:
        return ""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def _truncate(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


class Renderer:
    """Formats command results for either a human or a machine."""

    def __init__(
        self,
        *,
        json_mode: bool = False,
        color: bool = True,
        quiet: bool = False,
    ) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        # In JSON mode stdout is reserved for the document, so the human
        # console is pointed at stderr for the whole run.
        self.out = Console(
            file=sys.stderr if json_mode else sys.stdout,
            no_color=not color,
            soft_wrap=False,
            highlight=False,
        )
        self.err = Console(file=sys.stderr, no_color=not color, highlight=False)

    # -- machine output ------------------------------------------------------

    def json(self, payload: Any) -> None:
        """Emit the single JSON document for this command."""
        json.dump(payload, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        sys.stdout.flush()

    # -- human messaging (always stderr-safe) --------------------------------

    def info(self, message: str) -> None:
        if not self.quiet:
            self.out.print(message)

    def success(self, message: str) -> None:
        if not self.quiet:
            self.out.print(f"[green]✓[/green] {message}")

    def warn(self, message: str) -> None:
        self.err.print(f"[yellow]warning:[/yellow] {message}")

    def error(self, message: str, hint: str | None = None) -> None:
        self.err.print(f"[red]error:[/red] {message}")
        if hint:
            self.err.print(f"  [dim]{hint}[/dim]")

    # -- listings ------------------------------------------------------------

    def threads(self, threads: Sequence[Thread], *, total_hint: str = "") -> None:
        if self.json_mode:
            self.json([t.to_dict() for t in threads])
            return
        if not threads:
            self.info("[dim]No conversations found.[/dim]")
            return

        table = Table(box=None, pad_edge=False, show_edge=False, expand=True)
        table.add_column("#", justify="right", style="dim", width=3, no_wrap=True)
        table.add_column("", width=2, no_wrap=True)  # attachment / star flags
        table.add_column("From", width=24, no_wrap=True)
        table.add_column("Subject", ratio=1, no_wrap=True)
        table.add_column("Date", width=10, justify="right", no_wrap=True)

        for i, thread in enumerate(threads, start=1):
            unread = thread.is_unread
            style = "bold" if unread else ""
            who = ", ".join(thread.participants) or "(unknown)"
            if thread.message_count > 1:
                who = f"{_truncate(who, 20)} ({thread.message_count})"
            flags = ("★" if thread.is_starred else " ") + (
                "\U0001f4ce" if thread.has_attachments else " "
            )
            table.add_row(
                str(i),
                Text(flags, style="yellow" if thread.is_starred else "dim"),
                Text(_truncate(who, 24), style=style),
                Text(_truncate(thread.subject, 200), style=style),
                Text(format_date(thread.date), style="dim"),
            )
        self.out.print(table)
        if total_hint and not self.quiet:
            self.out.print(f"[dim]{total_hint}[/dim]")

    def messages(self, messages: Sequence[Message], *, total_hint: str = "") -> None:
        if self.json_mode:
            self.json([m.to_dict() for m in messages])
            return
        if not messages:
            self.info("[dim]No messages found.[/dim]")
            return

        table = Table(box=None, pad_edge=False, show_edge=False, expand=True)
        table.add_column("#", justify="right", style="dim", width=3, no_wrap=True)
        table.add_column("", width=2, no_wrap=True)
        table.add_column("From", width=24, no_wrap=True)
        table.add_column("Subject", ratio=1, no_wrap=True)
        table.add_column("Date", width=10, justify="right", no_wrap=True)

        for i, msg in enumerate(messages, start=1):
            style = "bold" if msg.is_unread else ""
            flags = ("★" if msg.is_starred else " ") + (
                "\U0001f4ce" if msg.has_attachments else " "
            )
            table.add_row(
                str(i),
                Text(flags, style="yellow" if msg.is_starred else "dim"),
                Text(_truncate(msg.sender_name, 24), style=style),
                Text(_truncate(msg.subject, 200), style=style),
                Text(format_date(msg.date), style="dim"),
            )
        self.out.print(table)
        if total_hint and not self.quiet:
            self.out.print(f"[dim]{total_hint}[/dim]")

    # -- single message / thread --------------------------------------------

    def message_detail(
        self,
        messages: Sequence[Message],
        *,
        show_quoted: bool = False,
        prefer_html: bool = False,
        headers_only: bool = False,
    ) -> None:
        if self.json_mode:
            self.json([m.to_dict(include_body=not headers_only) for m in messages])
            return

        for idx, msg in enumerate(messages):
            if idx:
                self.out.print()
                self.out.rule(style="dim")
                self.out.print()
            self._message_headers(msg)
            if headers_only:
                continue
            self.out.print()
            body = self._pick_body(msg, prefer_html=prefer_html)
            if body is None:
                self.out.print("[dim](no readable text body)[/dim]")
            else:
                visible, quoted = split_quoted(body)
                self.out.print(Text(visible))
                if quoted:
                    if show_quoted:
                        self.out.print(Text(quoted, style="dim"))
                    else:
                        lines = len(quoted.splitlines())
                        self.out.print(
                            f"\n[dim]… {lines} quoted line"
                            f"{'s' if lines != 1 else ''} hidden "
                            f"(--show-quoted to expand)[/dim]"
                        )
            if msg.attachments:
                self.out.print()
                for att in msg.attachments:
                    self.out.print(
                        f"[cyan]\U0001f4ce [{att.index}] {att.filename}[/cyan] "
                        f"[dim]{att.mime_type}, {format_size(att.size)}[/dim]"
                    )

    def _message_headers(self, msg: Message) -> None:
        self.out.print(f"[bold]{msg.subject}[/bold]")
        self.out.print(f"[dim]From:[/dim] {msg.sender}")
        if msg.to:
            self.out.print(f"[dim]To:[/dim]   {msg.to}")
        if msg.cc:
            self.out.print(f"[dim]Cc:[/dim]   {msg.cc}")
        date = msg.date
        if date:
            self.out.print(
                f"[dim]Date:[/dim] {date.astimezone().strftime('%Y-%m-%d %H:%M %Z')}"
            )
        self.out.print(f"[dim]Id:[/dim]   {msg.id}")

    @staticmethod
    def _pick_body(msg: Message, *, prefer_html: bool) -> str | None:
        if prefer_html and msg.body_html:
            return msg.body_html
        if msg.body_text:
            return msg.body_text
        if msg.body_html:
            return html_to_text(msg.body_html)
        return None

    # -- labels / attachments ------------------------------------------------

    def labels(self, labels: Sequence[Label]) -> None:
        if self.json_mode:
            self.json([lb.to_dict() for lb in labels])
            return
        if not labels:
            self.info("[dim]No labels.[/dim]")
            return
        table = Table(box=None, pad_edge=False, show_edge=False)
        table.add_column("Name", no_wrap=True)
        table.add_column("Type", style="dim", no_wrap=True)
        table.add_column("Total", justify="right", style="dim", no_wrap=True)
        table.add_column("Unread", justify="right", no_wrap=True)
        for lb in labels:
            unread = lb.messages_unread or 0
            table.add_row(
                Text(lb.name, style="dim" if lb.is_system else ""),
                lb.type,
                str(lb.messages_total) if lb.messages_total is not None else "",
                Text(str(unread) if unread else "", style="bold" if unread else "dim"),
            )
        self.out.print(table)

    def attachments(self, attachments: Sequence[Attachment]) -> None:
        if self.json_mode:
            self.json([a.to_dict() for a in attachments])
            return
        if not attachments:
            self.info("[dim]No attachments on this message.[/dim]")
            return
        table = Table(box=None, pad_edge=False, show_edge=False)
        table.add_column("#", justify="right", style="dim", width=3)
        table.add_column("Filename", no_wrap=True)
        table.add_column("Type", style="dim", no_wrap=True)
        table.add_column("Size", justify="right", style="dim", no_wrap=True)
        for att in attachments:
            table.add_row(
                str(att.index), att.filename, att.mime_type, format_size(att.size)
            )
        self.out.print(table)

    # -- generic -------------------------------------------------------------

    def result(self, payload: Any, human: str | None = None) -> None:
        """Report a mutation: JSON document, or a one-line confirmation."""
        if self.json_mode:
            self.json(payload)
        elif human:
            self.success(human)

    def table(self, rows: Iterable[tuple[str, str]], *, title: str = "") -> None:
        """Simple key/value table, used by ``auth status`` and ``doctor``."""
        if title:
            self.out.print(f"[bold]{title}[/bold]")
        table = Table(box=None, pad_edge=False, show_edge=False, show_header=False)
        table.add_column("", style="dim", no_wrap=True)
        table.add_column("")
        for key, value in rows:
            table.add_row(key, value)
        self.out.print(table)


_TAG_RE = None


def html_to_text(html: str) -> str:
    """Crude HTML flattening for messages that ship no text/plain part.

    Deliberately not a full renderer — it drops script/style, turns block
    breaks into newlines, strips tags, and unescapes entities. Good enough to
    read a message; ``--html`` shows the source when it is not.
    """
    global _TAG_RE
    import html as html_mod
    import re

    if _TAG_RE is None:
        _TAG_RE = re.compile(r"<[^>]+>")

    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li)>", "\n", text)
    text = re.sub(r"(?i)<li\b[^>]*>", "  • ", text)
    text = _TAG_RE.sub("", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()

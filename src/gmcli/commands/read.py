"""``gmail read`` — display a conversation or a single message.

Note the naming: ``read`` *shows* mail. Marking something as read is
``gmail mark read``, so the verb is never ambiguous.
"""

from __future__ import annotations

import typer

from ..api import messages as messages_api
from ..api import threads as threads_api
from ..context import AppContext
from ..errors import NotFoundError
from ..idref import resolve_one
from ..models import Message, Thread


def _fetch(app_ctx: AppContext, ref: str) -> tuple[str, Thread | Message]:
    """Resolve a reference to whatever it actually names.

    The last listing tells us whether ``#3`` was a thread or a message. For a
    raw id we try the thread endpoint first and fall back to the message one,
    since the two id spaces are indistinguishable by shape.
    """
    client = app_ctx.client
    cache = app_ctx.cache
    target = resolve_one(ref, cache)

    listing = cache.get_listing()
    kind = listing[0] if listing and target in listing[1] else None

    if kind == "message":
        return "message", messages_api.get_message(client, target, cache=cache)
    if kind == "thread":
        return "thread", threads_api.get_thread(client, target)

    try:
        return "thread", threads_api.get_thread(client, target)
    except NotFoundError:
        return "message", messages_api.get_message(client, target, cache=cache)


def register(app: typer.Typer) -> None:
    @app.command("read")
    def read_cmd(
        ctx: typer.Context,
        ref: str = typer.Argument(
            ..., help="A #N reference from the last listing, or a full id."
        ),
        show_quoted: bool = typer.Option(
            False, "--show-quoted", help="Expand quoted reply history."
        ),
        html: bool = typer.Option(
            False, "--html", help="Show the HTML source instead of the text part."
        ),
        headers_only: bool = typer.Option(
            False, "--headers", help="Show headers only, no body."
        ),
        raw: bool = typer.Option(
            False, "--raw", help="Dump the original RFC 822 source."
        ),
        latest: bool = typer.Option(
            False, "--latest", help="For a thread, show only the newest message."
        ),
        mark_read: bool = typer.Option(
            False, "--mark-read", help="Also mark it as read."
        ),
    ) -> None:
        """Show a conversation or message."""
        app_ctx: AppContext = ctx.obj
        out = app_ctx.renderer

        kind, item = _fetch(app_ctx, ref)

        if raw:
            # --raw is a passthrough of the original source, so it goes to
            # stdout verbatim even under --json.
            target_id = item.latest.id if isinstance(item, Thread) else item.id
            source = messages_api.get_message(app_ctx.client, target_id, fmt="raw")
            typer.echo((source.raw or b"").decode("utf-8", errors="replace"))
            return

        if isinstance(item, Thread):
            msgs = item.messages
            if latest and msgs:
                msgs = msgs[-1:]
        else:
            msgs = [item]

        out.message_detail(
            msgs,
            show_quoted=show_quoted,
            prefer_html=html,
            headers_only=headers_only,
        )

        if mark_read:
            ids = [m.id for m in msgs if m.is_unread]
            if ids:
                messages_api.modify(app_ctx.client, ids, remove=["UNREAD"])
                out.info(f"[dim]Marked {len(ids)} message(s) as read.[/dim]")

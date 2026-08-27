"""``gmail ls`` and ``gmail search`` — the two ways mail gets onto the screen.

Both are the same pipeline: build a Gmail query, page ids, batch-fetch
metadata, render, and record the ids so ``#N`` works next.
"""

from __future__ import annotations

import typer

from ..api import messages as messages_api
from ..api import threads as threads_api
from ..context import AppContext
from ..errors import UsageError


def _record_and_render(
    app_ctx: AppContext,
    *,
    query: str | None,
    label_ids: list[str] | None,
    limit: int,
    as_messages: bool,
    include_spam_trash: bool,
) -> None:
    client = app_ctx.client
    cache = app_ctx.cache
    out = app_ctx.renderer

    if as_messages:
        ids = messages_api.list_message_ids(
            client,
            query=query,
            label_ids=label_ids,
            limit=limit,
            include_spam_trash=include_spam_trash,
        )
        items = messages_api.get_messages_metadata(client, ids)
        cache.set_listing("message", [m.id for m in items])
        out.messages(items, total_hint=_hint(len(items), limit, "message"))
    else:
        ids = threads_api.list_thread_ids(
            client,
            query=query,
            label_ids=label_ids,
            limit=limit,
            include_spam_trash=include_spam_trash,
        )
        items = threads_api.get_threads_metadata(client, ids)
        cache.set_listing("thread", [t.id for t in items])
        out.threads(items, total_hint=_hint(len(items), limit, "conversation"))


def _hint(count: int, limit: int, noun: str) -> str:
    if count == 0:
        return ""
    plural = "" if count == 1 else "s"
    more = "  (use -n to show more)" if count >= limit else ""
    return f"{count} {noun}{plural}{more}"


def build_query(
    raw: str | None,
    *,
    unread: bool,
    starred: bool,
    has_attachment: bool,
    sender: str | None,
    after: str | None,
    before: str | None,
) -> str | None:
    """Compose flag shortcuts into a Gmail search string.

    Flags are additive with any raw query, so
    ``gmail search "from:bob" --unread`` means both.
    """
    parts = [raw.strip()] if raw and raw.strip() else []
    if unread:
        parts.append("is:unread")
    if starred:
        parts.append("is:starred")
    if has_attachment:
        parts.append("has:attachment")
    if sender:
        parts.append(f"from:{sender}")
    if after:
        parts.append(f"after:{after}")
    if before:
        parts.append(f"before:{before}")
    return " ".join(parts) or None


def register(app: typer.Typer) -> None:
    @app.command("ls")
    def ls_cmd(
        ctx: typer.Context,
        limit: int = typer.Option(
            None, "--limit", "-n", help="How many to show. Defaults to 20."
        ),
        label: list[str] = typer.Option(
            None,
            "--label",
            "-l",
            help="Restrict to a label. Repeatable; multiple labels are ANDed.",
        ),
        unread: bool = typer.Option(False, "--unread", "-u", help="Unread only."),
        starred: bool = typer.Option(False, "--starred", "-s", help="Starred only."),
        has_attachment: bool = typer.Option(
            False, "--attachments", help="With attachments only."
        ),
        sender: str = typer.Option(None, "--from", help="From this sender."),
        as_messages: bool = typer.Option(
            False,
            "--messages",
            "-m",
            help="List individual messages instead of conversations.",
        ),
        all_mail: bool = typer.Option(
            False, "--all", help="All mail, not just the inbox."
        ),
    ) -> None:
        """List conversations in your inbox."""
        app_ctx: AppContext = ctx.obj
        count = limit or app_ctx.config.output.max_results

        label_ids: list[str] = []
        if label:
            label_ids = [app_ctx.labels.resolve(name) for name in label]
        elif not all_mail:
            label_ids = ["INBOX"]

        query = build_query(
            None,
            unread=unread,
            starred=starred,
            has_attachment=has_attachment,
            sender=sender,
            after=None,
            before=None,
        )
        _record_and_render(
            app_ctx,
            query=query,
            label_ids=label_ids or None,
            limit=count,
            as_messages=as_messages,
            include_spam_trash=False,
        )

    @app.command("search")
    def search_cmd(
        ctx: typer.Context,
        query: list[str] = typer.Argument(
            None,
            help="Gmail search query. All of Gmail's operators work: "
            'from:, to:, subject:, has:attachment, larger:, after:, "exact phrase".',
        ),
        limit: int = typer.Option(None, "--limit", "-n", help="How many to show."),
        label: list[str] = typer.Option(None, "--label", "-l", help="Restrict to a label."),
        unread: bool = typer.Option(False, "--unread", "-u", help="Unread only."),
        starred: bool = typer.Option(False, "--starred", "-s", help="Starred only."),
        has_attachment: bool = typer.Option(
            False, "--attachments", help="With attachments only."
        ),
        sender: str = typer.Option(None, "--from", help="From this sender."),
        after: str = typer.Option(None, "--after", help="After a date (YYYY/MM/DD)."),
        before: str = typer.Option(None, "--before", help="Before a date (YYYY/MM/DD)."),
        as_messages: bool = typer.Option(
            False, "--messages", "-m", help="List messages instead of conversations."
        ),
        include_spam_trash: bool = typer.Option(
            False, "--all-folders", help="Include Spam and Trash."
        ),
    ) -> None:
        """Search mail using Gmail's own query syntax.

        The query is passed straight to Gmail, so every operator the web UI
        supports works here unchanged. Saved aliases from your config file are
        expanded first.
        """
        app_ctx: AppContext = ctx.obj
        raw = " ".join(query) if query else ""

        # A bare word matching a configured alias expands to the saved query.
        alias = app_ctx.config.aliases.get(raw.strip())
        if alias:
            raw = alias

        full = build_query(
            raw,
            unread=unread,
            starred=starred,
            has_attachment=has_attachment,
            sender=sender,
            after=after,
            before=before,
        )
        if not full:
            raise UsageError(
                "Nothing to search for.",
                hint='Pass a query, e.g. `gmail search "from:bob has:attachment"`, '
                "or use `gmail ls` to browse the inbox.",
            )

        label_ids = [app_ctx.labels.resolve(name) for name in label] if label else None
        _record_and_render(
            app_ctx,
            query=full,
            label_ids=label_ids,
            limit=limit or app_ctx.config.output.max_results,
            as_messages=as_messages,
            include_spam_trash=include_spam_trash,
        )

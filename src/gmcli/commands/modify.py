"""Label, archive, mark, trash, and untrash.

Every mutation here operates on whole conversations by default, matching what
the listing showed. ``--messages`` narrows to individual messages.

There is no permanent-delete command anywhere in gmcli, and there cannot be:
the ``gmail.modify`` scope we request is not permitted to perform one. Trash is
recoverable for 30 days, and ``gmail untrash`` brings things straight back.
"""

from __future__ import annotations

import typer

from ..api import messages as messages_api
from ..api import threads as threads_api
from ..context import AppContext
from ..idref import resolve


def _apply(
    app_ctx: AppContext,
    refs: list[str],
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    as_messages: bool = False,
) -> int:
    ids = resolve(refs, app_ctx.cache)
    if as_messages:
        return messages_api.modify(
            app_ctx.client, ids, add=add or [], remove=remove or []
        )
    return threads_api.modify_threads(
        app_ctx.client, ids, add=add or [], remove=remove or []
    )


def _report(app_ctx: AppContext, count: int, verb: str, unit: str) -> None:
    plural = "" if count == 1 else "s"
    app_ctx.renderer.result(
        {"action": verb, "count": count, "unit": unit},
        f"{verb} {count} {unit}{plural}",
    )


def register(app: typer.Typer) -> None:
    label_app = typer.Typer(
        help="Add or remove labels on mail.", no_args_is_help=True
    )
    app.add_typer(label_app, name="label")

    @label_app.command("add")
    def label_add(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(..., help="#N references or ids."),
        label: str = typer.Option(..., "--label", "-l", help="Label name."),
        as_messages: bool = typer.Option(
            False, "--messages", "-m", help="Act on messages, not conversations."
        ),
        create: bool = typer.Option(
            False, "--create", help="Create the label if it does not exist."
        ),
    ) -> None:
        """Add a label."""
        app_ctx: AppContext = ctx.obj
        label_id = _resolve_or_create(app_ctx, label, create=create)
        count = _apply(app_ctx, refs, add=[label_id], as_messages=as_messages)
        _report(app_ctx, count, "Labelled", "message" if as_messages else "conversation")

    @label_app.command("remove")
    def label_remove(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(..., help="#N references or ids."),
        label: str = typer.Option(..., "--label", "-l", help="Label name."),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Remove a label."""
        app_ctx: AppContext = ctx.obj
        label_id = app_ctx.labels.resolve(label)
        count = _apply(app_ctx, refs, remove=[label_id], as_messages=as_messages)
        _report(
            app_ctx, count, "Unlabelled", "message" if as_messages else "conversation"
        )

    @app.command("archive")
    def archive_cmd(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(..., help="#N references or ids."),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Archive: remove from the inbox, keeping everything else."""
        app_ctx: AppContext = ctx.obj
        count = _apply(app_ctx, refs, remove=["INBOX"], as_messages=as_messages)
        _report(app_ctx, count, "Archived", "message" if as_messages else "conversation")

    @app.command("unarchive")
    def unarchive_cmd(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(..., help="#N references or ids."),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Move back into the inbox."""
        app_ctx: AppContext = ctx.obj
        count = _apply(app_ctx, refs, add=["INBOX"], as_messages=as_messages)
        _report(
            app_ctx, count, "Moved to inbox", "message" if as_messages else "conversation"
        )

    mark_app = typer.Typer(help="Change read and starred state.", no_args_is_help=True)
    app.add_typer(mark_app, name="mark")

    @mark_app.command("read")
    def mark_read(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(...),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Mark as read."""
        app_ctx: AppContext = ctx.obj
        count = _apply(app_ctx, refs, remove=["UNREAD"], as_messages=as_messages)
        _report(app_ctx, count, "Marked read", "message" if as_messages else "conversation")

    @mark_app.command("unread")
    def mark_unread(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(...),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Mark as unread."""
        app_ctx: AppContext = ctx.obj
        count = _apply(app_ctx, refs, add=["UNREAD"], as_messages=as_messages)
        _report(
            app_ctx, count, "Marked unread", "message" if as_messages else "conversation"
        )

    @mark_app.command("star")
    def mark_star(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(...),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Star."""
        app_ctx: AppContext = ctx.obj
        count = _apply(app_ctx, refs, add=["STARRED"], as_messages=as_messages)
        _report(app_ctx, count, "Starred", "message" if as_messages else "conversation")

    @mark_app.command("unstar")
    def mark_unstar(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(...),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Remove a star."""
        app_ctx: AppContext = ctx.obj
        count = _apply(app_ctx, refs, remove=["STARRED"], as_messages=as_messages)
        _report(app_ctx, count, "Unstarred", "message" if as_messages else "conversation")

    @app.command("trash")
    def trash_cmd(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(..., help="#N references or ids."),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Move to Trash. Recoverable for 30 days with `gmail untrash`."""
        app_ctx: AppContext = ctx.obj
        ids = resolve(refs, app_ctx.cache)
        if as_messages:
            count = messages_api.trash(app_ctx.client, ids)
        else:
            count = threads_api.trash_threads(app_ctx.client, ids)
        _report(app_ctx, count, "Trashed", "message" if as_messages else "conversation")

    @app.command("untrash")
    def untrash_cmd(
        ctx: typer.Context,
        refs: list[str] = typer.Argument(..., help="#N references or ids."),
        as_messages: bool = typer.Option(False, "--messages", "-m"),
    ) -> None:
        """Restore from Trash."""
        app_ctx: AppContext = ctx.obj
        ids = resolve(refs, app_ctx.cache)
        if as_messages:
            count = messages_api.untrash(app_ctx.client, ids)
        else:
            count = threads_api.untrash_threads(app_ctx.client, ids)
        _report(app_ctx, count, "Restored", "message" if as_messages else "conversation")


def _resolve_or_create(app_ctx: AppContext, name: str, *, create: bool) -> str:
    from ..api.labels import create_label
    from ..errors import NotFoundError

    try:
        return app_ctx.labels.resolve(name)
    except NotFoundError:
        if not create:
            raise
        label = create_label(app_ctx.client, name, app_ctx.cache)
        app_ctx.renderer.info(f"[dim]Created label {label.name}[/dim]")
        return label.id

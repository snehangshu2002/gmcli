"""``gmail attachments …`` — list and download attachments."""

from __future__ import annotations

import fnmatch
from pathlib import Path

import typer

from ..api import attachments as attachments_api
from ..api import messages as messages_api
from ..api import threads as threads_api
from ..context import AppContext
from ..errors import NotFoundError, UsageError
from ..idref import resolve_one
from ..models import Attachment

app = typer.Typer(help="List and download attachments.", no_args_is_help=True)


def _collect(app_ctx: AppContext, ref: str) -> list[Attachment]:
    """Every attachment on a message, or across a whole conversation.

    Indices are renumbered across the conversation so ``--index 3`` means the
    third attachment the listing showed, not the third on some message.
    """
    target = resolve_one(ref, app_ctx.cache)

    listing = app_ctx.cache.get_listing()
    kind = listing[0] if listing and target in listing[1] else None

    items: list[Attachment] = []
    if kind == "message":
        msg = messages_api.get_message(app_ctx.client, target, cache=app_ctx.cache)
        items = list(msg.attachments)
    else:
        try:
            thread = threads_api.get_thread(app_ctx.client, target)
            for msg in thread.messages:
                items.extend(msg.attachments)
        except NotFoundError:
            msg = messages_api.get_message(app_ctx.client, target, cache=app_ctx.cache)
            items = list(msg.attachments)

    return [
        Attachment(
            message_id=att.message_id,
            attachment_id=att.attachment_id,
            filename=att.filename,
            mime_type=att.mime_type,
            size=att.size,
            index=i,
        )
        for i, att in enumerate(items, start=1)
    ]


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="#N reference or id."),
) -> None:
    """List the attachments on a message or conversation."""
    app_ctx: AppContext = ctx.obj
    app_ctx.renderer.attachments(_collect(app_ctx, ref))


@app.command("download")
def download_cmd(
    ctx: typer.Context,
    ref: str = typer.Argument(..., help="#N reference or id."),
    all_files: bool = typer.Option(False, "--all", help="Download every attachment."),
    index: list[int] = typer.Option(
        None, "--index", "-i", help="Download by index from `attachments list`."
    ),
    name: str = typer.Option(
        None, "--name", help="Download attachments matching a glob, e.g. '*.pdf'."
    ),
    out_dir: Path = typer.Option(
        Path("."), "--out", "-o", help="Directory to write into."
    ),
) -> None:
    """Download attachments.

    Filenames from the message are sanitized before use and collisions get a
    numeric suffix, so nothing is silently overwritten and nothing escapes the
    output directory.
    """
    app_ctx: AppContext = ctx.obj
    out = app_ctx.renderer

    available = _collect(app_ctx, ref)
    if not available:
        raise NotFoundError("That message has no attachments.")

    if all_files:
        selected = available
    elif index:
        by_index = {att.index: att for att in available}
        missing = [i for i in index if i not in by_index]
        if missing:
            raise UsageError(
                f"No attachment with index {', '.join(map(str, missing))}. "
                f"Available: 1–{len(available)}."
            )
        selected = [by_index[i] for i in index]
    elif name:
        selected = [
            att for att in available if fnmatch.fnmatch(att.filename.lower(), name.lower())
        ]
        if not selected:
            raise NotFoundError(f"No attachment matching {name!r}.")
    elif len(available) == 1:
        # Unambiguous: just take it.
        selected = available
    else:
        raise UsageError(
            f"That message has {len(available)} attachments.",
            hint="Pass --all, --index N, or --name '*.pdf' to choose.",
        )

    written = attachments_api.download(app_ctx.client, selected, out_dir)

    if app_ctx.json_mode:
        out.json(
            [
                {**att.to_dict(), "path": str(path.resolve())}
                for att, path in written
            ]
        )
        return
    for att, path in written:
        out.success(f"{att.filename} → {path}")

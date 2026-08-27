"""``gmail draft …`` — compose now, send later."""

from __future__ import annotations

from pathlib import Path

import typer

from ..api import compose
from ..api import messages as messages_api
from ..context import AppContext
from ..models import Message
from .send import resolve_body

app = typer.Typer(help="Create and manage drafts.", no_args_is_help=True)


@app.command("create")
def create_cmd(
    ctx: typer.Context,
    to: list[str] = typer.Option(None, "--to", "-t", help="Recipient."),
    subject: str = typer.Option("", "--subject", "-s"),
    body: str = typer.Option(None, "--body", "-b"),
    body_file: Path = typer.Option(None, "--body-file", "-F"),
    cc: list[str] = typer.Option(None, "--cc"),
    bcc: list[str] = typer.Option(None, "--bcc"),
    attach: list[Path] = typer.Option(None, "--attach", "-a"),
) -> None:
    """Save a draft without sending it."""
    app_ctx: AppContext = ctx.obj

    recipients = compose.validate_addresses(compose.split_addresses(to), field="--to")
    cc_list = compose.validate_addresses(compose.split_addresses(cc), field="--cc")
    bcc_list = compose.validate_addresses(compose.split_addresses(bcc), field="--bcc")

    header_stub = "\n".join(
        f"# {k}: {v}"
        for k, v in (("To", ", ".join(recipients)), ("Subject", subject))
        if v
    )
    text = resolve_body(body=body, body_file=body_file, editor_headers=header_stub)

    msg = compose.build_message(
        to=recipients or ["undisclosed-recipients:;"],
        cc=cc_list,
        bcc=bcc_list,
        subject=subject,
        body_text=text,
        sender=app_ctx.config.send.default_from,
        attachments=list(attach or []),
    )
    result = messages_api.create_draft(app_ctx.client, compose.encode(msg))
    app_ctx.renderer.result(
        {"draft_id": result.get("id"), **compose.describe(msg)},
        f"Draft saved ({result.get('id')})",
    )


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """List saved drafts."""
    app_ctx: AppContext = ctx.obj
    out = app_ctx.renderer

    drafts = messages_api.list_drafts(app_ctx.client, limit=limit)
    if not drafts:
        if app_ctx.json_mode:
            out.json([])
        else:
            out.info("[dim]No drafts.[/dim]")
        return

    # drafts.list returns ids only; fetch each one's headers to show anything
    # useful. Drafts are few, so this stays cheap.
    rows = []
    for draft in drafts:
        detail = messages_api.get_draft(app_ctx.client, draft["id"])
        msg = Message.from_api(detail["message"])
        rows.append({"draft_id": draft["id"], **msg.to_dict()})

    if app_ctx.json_mode:
        out.json(rows)
        return

    out.table(
        [
            (row["draft_id"], f"{row['to'] or '(no recipient)'} — {row['subject']}")
            for row in rows
        ],
        title="Drafts",
    )


@app.command("show")
def show_cmd(ctx: typer.Context, draft_id: str) -> None:
    """Show a draft's contents."""
    app_ctx: AppContext = ctx.obj
    detail = messages_api.get_draft(app_ctx.client, draft_id)
    msg = Message.from_api(detail["message"])
    app_ctx.renderer.message_detail([msg], show_quoted=True)


@app.command("send")
def send_cmd(ctx: typer.Context, draft_id: str) -> None:
    """Send a saved draft."""
    app_ctx: AppContext = ctx.obj
    result = messages_api.send_draft(app_ctx.client, draft_id)
    app_ctx.renderer.result(
        {"id": result.get("id"), "thread_id": result.get("threadId")},
        f"Sent draft {draft_id}",
    )


@app.command("delete")
def delete_cmd(
    ctx: typer.Context,
    draft_id: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Discard a draft."""
    app_ctx: AppContext = ctx.obj
    if not yes and not app_ctx.json_mode:
        if not typer.confirm(f"Discard draft {draft_id}?"):
            app_ctx.renderer.info("Cancelled.")
            raise typer.Exit(code=0)
    messages_api.delete_draft(app_ctx.client, draft_id)
    app_ctx.renderer.result({"deleted": draft_id}, f"Discarded draft {draft_id}")

"""``gmail send``, ``reply``, and ``forward``.

Body text can arrive three ways, checked in this order: an explicit
``--body``/``--body-file``, piped stdin, or — when stdout is a terminal and
none of those were given — ``$EDITOR``. That covers scripting and interactive
use through one code path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import typer

from ..api import compose
from ..api import messages as messages_api
from ..api import threads as threads_api
from ..context import AppContext
from ..errors import UsageError

EDITOR_TEMPLATE = """\
{headers}
# Everything above the blank line is headers; edit them if you like.
# Lines starting with '#' are removed. Save an empty body to abort.

"""


def _editor_command() -> list[str]:
    editor = os.environ.get("GMCLI_EDITOR") or os.environ.get("EDITOR")
    if not editor:
        # Fall back to something almost certainly present rather than failing.
        for candidate in ("nano", "vi"):
            from shutil import which

            if which(candidate):
                editor = candidate
                break
    if not editor:
        raise UsageError(
            "No body given and no $EDITOR set.",
            hint="Pass --body, --body-file, pipe the body on stdin, or set $EDITOR.",
        )
    import shlex

    return shlex.split(editor)


def compose_in_editor(initial: str = "", *, headers: str = "") -> str:
    """Open $EDITOR and return the body the user wrote.

    Comment lines are stripped so the instructions never end up in the mail.
    """
    template = EDITOR_TEMPLATE.format(headers=headers) if headers else ""
    with tempfile.NamedTemporaryFile(
        mode="w+", suffix=".gmail.md", encoding="utf-8", delete=False
    ) as fh:
        fh.write(template + initial)
        path = Path(fh.name)

    try:
        subprocess.run([*_editor_command(), str(path)], check=True)
        content = path.read_text(encoding="utf-8")
    except subprocess.CalledProcessError as exc:
        raise UsageError(f"Editor exited with status {exc.returncode}.") from exc
    finally:
        path.unlink(missing_ok=True)

    body = "\n".join(
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    ).strip()
    if not body:
        raise UsageError("Empty body — nothing sent.")
    return body


def resolve_body(
    *,
    body: str | None,
    body_file: Path | None,
    editor_headers: str = "",
    allow_editor: bool = True,
    initial: str = "",
) -> str:
    """Pick the body from flags, stdin, or the editor — in that order."""
    if body is not None and body_file is not None:
        raise UsageError("Pass either --body or --body-file, not both.")
    if body is not None:
        return body
    if body_file is not None:
        path = body_file.expanduser()
        if str(path) == "-":
            return sys.stdin.read()
        if not path.exists():
            raise UsageError(f"No such file: {path}")
        return path.read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            return piped
    if allow_editor and sys.stdout.isatty():
        return compose_in_editor(initial, headers=editor_headers)
    raise UsageError(
        "No message body.",
        hint="Pass --body TEXT, --body-file FILE, or pipe the body on stdin.",
    )


def _finish(
    app_ctx: AppContext,
    msg,
    *,
    dry_run: bool,
    thread_id: str | None,
    verb: str,
) -> None:
    """Either print the assembled MIME, or actually send it."""
    out = app_ctx.renderer
    if dry_run:
        summary = compose.describe(msg)
        if app_ctx.json_mode:
            out.json({"dry_run": True, **summary})
        else:
            out.info("[yellow]--dry-run: nothing was sent.[/yellow]\n")
            typer.echo(compose.render_preview(msg))
        return

    result = messages_api.send_raw(
        app_ctx.client, compose.encode(msg), thread_id=thread_id
    )
    out.result(
        {
            "id": result.get("id"),
            "thread_id": result.get("threadId"),
            **compose.describe(msg),
        },
        f"{verb} to {msg.get('To', '')}",
    )


def register(app: typer.Typer) -> None:
    @app.command("send")
    def send_cmd(
        ctx: typer.Context,
        to: list[str] = typer.Option(
            None, "--to", "-t", help="Recipient. Repeatable, or comma-separated."
        ),
        subject: str = typer.Option("", "--subject", "-s", help="Subject line."),
        body: str = typer.Option(None, "--body", "-b", help="Message body."),
        body_file: Path = typer.Option(
            None, "--body-file", "-F", help="Read the body from a file, or '-' for stdin."
        ),
        cc: list[str] = typer.Option(None, "--cc", help="Cc recipient."),
        bcc: list[str] = typer.Option(None, "--bcc", help="Bcc recipient."),
        html_file: Path = typer.Option(
            None, "--html", help="Attach an HTML alternative part from this file."
        ),
        attach: list[Path] = typer.Option(
            None, "--attach", "-a", help="Attach a file. Repeatable."
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Print the assembled message; send nothing."
        ),
    ) -> None:
        """Send a message."""
        app_ctx: AppContext = ctx.obj

        recipients = compose.validate_addresses(
            compose.split_addresses(to), field="--to"
        )
        cc_list = compose.validate_addresses(compose.split_addresses(cc), field="--cc")
        bcc_list = compose.validate_addresses(
            compose.split_addresses(bcc), field="--bcc"
        )
        if not (recipients or cc_list or bcc_list):
            raise UsageError("No recipients.", hint="Pass --to.")

        header_stub = "\n".join(
            f"# {k}: {v}"
            for k, v in (
                ("To", ", ".join(recipients)),
                ("Cc", ", ".join(cc_list)),
                ("Subject", subject),
            )
            if v
        )
        text = resolve_body(
            body=body, body_file=body_file, editor_headers=header_stub
        )

        signature = app_ctx.config.send.signature
        if signature and not dry_run:
            text = f"{text.rstrip()}\n\n-- \n{signature}"

        msg = compose.build_message(
            to=recipients,
            cc=cc_list,
            bcc=bcc_list,
            subject=subject,
            body_text=text,
            body_html=(
                html_file.expanduser().read_text(encoding="utf-8")
                if html_file
                else None
            ),
            sender=app_ctx.config.send.default_from,
            attachments=list(attach or []),
        )
        _finish(app_ctx, msg, dry_run=dry_run, thread_id=None, verb="Sent")

    @app.command("reply")
    def reply_cmd(
        ctx: typer.Context,
        ref: str = typer.Argument(..., help="#N reference or id to reply to."),
        body: str = typer.Option(None, "--body", "-b", help="Reply body."),
        body_file: Path = typer.Option(None, "--body-file", "-F"),
        reply_all: bool = typer.Option(
            False, "--all", "-A", help="Reply to everyone, not just the sender."
        ),
        attach: list[Path] = typer.Option(None, "--attach", "-a"),
        no_quote: bool = typer.Option(
            False, "--no-quote", help="Do not quote the message being replied to."
        ),
        dry_run: bool = typer.Option(False, "--dry-run"),
    ) -> None:
        """Reply to a message, keeping it in the same conversation."""
        from ..idref import resolve_one

        app_ctx: AppContext = ctx.obj
        target = resolve_one(ref, app_ctx.cache)

        # Reply to the newest message in the conversation, which is what
        # "reply to this thread" means to a human.
        parent = threads_api.latest_message(app_ctx.client, target)
        if parent is None:
            parent = messages_api.get_message(
                app_ctx.client, target, cache=app_ctx.cache
            )

        to_preview, cc_preview = compose.reply_recipients(
            parent, reply_all=reply_all, self_address=app_ctx.account
        )
        header_stub = "\n".join(
            f"# {k}: {v}"
            for k, v in (
                ("To", ", ".join(to_preview)),
                ("Cc", ", ".join(cc_preview)),
                ("Subject", compose.reply_subject(parent.subject)),
            )
            if v
        )
        text = resolve_body(body=body, body_file=body_file, editor_headers=header_stub)

        msg = compose.build_reply(
            parent,
            body=text,
            sender=app_ctx.account,
            reply_all=reply_all,
            attachments=list(attach or []),
            quote=not no_quote,
        )
        _finish(
            app_ctx,
            msg,
            dry_run=dry_run,
            thread_id=parent.thread_id,
            verb="Replied",
        )

    @app.command("forward")
    def forward_cmd(
        ctx: typer.Context,
        ref: str = typer.Argument(..., help="#N reference or id to forward."),
        to: list[str] = typer.Option(..., "--to", "-t", help="Recipient."),
        body: str = typer.Option(
            None, "--body", "-b", help="Note to add above the forwarded text."
        ),
        body_file: Path = typer.Option(None, "--body-file", "-F"),
        cc: list[str] = typer.Option(None, "--cc"),
        attach: list[Path] = typer.Option(None, "--attach", "-a"),
        dry_run: bool = typer.Option(False, "--dry-run"),
    ) -> None:
        """Forward a message.

        The original's attachments are not carried over — download them with
        `gmail attachments download` and re-attach with -a if you need them.
        """
        from ..idref import resolve_one

        app_ctx: AppContext = ctx.obj
        target = resolve_one(ref, app_ctx.cache)
        try:
            parent = messages_api.get_message(
                app_ctx.client, target, cache=app_ctx.cache
            )
        except Exception:
            latest = threads_api.latest_message(app_ctx.client, target)
            if latest is None:
                raise
            parent = latest

        recipients = compose.validate_addresses(
            compose.split_addresses(to), field="--to"
        )
        cc_list = compose.validate_addresses(compose.split_addresses(cc), field="--cc")

        # A forward is meaningful with no added note, so the editor is opt-in
        # here rather than automatic.
        text = ""
        if body is not None or body_file is not None or not sys.stdin.isatty():
            text = resolve_body(
                body=body, body_file=body_file, allow_editor=False
            )

        msg = compose.build_forward(
            parent,
            to=recipients,
            cc=cc_list,
            body=text,
            sender=app_ctx.account,
            attachments=list(attach or []),
        )
        _finish(app_ctx, msg, dry_run=dry_run, thread_id=None, verb="Forwarded")

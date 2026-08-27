"""``gmail ui`` — the interactive mailbox.

The UI is an alternative front end, never a replacement: every command
documented in the README keeps working exactly as before, and the UI reaches
Gmail through the same ``api/`` layer with the same ``gmail.modify`` scope.
The two also share the ``#N`` numbering — the UI records what it lists, so you
can quit and immediately run ``gmail archive '#2'`` on what was on screen.
"""

from __future__ import annotations

import sys

import typer

from ..context import AppContext
from ..errors import UsageError


def register(app: typer.Typer) -> None:
    @app.command("ui")
    def ui_cmd(
        ctx: typer.Context,
        limit: int = typer.Option(
            50, "--limit", "-n", help="How many conversations to fetch per mailbox."
        ),
        mailbox: str = typer.Option(
            None, "--mailbox", "-l", help="Open on this label instead of the inbox."
        ),
        query: str = typer.Option(
            None, "--search", help="Open showing the results of a search."
        ),
        as_messages: bool = typer.Option(
            False, "--messages", "-m", help="Start in message view, not conversations."
        ),
        mouse: bool = typer.Option(
            True,
            "--mouse/--no-mouse",
            help="Mouse reporting. Off restores your terminal's text selection.",
        ),
        images: bool = typer.Option(
            True,
            "--images/--no-images",
            help="Draw image attachments inline where the terminal supports it.",
        ),
    ) -> None:
        """Browse and manage mail in a full-screen terminal interface.

        Keys are shown along the bottom; press ? for the full list. Everything
        the UI can do has a command equivalent, and vice versa — use whichever
        suits the moment.

        The mouse works: click a row to select it, double-click to open, right-
        click to mark, scroll with the wheel, and click the key hints along the
        bottom as buttons. Press M to turn that off if you would rather your
        terminal kept its own text selection.

        On a terminal that draws images — Ghostty, Kitty, WezTerm, iTerm2 — `i`
        shows an image attachment inline.
        """
        app_ctx: AppContext = ctx.obj

        if app_ctx.json_mode:
            raise UsageError(
                "`gmail ui` is interactive and has no JSON output.",
                hint="Use `gmail ls --json` or `gmail search --json` for scripting.",
            )
        if not sys.stdout.isatty() or not sys.stdin.isatty():
            raise UsageError(
                "`gmail ui` needs a terminal.",
                hint="Run it directly rather than through a pipe, or use "
                "`gmail ls` / `gmail search` for non-interactive output.",
            )

        from ..ui.app import MailApp

        ui = MailApp(app_ctx, limit=limit, mouse=mouse, images=images)
        if mailbox:
            # Resolve the name the same way `gmail ls --label` does, so an
            # unknown label fails here with the familiar "did you mean" hint.
            label_id = app_ctx.labels.resolve(mailbox)
            ui.load_mailboxes()
            match = next(
                (
                    i
                    for i, box in enumerate(ui.state.mailboxes)
                    if box.label_ids == (label_id,)
                ),
                None,
            )
            if match is None:
                from ..ui.state import Mailbox

                ui.state.mailboxes.append(
                    Mailbox(mailbox, label_ids=(label_id,), counter=label_id)
                )
                match = len(ui.state.mailboxes) - 1
            ui.state.mailbox_index = match
        if query:
            ui.state.query = app_ctx.config.aliases.get(query, query)
        ui.state.as_messages = as_messages
        ui.run()

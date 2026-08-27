"""Entry point: global options, command registration, and error handling."""

from __future__ import annotations

import functools
import os
import sys
from typing import Any, Callable

import typer

from . import __version__
from .commands import attachments as attachments_cmd
from .commands import auth as auth_cmd
from .commands import cache_cmd
from .commands import draft as draft_cmd
from .commands import labels as labels_cmd
from .commands import listing, modify, read, send
from .commands import ui_cmd
from .config import Config
from .context import AppContext
from .errors import GmcliError

app = typer.Typer(
    name="gmail",
    help=(
        "Manage Gmail from the terminal.\n\n"
        "Run [bold]gmail ui[/bold] for the full-screen interactive mailbox, or "
        "use the commands below — both do the same things, and both share the "
        "same #N numbering.\n\n"
        "Listings show a #N column; later commands accept those references "
        "(#3, #1-5, #1,3,7) as well as full ids. Add --json to any command "
        "for machine-readable output.\n\n"
        "gmcli requests only the gmail.modify scope, so it can archive and "
        "trash mail but can never delete it permanently."
    ),
    no_args_is_help=True,
    add_completion=True,
    rich_markup_mode="rich",
)

app.add_typer(auth_cmd.app, name="auth")
app.add_typer(labels_cmd.app, name="labels")
app.add_typer(draft_cmd.app, name="draft")
app.add_typer(attachments_cmd.app, name="attachments")
app.add_typer(cache_cmd.app, name="cache")

listing.register(app)
read.register(app)
modify.register(app)
send.register(app)
ui_cmd.register(app)


def _report(exc: GmcliError) -> None:
    from rich.console import Console

    err = Console(stderr=True, highlight=False)
    err.print(f"[red]error:[/red] {exc.message}")
    if exc.hint:
        err.print(f"  [dim]{exc.hint}[/dim]")


def _guard(callback: Callable[..., Any]) -> Callable[..., Any]:
    """Turn a GmcliError into its documented exit code.

    ``functools.wraps`` sets ``__wrapped__``, which ``inspect.signature``
    follows, so Typer still sees the original parameters and builds the same
    options and arguments.
    """

    @functools.wraps(callback)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return callback(*args, **kwargs)
        except GmcliError as exc:
            _report(exc)
            raise typer.Exit(code=exc.exit_code) from exc

    return wrapper


def _install_error_handling(target: typer.Typer) -> None:
    """Wrap every command so exit codes hold for any entry point.

    Doing this in ``main()`` alone would leave ``python -m gmcli``, embedders,
    and the test runner disagreeing with the console script about exit codes —
    and those codes are a documented contract.
    """
    if target.registered_callback and target.registered_callback.callback:
        target.registered_callback.callback = _guard(
            target.registered_callback.callback
        )
    for command in target.registered_commands:
        if command.callback:
            command.callback = _guard(command.callback)
    for group in target.registered_groups:
        if group.typer_instance is not None:
            _install_error_handling(group.typer_instance)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"gmcli {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    account: str = typer.Option(
        None,
        "--account",
        "-A",
        help="Act on this account instead of the default.",
        metavar="EMAIL",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit JSON on stdout. All human messaging moves to stderr.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Suppress non-essential output."
    ),
    no_color: bool = typer.Option(
        False, "--no-color", help="Disable colored output."
    ),
    version: bool = typer.Option(
        None,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Manage Gmail from the terminal."""
    config = Config.load()
    ctx.obj = AppContext(
        account_override=account,
        json_mode=json_output,
        quiet=quiet,
        # NO_COLOR is a de facto standard; honor it alongside the flag.
        color=not no_color and "NO_COLOR" not in os.environ,
        config=config,
    )


@app.command("completion")
def completion_cmd() -> None:
    """Explain how to install shell completion."""
    typer.echo(
        "Install shell completion with:\n"
        "  gmail --install-completion\n\n"
        "Then restart your shell. To see the script without installing it:\n"
        "  gmail --show-completion"
    )


# Registration is complete once the module body has run, so the guards go on
# here rather than beside the individual registrations.
_install_error_handling(app)


def main() -> None:
    """Console-script entry point.

    Command errors are already mapped to exit codes by ``_install_error_handling``;
    what remains here is the safety net for anything raised outside a command
    callback, plus the signal-ish cases a CLI should not report as crashes.
    """
    try:
        app()
    except GmcliError as exc:  # pragma: no cover - defensive
        _report(exc)
        raise SystemExit(exc.exit_code)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except BrokenPipeError:
        # `gmail ls | head` closes the pipe early; that is not a failure.
        try:
            sys.stdout.close()
        finally:
            raise SystemExit(0)


if __name__ == "__main__":  # pragma: no cover
    main()

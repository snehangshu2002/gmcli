"""``gmail cache …`` — inspect and clear the disposable cache."""

from __future__ import annotations

import typer

from ..cache import Cache, cache_root
from ..context import AppContext

app = typer.Typer(help="Manage the local cache.", no_args_is_help=True)


@app.command("clear")
def clear_cmd(
    ctx: typer.Context,
    all_accounts: bool = typer.Option(
        False, "--all", help="Clear every account's cache, not just the active one."
    ),
) -> None:
    """Delete cached labels, bodies, and listing references."""
    app_ctx: AppContext = ctx.obj
    if all_accounts:
        removed = Cache.clear_all()
        scope = "all accounts"
    else:
        removed = app_ctx.cache.clear()
        scope = app_ctx.account
    app_ctx.renderer.result(
        {"removed_files": removed, "scope": scope},
        f"Cleared {removed} cached file(s) for {scope}",
    )


@app.command("path")
def path_cmd(ctx: typer.Context) -> None:
    """Print the cache directory."""
    app_ctx: AppContext = ctx.obj
    if app_ctx.json_mode:
        app_ctx.renderer.json({"path": str(cache_root())})
    else:
        # Bare path on stdout so `cd "$(gmail cache path)"` works.
        typer.echo(str(cache_root()))

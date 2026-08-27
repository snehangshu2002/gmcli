"""``gmail labels …`` — manage the label list itself."""

from __future__ import annotations

import typer

from ..api.labels import (
    create_label,
    delete_label,
    fetch_label_details,
    fetch_labels,
    rename_label,
)
from ..context import AppContext
from ..errors import UsageError

app = typer.Typer(help="List and manage labels.", no_args_is_help=True)


@app.command("list")
def list_cmd(
    ctx: typer.Context,
    user_only: bool = typer.Option(
        False, "--user", "-u", help="Only labels you created."
    ),
    counts: bool = typer.Option(
        False, "--counts", "-c", help="Include message and unread counts."
    ),
) -> None:
    """List labels."""
    app_ctx: AppContext = ctx.obj
    labels = fetch_labels(app_ctx.client, app_ctx.cache)
    if user_only:
        labels = [lb for lb in labels if not lb.is_system]
    if counts:
        # labels.list omits counts; only labels.get carries them, so this
        # costs one batched round trip and is opt-in for that reason.
        labels = fetch_label_details(app_ctx.client, [lb.id for lb in labels])
    app_ctx.renderer.labels(labels)


@app.command("create")
def create_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Label name. Use 'parent/child' to nest."),
) -> None:
    """Create a label."""
    app_ctx: AppContext = ctx.obj
    if app_ctx.labels.get(name):
        raise UsageError(f"A label named {name!r} already exists.")
    label = create_label(app_ctx.client, name, app_ctx.cache)
    app_ctx.renderer.result(label.to_dict(), f"Created label {label.name}")


@app.command("rename")
def rename_cmd(
    ctx: typer.Context,
    old: str = typer.Argument(..., help="Current name."),
    new: str = typer.Argument(..., help="New name."),
) -> None:
    """Rename a label."""
    app_ctx: AppContext = ctx.obj
    existing = app_ctx.labels.get(old)
    if existing is None:
        raise UsageError(f"No label named {old!r}.")
    if existing.is_system:
        raise UsageError(f"{existing.name} is a system label and cannot be renamed.")
    label = rename_label(app_ctx.client, existing.id, new, app_ctx.cache)
    app_ctx.renderer.result(label.to_dict(), f"Renamed {old} → {label.name}")


@app.command("delete")
def delete_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Label to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Delete a label. Messages that carried it are not affected."""
    app_ctx: AppContext = ctx.obj
    existing = app_ctx.labels.get(name)
    if existing is None:
        raise UsageError(f"No label named {name!r}.")
    if existing.is_system:
        raise UsageError(f"{existing.name} is a system label and cannot be deleted.")

    if not yes and not app_ctx.json_mode:
        # Deleting a label is not recoverable, though the mail itself is safe.
        confirm = typer.confirm(
            f"Delete label {existing.name!r}? Messages keep their other labels."
        )
        if not confirm:
            app_ctx.renderer.info("Cancelled.")
            raise typer.Exit(code=0)

    delete_label(app_ctx.client, existing.id, app_ctx.cache)
    app_ctx.renderer.result(
        {"deleted": existing.name}, f"Deleted label {existing.name}"
    )

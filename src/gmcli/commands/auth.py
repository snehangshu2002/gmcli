"""``gmail auth …`` — login, logout, account switching, and diagnostics."""

from __future__ import annotations

import time
from pathlib import Path

import typer

from ..auth.flow import (
    PUBLISHING_STATUS_HINT,
    SCOPES,
    TESTING_TOKEN_LIFETIME_DAYS,
    load_credentials,
    login,
    logout,
    token_age_days,
)
from ..auth.client_config import (
    describe_client_source,
    has_client,
    resolve_client,
)
from ..auth.store import (
    account_backend,
    get_store,
    list_accounts,
    token_file_mode,
)
from ..config import config_path
from ..context import AppContext
from ..errors import AuthError, UsageError
from ..output import Renderer

app = typer.Typer(help="Authorize gmcli and manage accounts.", no_args_is_help=True)

REVOKE_URL = "https://myaccount.google.com/permissions"


@app.command("login")
def login_cmd(
    ctx: typer.Context,
    credentials: Path = typer.Option(
        None,
        "--credentials",
        "-c",
        help="Path to your OAuth client JSON. Needed once; copied into "
        "gmcli's data directory afterwards.",
    ),
    port: int = typer.Option(
        0,
        "--port",
        "-p",
        help="Fix the loopback redirect port (useful over an SSH tunnel). "
        "0 picks a free one.",
    ),
) -> None:
    """Authorize an account through your browser."""
    app_ctx: AppContext = ctx.obj
    out = app_ctx.renderer

    # Resolve first: failing after announcing a browser we never open reads
    # like a crash rather than a missing prerequisite.
    resolve_client(credentials, config=app_ctx.config)

    out.info("Opening your browser to authorize gmcli…")
    out.info(f"[dim]Requesting scope: {SCOPES[0]}[/dim]")
    email, store, client = login(credentials=credentials, port=port)

    if app_ctx.json_mode:
        out.json(
            {
                "account": email,
                "backend": store.name,
                "scopes": SCOPES,
                "client_source": client.source,
            }
        )
        return

    out.success(f"Authorized as [bold]{email}[/bold]")
    out.info(f"[dim]Credentials stored in {store.description}[/dim]")

    if not app_ctx.config.default_account:
        app_ctx.config.default_account = email
        app_ctx.config.save()
        out.info("[dim]Set as the default account.[/dim]")

    out.info("")
    out.info("[yellow]One thing to check:[/yellow] " + PUBLISHING_STATUS_HINT)


@app.command("logout")
def logout_cmd(
    ctx: typer.Context,
    account: str = typer.Option(None, "--account", "-a", help="Account to forget."),
    all_accounts: bool = typer.Option(
        False, "--all", help="Forget every account."
    ),
) -> None:
    """Remove stored credentials from this machine."""
    app_ctx: AppContext = ctx.obj
    out = app_ctx.renderer

    if all_accounts:
        targets = list_accounts()
    elif account:
        targets = [account]
    else:
        targets = [app_ctx.account]

    if not targets:
        out.info("No accounts are logged in.")
        return

    removed = [t for t in targets if logout(t)]

    config = app_ctx.config
    if config.default_account in removed:
        config.default_account = None
        remaining = list_accounts()
        if len(remaining) == 1:
            config.default_account = remaining[0]
        config.save()

    if app_ctx.json_mode:
        out.json({"removed": removed})
        return

    for name in removed:
        out.success(f"Forgot credentials for {name}")
    out.info(
        f"[dim]This only clears local credentials. To revoke gmcli's access "
        f"at Google, visit {REVOKE_URL}[/dim]"
    )


@app.command("list")
def list_cmd(ctx: typer.Context) -> None:
    """List authorized accounts."""
    app_ctx: AppContext = ctx.obj
    out = app_ctx.renderer
    accounts = list_accounts()
    default = app_ctx.config.default_account

    if app_ctx.json_mode:
        out.json(
            [
                {
                    "account": a,
                    "default": a == default,
                    "backend": account_backend(a),
                }
                for a in accounts
            ]
        )
        return

    if not accounts:
        out.info("No accounts. Run `gmail auth login` to add one.")
        return
    for name in accounts:
        marker = "[green]*[/green]" if name == default else " "
        out.info(f"{marker} {name}")


@app.command("switch")
def switch_cmd(ctx: typer.Context, account: str) -> None:
    """Set the default account used when --account is not given."""
    app_ctx: AppContext = ctx.obj
    out = app_ctx.renderer

    accounts = list_accounts()
    if account not in accounts:
        raise UsageError(
            f"{account} is not logged in.",
            hint=f"Available: {', '.join(accounts) or 'none'}",
        )
    app_ctx.config.default_account = account
    app_ctx.config.save()
    out.result({"default_account": account}, f"Default account is now {account}")


@app.command("status")
def status_cmd(ctx: typer.Context) -> None:
    """Show the active account, storage backend, and token health."""
    app_ctx: AppContext = ctx.obj
    out = app_ctx.renderer

    store = get_store()
    accounts = list_accounts()
    default = app_ctx.config.default_account
    active = app_ctx.account_override or default or (accounts[0] if len(accounts) == 1 else None)

    payload = store.load(active) if active else None
    age = token_age_days(payload) if payload else None
    mode = token_file_mode(active) if active else None

    if app_ctx.json_mode:
        out.json(
            {
                "active_account": active,
                "accounts": accounts,
                "default_account": default,
                "backend": store.name,
                "backend_description": store.description,
                "token_age_days": round(age, 2) if age is not None else None,
                "token_file_mode": oct(mode) if mode is not None else None,
                "scopes": SCOPES,
                "config_path": str(config_path()),
                "client_secret_configured": has_client(app_ctx.config),
                "client_source": describe_client_source(app_ctx.config) or None,
            }
        )
        return

    rows = [
        ("Account", active or "[red]none[/red]"),
        ("Accounts", ", ".join(accounts) or "none"),
        ("Token store", store.description),
        ("Scope", SCOPES[0]),
        ("Config", str(config_path())),
        (
            "OAuth client",
            describe_client_source(app_ctx.config) or "[red]missing[/red]",
        ),
    ]
    if age is not None:
        warn = age >= TESTING_TOKEN_LIFETIME_DAYS - 1
        rows.append(
            (
                "Token age",
                f"[yellow]{age:.1f} days[/yellow]" if warn else f"{age:.1f} days",
            )
        )
    if mode is not None:
        rows.append(("Token file mode", oct(mode)))
    out.table(rows, title="gmcli")

    if not app_ctx.json_mode:
        out.info(
            "\n[dim]Permanent deletion is not available: gmcli requests only "
            "gmail.modify, which cannot delete mail irreversibly.[/dim]"
        )


@app.command("doctor")
def doctor_cmd(ctx: typer.Context) -> None:
    """Diagnose authentication problems and explain how to fix them."""
    app_ctx: AppContext = ctx.obj
    out = app_ctx.renderer
    checks: list[dict[str, str]] = []

    def record(name: str, ok: bool | None, detail: str) -> None:
        checks.append(
            {
                "check": name,
                "status": "ok" if ok else ("warn" if ok is None else "fail"),
                "detail": detail,
            }
        )

    # 1. OAuth client present, and from where?
    source = describe_client_source(app_ctx.config)
    if source:
        record("oauth_client", True, f"Using the {source}.")
    else:
        record(
            "oauth_client",
            False,
            "No OAuth client configured. Run `gmail auth setup` to create one "
            "(about two minutes, guided).",
        )

    # 2. Any accounts?
    accounts = list_accounts()
    record(
        "accounts",
        bool(accounts),
        ", ".join(accounts) if accounts else "No accounts logged in.",
    )

    # 3. Token store
    store = get_store()
    record(
        "token_store",
        store.name == "keyring" or None,
        store.description
        + (
            ""
            if store.name == "keyring"
            else " — no usable keyring found; this is expected on headless hosts."
        ),
    )

    # 4. Token permissions, when a file is in play
    account = None
    try:
        account = app_ctx.account
    except (AuthError, UsageError) as exc:
        record("active_account", False, str(exc))

    if account:
        mode = token_file_mode(account)
        if mode is None:
            record("token_permissions", True, "No token file (stored in keyring).")
        elif mode & 0o077:
            record(
                "token_permissions",
                False,
                f"Token file is {oct(mode)} — readable by others. "
                f"Run: chmod 600 on it, or log in again.",
            )
        else:
            record("token_permissions", True, f"Token file is {oct(mode)}.")

        # 5. Token age against the Testing-status window
        payload = store.load(account)
        age = token_age_days(payload) if payload else None
        if age is None:
            record("token_age", None, "Unknown (token predates age tracking).")
        elif age >= TESTING_TOKEN_LIFETIME_DAYS:
            record(
                "token_age",
                None,
                f"{age:.1f} days old. If refresh fails, this is almost certainly "
                f"the 'Testing' publishing-status expiry. {PUBLISHING_STATUS_HINT}",
            )
        else:
            record("token_age", True, f"{age:.1f} days old.")

        # 6. Does a refresh actually work right now?
        try:
            creds = load_credentials(account)
            record("refresh", True, "Credentials are valid.")
            granted = set(getattr(creds, "scopes", None) or [])
            if granted and not set(SCOPES).issubset(granted):
                record(
                    "scopes",
                    False,
                    f"Granted {sorted(granted)}, need {SCOPES}. "
                    "Run `gmail auth login` to re-consent.",
                )
            else:
                record("scopes", True, SCOPES[0])
        except AuthError as exc:
            record("refresh", False, f"{exc.message} {exc.hint or ''}".strip())

    # 7. Clock skew breaks JWT validation in confusing ways.
    skew = _clock_skew_seconds()
    if skew is None:
        record("clock", None, "Could not check system clock against Google.")
    elif abs(skew) > 60:
        record(
            "clock",
            False,
            f"System clock is off by {skew:.0f}s. OAuth tokens will be rejected. "
            "Enable NTP time sync.",
        )
    else:
        record("clock", True, f"Within {abs(skew):.0f}s of Google's clock.")

    if app_ctx.json_mode:
        out.json(checks)
        return

    _print_checks(out, checks)
    if any(c["status"] == "fail" for c in checks):
        raise typer.Exit(code=3)


def _print_checks(out: Renderer, checks: list[dict[str, str]]) -> None:
    icons = {"ok": "[green]✓[/green]", "warn": "[yellow]![/yellow]", "fail": "[red]✗[/red]"}
    for check in checks:
        out.info(f"{icons[check['status']]} [bold]{check['check']}[/bold]")
        out.info(f"   [dim]{check['detail']}[/dim]")


def _clock_skew_seconds() -> float | None:
    """Compare the local clock to Google's Date header.

    A skewed clock produces OAuth failures whose error text says nothing about
    time, so it is worth checking explicitly.
    """
    try:
        import email.utils
        import urllib.request

        before = time.time()
        with urllib.request.urlopen(
            "https://oauth2.googleapis.com/", timeout=5
        ) as resp:
            date_header = resp.headers.get("Date")
        after = time.time()
        if not date_header:
            return None
        server = email.utils.parsedate_to_datetime(date_header).timestamp()
        return ((before + after) / 2) - server
    except Exception:  # noqa: BLE001 - diagnostics must never crash
        return None


# Registered here rather than in cli.py so the wizard travels with the group it
# belongs to. setup.py imports nothing from this module, so there is no cycle.
from .setup import register as _register_setup  # noqa: E402

_register_setup(app)

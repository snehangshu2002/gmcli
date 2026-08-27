"""``gmail auth setup`` — a guided walk through creating an OAuth client.

Google will not let a distributed CLI request restricted Gmail scopes through
a shared client without verification and a paid annual security assessment, so
every user needs their own client. That is a genuine constraint, not a design
choice — but "read the README and find five console pages yourself" is not the
only way to satisfy it.

This wizard opens each console page at the right moment, automates what
``gcloud`` can automate when it is installed, finds the downloaded client JSON
on its own, validates it, and finishes by logging in — turning a documentation
safari into roughly two minutes of pressing Enter.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import typer

from ..auth.client_config import load_client_file
from ..auth.flow import install_client_secret, login
from ..config import client_secret_path
from ..context import AppContext
from ..errors import AuthError, UsageError
from ..output import Renderer

CONSOLE = "https://console.cloud.google.com"

# Where browsers land a downloaded client, newest first wins.
DOWNLOAD_DIRS = ("~/Downloads", "~/Desktop", ".")
DOWNLOAD_GLOBS = ("client_secret*.json", "client_secret*.JSON")
# A file touched within this window is almost certainly the one just downloaded.
FRESH_SECONDS = 1800


def _url(path: str, project: str | None) -> str:
    return f"{CONSOLE}{path}" + (f"?project={project}" if project else "")


def _open(url: str, out: Renderer, *, no_browser: bool) -> None:
    """Show a URL, and open it unless the user asked us not to.

    The URL is always printed: ``webbrowser`` silently no-ops in plenty of
    environments (SSH, WSL without a handler, bare TTY), and a wizard that
    claims to have opened a page that never appeared is worse than one that
    just prints the link.
    """
    out.info(f"  [cyan]{url}[/cyan]")
    if no_browser:
        return
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - opening a browser is best-effort
        pass


def _step(out: Renderer, number: int, total: int, title: str) -> None:
    out.info(f"\n[bold]Step {number}/{total} — {title}[/bold]")


def _wait(message: str = "Press Enter when done") -> None:
    typer.prompt(f"  {message}", default="", show_default=False, prompt_suffix=" … ")


def _gcloud() -> str | None:
    return shutil.which("gcloud")


def _run_gcloud(args: list[str], out: Renderer) -> bool:
    """Run a gcloud command, showing it first. Returns True on success."""
    printable = " ".join(["gcloud", *args])
    out.info(f"  [dim]$ {printable}[/dim]")
    try:
        result = subprocess.run(
            ["gcloud", *args], capture_output=True, text=True, timeout=180
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        out.warn(f"gcloud failed to run: {exc}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        out.warn(f"gcloud failed: {detail[-1] if detail else 'unknown error'}")
        return False
    return True


def find_downloaded_client(*, fresh_only: bool = True) -> list[Path]:
    """Locate freshly downloaded ``client_secret*.json`` files, newest first.

    Typing an absolute path to a file with a 60-character generated name is the
    most annoying part of the manual flow, so it is worth removing.
    """
    now = time.time()
    found: dict[Path, float] = {}
    for directory in DOWNLOAD_DIRS:
        base = Path(directory).expanduser()
        if not base.is_dir():
            continue
        for pattern in DOWNLOAD_GLOBS:
            for path in base.glob(pattern):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if fresh_only and now - mtime > FRESH_SECONDS:
                    continue
                found[path.resolve()] = mtime
    return [p for p, _ in sorted(found.items(), key=lambda kv: kv[1], reverse=True)]


def _looks_like_desktop_client(path: Path) -> bool:
    try:
        return "installed" in json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False


def _collect_client_file(out: Renderer) -> Path:
    """Find the downloaded JSON, or ask for its path as a fallback."""
    for attempt in range(3):
        candidates = [p for p in find_downloaded_client() if _looks_like_desktop_client(p)]
        if candidates:
            best = candidates[0]
            out.info(f"\n  Found [green]{best}[/green]")
            if typer.confirm("  Use this file?", default=True):
                return best

        if attempt == 0:
            out.info(
                "\n  [dim]No freshly downloaded client file found in "
                "~/Downloads, ~/Desktop, or here.[/dim]"
            )
        typed = typer.prompt(
            "  Path to the downloaded JSON (or press Enter to look again)",
            default="",
            show_default=False,
        ).strip()
        if typed:
            return Path(typed).expanduser()

    raise UsageError(
        "Could not find the downloaded OAuth client file.",
        hint="Re-run with the path: `gmail auth setup --credentials <path>`, "
        "or `gmail auth login --credentials <path>`.",
    )


def register(auth_app: typer.Typer) -> None:
    @auth_app.command("setup")
    def setup_cmd(
        ctx: typer.Context,
        project: str = typer.Option(
            None,
            "--project",
            "-p",
            help="Google Cloud project id to use, if you already have one.",
        ),
        credentials: Path = typer.Option(
            None,
            "--credentials",
            "-c",
            help="Skip the walkthrough and install this client JSON directly.",
        ),
        no_browser: bool = typer.Option(
            False, "--no-browser", help="Print console URLs instead of opening them."
        ),
        use_gcloud: bool = typer.Option(
            True,
            "--gcloud/--no-gcloud",
            help="Use the gcloud CLI for the steps it can automate, if installed.",
        ),
        force: bool = typer.Option(
            False, "--force", "-f", help="Replace an already-installed client."
        ),
    ) -> None:
        """Set up Google sign-in, step by step.

        Creates the one OAuth client gmcli needs and logs you in. Everything
        stays in your own Google Cloud project, so no third party ever sits
        between you and your mail.
        """
        app_ctx: AppContext = ctx.obj
        out = app_ctx.renderer

        # The fast path: a file was handed to us, so no walkthrough is needed.
        if credentials is not None:
            install_client_secret(credentials)
            out.success(f"Installed OAuth client from {credentials}")
            _finish_with_login(app_ctx, no_browser=no_browser)
            return

        if client_secret_path().exists() and not force:
            out.info(
                f"An OAuth client is already installed at "
                f"[dim]{client_secret_path()}[/dim]"
            )
            if not typer.confirm("Replace it and start over?", default=False):
                out.info("Keeping the existing client. Running login instead.")
                _finish_with_login(app_ctx, no_browser=no_browser)
                return

        if not sys.stdin.isatty():
            raise UsageError(
                "`gmail auth setup` is interactive and needs a terminal.",
                hint="For unattended setup, pass an existing client with "
                "`gmail auth login --credentials <path>`, or set "
                "GMCLI_CLIENT_ID and GMCLI_CLIENT_SECRET.",
            )

        _intro(out)

        gcloud_path = _gcloud() if use_gcloud else None
        total = 5

        project = _step_project(out, project, gcloud_path, total, no_browser)
        _step_enable_api(out, project, gcloud_path, total, no_browser)
        _step_consent_screen(out, project, total, no_browser)
        _step_publish(out, project, total, no_browser)
        _step_create_client(out, project, total, no_browser)

        path = _collect_client_file(out)
        install_client_secret(path)
        client = load_client_file(client_secret_path())
        out.success(f"OAuth client installed ({client.client_id[:24]}…)")

        _finish_with_login(app_ctx, no_browser=no_browser)

    # Registration appends, but `setup` is where a new user starts, so it
    # belongs at the top of `gmail auth --help` rather than the bottom.
    auth_app.registered_commands.insert(0, auth_app.registered_commands.pop())


def _intro(out: Renderer) -> None:
    out.info(
        "\n[bold]Setting up Google sign-in for gmcli[/bold]\n\n"
        "Gmail's API requires each person to use their own OAuth client — "
        "Google does not permit a shared one for mailbox access without a "
        "paid annual security audit. This is a one-time, ~2 minute setup, "
        "and it means your mail is only ever reachable by a client you own.\n"
    )
    if not typer.confirm("Ready to start?", default=True):
        raise typer.Exit(code=0)


def _step_project(
    out: Renderer,
    project: str | None,
    gcloud_path: str | None,
    total: int,
    no_browser: bool,
) -> str:
    _step(out, 1, total, "Google Cloud project")

    if project:
        out.info(f"  Using project [green]{project}[/green]")
        return project

    if gcloud_path and typer.confirm(
        "  gcloud is installed. Create the project automatically?", default=True
    ):
        suggested = f"gmcli-{int(time.time()) % 100000}"
        project = typer.prompt("  Project id", default=suggested).strip()
        if _run_gcloud(
            ["projects", "create", project, "--name=gmcli"], out
        ):
            out.success(f"Created project {project}")
            return project
        out.warn("Falling back to creating it in the browser.")

    out.info("  Create a project (any name — 'gmcli' is fine):")
    _open(f"{CONSOLE}/projectcreate", out, no_browser=no_browser)
    _wait("Press Enter once the project exists")

    project = typer.prompt("  Project id (shown in the console's project picker)").strip()
    if not project:
        raise UsageError("A project id is required to continue.")
    return project


def _step_enable_api(
    out: Renderer,
    project: str,
    gcloud_path: str | None,
    total: int,
    no_browser: bool,
) -> None:
    _step(out, 2, total, "Enable the Gmail API")

    if gcloud_path and typer.confirm("  Enable it with gcloud?", default=True):
        if _run_gcloud(
            ["services", "enable", "gmail.googleapis.com", f"--project={project}"], out
        ):
            out.success("Gmail API enabled")
            return
        out.warn("Falling back to the browser.")

    out.info("  Click [bold]Enable[/bold] on this page:")
    _open(
        _url("/apis/library/gmail.googleapis.com", project), out, no_browser=no_browser
    )
    _wait("Press Enter once the API is enabled")


def _step_consent_screen(
    out: Renderer, project: str, total: int, no_browser: bool
) -> None:
    _step(out, 3, total, "Consent screen (Branding)")
    out.info(
        "  Three fields are required — everything else on the page is\n"
        "  optional and can be left blank:\n\n"
        "    • [bold]App name[/bold] — anything; 'gmcli' is fine\n"
        "    • [bold]User support email[/bold] — pick your address\n"
        "    • [bold]Developer contact information[/bold] — your address again\n\n"
        "  Skip the logo, app domain, privacy policy, and authorized domains.\n"
        "  Choose [bold]External[/bold] as the user type (or Internal, if this\n"
        "  is a Workspace account — that skips the unverified-app warning).\n"
        "  Then click [bold]Save[/bold].\n\n"
        "  [yellow]Do not skip this:[/yellow] the next step's 'Publish app'\n"
        "  button stays greyed out until Branding is complete."
    )
    _open(_url("/auth/branding", project), out, no_browser=no_browser)
    _wait("Press Enter once Branding is saved")


def _step_publish(out: Renderer, project: str, total: int, no_browser: bool) -> None:
    _step(out, 4, total, "Publish the app  [yellow](the step everyone misses)[/yellow]")
    out.info(
        "  Set the publishing status to [bold]In production[/bold].\n\n"
        "  [yellow]Why this matters:[/yellow] while the app sits in 'Testing',\n"
        "  Google expires refresh tokens after [bold]7 days[/bold]. Everything\n"
        "  works perfectly for a week and then fails with an opaque\n"
        "  'invalid_grant' error. Publishing avoids that entirely.\n\n"
        "  Your app stays unverified, which is fine: you will see a\n"
        "  'Google hasn't verified this app' screen at login — click\n"
        "  [bold]Advanced → Go to (your app)[/bold]. That warning is correct;\n"
        "  the unverified app is yours.\n\n"
        "  [dim]If 'Publish app' is greyed out, the page will say the OAuth\n"
        "  configuration is incomplete — go back to Branding (step 3), fill in\n"
        "  the app name and both email fields, Save, then return here.[/dim]"
    )
    _open(_url("/auth/audience", project), out, no_browser=no_browser)
    _wait("Press Enter once the app is published")


def _step_create_client(
    out: Renderer, project: str, total: int, no_browser: bool
) -> None:
    _step(out, 5, total, "Create the OAuth client")
    out.info(
        "  Application type: [bold]Desktop app[/bold] — not 'Web application'.\n"
        "  Only a desktop client can use the loopback redirect this CLI needs.\n"
        "  Then [bold]download the JSON[/bold]; I will find it automatically."
    )
    _open(_url("/auth/clients/create", project), out, no_browser=no_browser)
    _wait("Press Enter once you have downloaded the JSON")


def _finish_with_login(app_ctx: AppContext, *, no_browser: bool) -> None:
    """Log in immediately, so setup ends in a working state rather than a to-do."""
    out = app_ctx.renderer
    out.info("\n[bold]Signing in…[/bold]")
    out.info("  [dim]Your browser will open to approve access.[/dim]")

    try:
        email, store, client = login(open_browser=not no_browser)
    except AuthError as exc:
        out.error(exc.message, exc.hint)
        out.info(
            "\n[dim]The client is installed, so you can retry the sign-in alone "
            "with `gmail auth login`.[/dim]"
        )
        raise typer.Exit(code=3) from exc

    if not app_ctx.config.default_account:
        app_ctx.config.default_account = email
        app_ctx.config.save()

    if app_ctx.json_mode:
        out.json(
            {"account": email, "backend": store.name, "client_source": client.source}
        )
        return

    out.info("")
    out.success(f"Signed in as [bold]{email}[/bold]")
    out.info(f"  [dim]Credentials stored in {store.description}[/dim]")
    # Padded to a common column so the descriptions line up.
    examples = (
        ("gmail ls", "list your inbox"),
        ('gmail search "is:unread"', "find unread mail"),
        ("gmail --help", "everything else"),
    )
    width = max(len(cmd) for cmd, _ in examples)
    out.info("\n[bold]You're set.[/bold] Try:")
    for cmd, description in examples:
        out.info(f"  [cyan]{cmd}[/cyan]{' ' * (width - len(cmd))}   {description}")

"""Noticing that a newer gmcli exists — and installing it.

Three rules shape this, in order:

1. **It never makes anyone wait.** The version on PyPI is fetched on a daemon
   thread and written to the cache; the *next* run is what mentions it. A
   command's latency is Gmail's business, and a release check has no claim on
   it. `atexit` gives the thread a fraction of a second to land so that short,
   offline commands still eventually see a release — beyond that budget the
   process exits and the check is simply retried tomorrow.
2. **It never breaks a pipe.** The notice goes to stderr, only when stderr is a
   terminal, never under `--json` or `--quiet`. `gmail ls --json | jq` must be
   unaffected by anything here.
3. **It never nags a developer.** Running out of a source checkout — the `-e`
   install this repo uses — skips the check entirely, because `pip install -U`
   is not how you would update that copy anyway.

`GMCLI_NO_UPDATE_CHECK=1`, or `[update] check = false` in the config file,
turns the whole thing off.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import cache_dir
from .errors import ApiError, UsageError

PACKAGE = "gmcli"
PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
# Once a day is often enough to hear about a release and rare enough that PyPI
# never notices us.
CHECK_INTERVAL = 24 * 60 * 60
FETCH_TIMEOUT = 2.0
# How long the process will wait at exit for a check already in flight.
JOIN_BUDGET = 0.5


# -- version arithmetic -------------------------------------------------------


def release_tuple(version: str) -> tuple[int, ...] | None:
    """The numeric release of a version, or `None` if it is not a plain one.

    Pre-releases return `None` and are never offered: someone who wants an
    `rc` installs it deliberately, and being told about one by a tool they
    installed for their mail is noise.
    """
    core = version.strip().split("+", 1)[0]
    for suffix in (".post", ".dev"):
        core = core.split(suffix, 1)[0]
    parts = core.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = release_tuple(candidate), release_tuple(current)
    if new is None or old is None:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


# -- where the answer is kept -------------------------------------------------


def state_path() -> Path:
    """Not per-account: which gmcli is installed is not a property of a mailbox."""
    return cache_dir() / "update.json"


def _read_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(**fields: object) -> None:
    state = _read_state()
    state.update(fields)
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # an unwritable cache costs a check, nothing more


# -- the check ----------------------------------------------------------------


def running_from_checkout() -> bool:
    """Is this the repository rather than an installed package?"""
    return (Path(__file__).parent.parent.name == "src")


def stderr_is_terminal() -> bool:
    """Its own function so a test can be the one thing that lies about it."""
    return sys.stderr.isatty()


def checks_enabled(config: object | None = None) -> bool:
    if os.environ.get("GMCLI_NO_UPDATE_CHECK"):
        return False
    if running_from_checkout():
        return False
    update = getattr(config, "update", None)
    return bool(getattr(update, "check", True))


def fetch_latest(*, timeout: float = FETCH_TIMEOUT) -> tuple[str | None, str | None]:
    """Ask PyPI for the current release: `(version, problem)`, never both.

    The two failures are worth telling apart. A 404 means this copy did not
    come from PyPI — an unpublished build, or a private fork — and telling
    that user to check their network would send them after the wrong thing.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        PYPI_URL,
        headers={
            "User-Agent": f"gmcli/{__version__}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, f"{PACKAGE} is not published on PyPI"
        return None, f"PyPI answered {exc.code}"
    except Exception:
        return None, "PyPI could not be reached"
    version = (payload.get("info") or {}).get("version")
    if not isinstance(version, str):
        return None, "PyPI returned no version for this package"
    return version, None


def latest_version(*, timeout: float = FETCH_TIMEOUT) -> str | None:
    """The version alone, for the background check, which cannot act on why."""
    return fetch_latest(timeout=timeout)[0]


def _fetch_and_store() -> None:
    version = latest_version()
    if version:
        _write_state(latest=version, checked_at=time.time())


def start_check(config: object | None = None, *, force: bool = False) -> threading.Thread | None:
    """Kick off the daily check in the background. Returns the thread, if any."""
    if not force and not checks_enabled(config):
        return None
    state = _read_state()
    if not force and time.time() - float(state.get("checked_at") or 0) < CHECK_INTERVAL:
        return None
    # Stamp the attempt *before* making it: a machine that is offline for a week
    # should try once a day, not once a command.
    _write_state(checked_at=time.time())
    thread = threading.Thread(target=_fetch_and_store, name="gmcli-update", daemon=True)
    thread.start()
    # A daemon thread dies with the process, and `gmail cache path` is over in
    # milliseconds — without this, an install used only for quick commands
    # would never once complete a check. Half a second, then we give up.
    atexit.register(thread.join, JOIN_BUDGET)
    return thread


def pending_notice(current: str = __version__) -> str | None:
    """What to tell the user, from what the last check found. No network."""
    latest = _read_state().get("latest")
    if isinstance(latest, str) and is_newer(latest, current):
        return (
            f"Update available: gmcli {current} → {latest}. "
            "Run `gmail --upgrade` to install it."
        )
    return None


# -- installing it ------------------------------------------------------------


def upgrade_plan() -> tuple[list[str], str]:
    """The command that upgrades *this* install, and the name of the installer.

    An installed tool is upgraded by whatever put it there — `pip install -U`
    inside a pipx or uv venv is at best ignored and at worst breaks the shim.
    The layout of `sys.prefix` is what says which one that was.
    """
    import shutil

    parts = set(Path(sys.prefix).parts)
    if {"pipx", "venvs"} <= parts and shutil.which("pipx"):
        return ["pipx", "upgrade", PACKAGE], "pipx"
    if {"uv", "tools"} <= parts and shutil.which("uv"):
        return ["uv", "tool", "upgrade", PACKAGE], "uv"
    return [sys.executable, "-m", "pip", "install", "--upgrade", PACKAGE], "pip"


def upgrade(*, echo=print) -> int:
    """Run the upgrade in the foreground, streaming the installer's output."""
    if running_from_checkout():
        raise UsageError(
            "This gmcli runs from a source checkout, not an installed package.",
            hint="Update it with `git pull` in the repository instead.",
        )

    current = __version__
    echo(f"Current version: gmcli {current}")
    latest, problem = fetch_latest(timeout=10.0)
    if latest is None:
        raise ApiError(
            f"Could not check for a newer version: {problem}.",
            hint=f"Install directly with: {' '.join(upgrade_plan()[0])}",
        )
    _write_state(latest=latest, checked_at=time.time())
    if not is_newer(latest, current):
        echo(f"Already on the latest release ({latest}).")
        return 0

    argv, installer = upgrade_plan()
    echo(f"Upgrading to {latest} with {installer}: {' '.join(argv)}")
    try:
        code = subprocess.run(argv).returncode
    except OSError as exc:
        raise ApiError(
            f"Could not run {installer}: {exc}",
            hint=f"Upgrade manually with: {' '.join(argv)}",
        ) from exc
    if code == 0:
        echo(f"gmcli {latest} installed.")
    return code

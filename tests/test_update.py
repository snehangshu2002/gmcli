"""The release check and `gmail --upgrade`.

Nothing here touches the network: `latest_version` is the single seam every
test replaces. What is actually being pinned down is the etiquette — that the
check is throttled, that it never speaks on a pipe, and that the upgrade runs
the installer that put this copy here rather than whichever one is handiest.
"""

from __future__ import annotations

import json
import time

import pytest
from typer.testing import CliRunner

from gmcli import update
from gmcli.cli import app
from gmcli.config import Config
from gmcli.errors import ApiError, UsageError

runner = CliRunner()


@pytest.fixture
def installed(isolated_dirs, monkeypatch):
    """Pretend to be an installed package rather than this checkout."""
    monkeypatch.setattr(update, "running_from_checkout", lambda: False)
    monkeypatch.delenv("GMCLI_NO_UPDATE_CHECK", raising=False)
    return isolated_dirs


@pytest.fixture
def logged_in(installed, monkeypatch):
    """An account whose client is the fake service, so a command can run."""
    from gmcli.api.client import GmailClient
    from gmcli.auth import store as store_mod

    from conftest import FakeService

    store_mod.FileStore().save("me@example.com", {"token": "t", "refresh_token": "r"})
    store_mod.register_account("me@example.com", "file")
    config = Config()
    config.default_account = "me@example.com"
    config.save()

    service = FakeService()
    service.handlers["users.labels.list"] = {
        "labels": [{"id": "Label_7", "name": "finance", "type": "user"}]
    }
    monkeypatch.setattr(
        GmailClient, "for_account", classmethod(lambda cls, account: cls(service))
    )
    return service


# -- version arithmetic -------------------------------------------------------


@pytest.mark.parametrize(
    "candidate,current,expected",
    [
        ("0.2.0", "0.1.0", True),
        ("0.10.0", "0.9.0", True),      # not string order
        ("1.0", "0.9.9", True),         # different lengths
        ("0.1.0", "0.1.0", False),
        ("0.1.0", "0.2.0", False),
        ("0.2.0rc1", "0.1.0", False),   # a pre-release is never offered
        ("0.2.0b3", "0.1.0", False),
        ("not-a-version", "0.1.0", False),
    ],
)
def test_is_newer(candidate, current, expected):
    assert update.is_newer(candidate, current) is expected


def test_a_post_release_still_counts_as_its_release():
    assert update.release_tuple("0.2.0.post1") == (0, 2, 0)


# -- the check ----------------------------------------------------------------


def test_the_notice_names_both_versions_and_the_way_out(installed):
    update._write_state(latest="0.9.9", checked_at=time.time())
    notice = update.pending_notice("0.1.0")
    assert "0.1.0" in notice and "0.9.9" in notice and "gmail --upgrade" in notice


def test_no_notice_when_the_latest_is_what_is_installed(installed):
    update._write_state(latest="0.1.0", checked_at=time.time())
    assert update.pending_notice("0.1.0") is None


def test_no_notice_before_any_check_has_run(installed):
    assert update.pending_notice("0.1.0") is None


def test_the_check_runs_at_most_once_a_day(installed, monkeypatch):
    calls = []
    monkeypatch.setattr(update, "latest_version", lambda **kw: calls.append(1) or "0.9.9")

    first = update.start_check(Config())
    first.join(timeout=5)
    assert calls == [1]

    assert update.start_check(Config()) is None, "second run inside the interval"
    assert calls == [1]


def test_a_stale_check_runs_again(installed, monkeypatch):
    monkeypatch.setattr(update, "latest_version", lambda **kw: "0.9.9")
    update._write_state(checked_at=time.time() - update.CHECK_INTERVAL - 1)
    thread = update.start_check(Config())
    assert thread is not None
    thread.join(timeout=5)
    assert update.pending_notice("0.1.0")


def test_a_failed_check_still_counts_as_today(installed, monkeypatch):
    # Offline should mean one attempt a day, not one attempt per command.
    monkeypatch.setattr(update, "latest_version", lambda **kw: None)
    thread = update.start_check(Config())
    thread.join(timeout=5)
    assert json.loads(update.state_path().read_text())["checked_at"] > 0
    assert update.start_check(Config()) is None


def test_the_check_is_off_by_environment(installed, monkeypatch):
    monkeypatch.setenv("GMCLI_NO_UPDATE_CHECK", "1")
    assert update.checks_enabled(Config()) is False


def test_the_check_is_off_by_config(installed):
    config = Config()
    config.update.check = False
    assert update.checks_enabled(config) is False


def test_a_source_checkout_is_never_nagged(isolated_dirs, monkeypatch):
    monkeypatch.setattr(update, "running_from_checkout", lambda: True)
    assert update.checks_enabled(Config()) is False


def test_config_round_trips_the_switch(isolated_dirs):
    config = Config()
    config.update.check = False
    config.save()
    assert Config.load().update.check is False


# -- installing it ------------------------------------------------------------


def plan_under(monkeypatch, prefix: str, available: tuple[str, ...] = ()):
    monkeypatch.setattr(update.sys, "prefix", prefix)
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/bin/{name}" if name in available else None
    )
    return update.upgrade_plan()


def test_a_pipx_install_is_upgraded_by_pipx(monkeypatch):
    argv, name = plan_under(
        monkeypatch, "/home/x/.local/share/pipx/venvs/gmcli", ("pipx",)
    )
    assert (argv, name) == (["pipx", "upgrade", "gmcli"], "pipx")


def test_a_uv_tool_install_is_upgraded_by_uv(monkeypatch):
    argv, name = plan_under(
        monkeypatch, "/home/x/.local/share/uv/tools/gmcli", ("uv",)
    )
    assert (argv, name) == (["uv", "tool", "upgrade", "gmcli"], "uv")


def test_anything_else_falls_back_to_pip(monkeypatch):
    argv, name = plan_under(monkeypatch, "/usr", ("pipx", "uv"))
    assert name == "pip" and argv[1:] == ["-m", "pip", "install", "--upgrade", "gmcli"]


def test_a_pipx_layout_without_pipx_installed_falls_back(monkeypatch):
    _, name = plan_under(monkeypatch, "/home/x/.local/share/pipx/venvs/gmcli", ())
    assert name == "pip"


def test_upgrade_refuses_to_touch_a_checkout(isolated_dirs, monkeypatch):
    monkeypatch.setattr(update, "running_from_checkout", lambda: True)
    with pytest.raises(UsageError) as exc:
        update.upgrade(echo=lambda *a: None)
    assert "git pull" in exc.value.hint


def test_upgrade_says_so_when_already_current(installed, monkeypatch):
    monkeypatch.setattr(update, "fetch_latest", lambda **kw: (update.__version__, None))
    said = []
    assert update.upgrade(echo=said.append) == 0
    assert any("latest" in line for line in said)


def test_upgrade_runs_the_plan_and_returns_its_status(installed, monkeypatch):
    monkeypatch.setattr(update, "fetch_latest", lambda **kw: ("99.0.0", None))
    monkeypatch.setattr(update, "upgrade_plan", lambda: (["installer", "go"], "pip"))
    ran = []

    class Result:
        returncode = 7

    monkeypatch.setattr(update.subprocess, "run", lambda argv: ran.append(argv) or Result())
    assert update.upgrade(echo=lambda *a: None) == 7
    assert ran == [["installer", "go"]]


@pytest.mark.parametrize(
    "problem", ["PyPI could not be reached", "gmcli is not published on PyPI"]
)
def test_upgrade_says_which_of_the_two_failures_it_hit(installed, monkeypatch, problem):
    monkeypatch.setattr(update, "fetch_latest", lambda **kw: (None, problem))
    with pytest.raises(ApiError) as exc:
        update.upgrade(echo=lambda *a: None)
    assert problem in exc.value.message


# -- through the CLI ----------------------------------------------------------


def test_the_upgrade_flag_exits_with_the_installer_status(installed, monkeypatch):
    monkeypatch.setattr(update, "upgrade", lambda **kw: 3)
    result = runner.invoke(app, ["--upgrade"])
    assert result.exit_code == 3


def test_the_upgrade_flag_maps_an_error_to_its_exit_code(installed, monkeypatch):
    def refuse(**kwargs):
        raise UsageError("nope", hint="try this")

    monkeypatch.setattr(update, "upgrade", refuse)
    result = runner.invoke(app, ["--upgrade"])
    assert result.exit_code == 2


def test_json_output_is_never_polluted_by_a_notice(logged_in, monkeypatch):
    update._write_state(latest="99.0.0", checked_at=time.time())
    monkeypatch.setattr(update, "stderr_is_terminal", lambda: True)
    started = []
    monkeypatch.setattr(update, "start_check", lambda *a, **kw: started.append(1))

    result = runner.invoke(app, ["--json", "labels", "list"])
    assert result.exit_code == 0
    json.loads(result.stdout)  # stdout is the document and nothing else
    assert started == [], "no check may be started on a pipeline"


def test_no_notice_when_stderr_is_not_a_terminal(logged_in, monkeypatch):
    # A notice in a script's output is noise the script cannot use.
    update._write_state(latest="99.0.0", checked_at=time.time())
    monkeypatch.setattr(update, "stderr_is_terminal", lambda: False)
    result = runner.invoke(app, ["labels", "list"])
    assert "Update available" not in result.output


def test_the_notice_prints_after_the_command_on_a_terminal(logged_in, monkeypatch):
    update._write_state(latest="99.0.0", checked_at=time.time())
    monkeypatch.setattr(update, "stderr_is_terminal", lambda: True)
    monkeypatch.setattr(update, "start_check", lambda *a, **kw: None)

    result = runner.invoke(app, ["labels", "list"])
    output = result.output
    assert "Update available" in output
    assert output.index("finance") < output.index("Update available")


def test_quiet_suppresses_the_notice(logged_in, monkeypatch):
    update._write_state(latest="99.0.0", checked_at=time.time())
    monkeypatch.setattr(update, "stderr_is_terminal", lambda: True)
    result = runner.invoke(app, ["--quiet", "labels", "list"])
    assert "Update available" not in result.output

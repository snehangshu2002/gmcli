"""End-to-end command tests through the real Typer app.

Every command runs its actual code path — argument parsing, context building,
API calls, rendering — against the fake service. Nothing touches the network.
"""

from __future__ import annotations

import base64
import json

import pytest
from typer.testing import CliRunner

from gmcli.cli import app

from conftest import FakeService, make_message

runner = CliRunner()

ACCOUNT = "me@example.com"


@pytest.fixture
def env(isolated_dirs, monkeypatch):
    """A logged-in account whose client is the fake service."""
    from gmcli.api.client import GmailClient
    from gmcli.auth import store as store_mod
    from gmcli.config import Config

    store_mod.FileStore().save(ACCOUNT, {"token": "t", "refresh_token": "r"})
    store_mod.register_account(ACCOUNT, "file")

    config = Config()
    config.default_account = ACCOUNT
    config.save()

    service = FakeService()
    # Label list is requested by anything that resolves a label name.
    service.handlers["users.labels.list"] = {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "UNREAD", "name": "UNREAD", "type": "system"},
            {"id": "Label_7", "name": "finance", "type": "user"},
            {"id": "Label_8", "name": "clients/acme", "type": "user"},
        ]
    }

    monkeypatch.setattr(
        GmailClient, "for_account", classmethod(lambda cls, account: cls(service))
    )
    return service


def invoke(*args):
    return runner.invoke(app, list(args))


def seed_threads(service: FakeService, count: int = 3) -> list[str]:
    ids = [f"{i:016x}" for i in range(1, count + 1)]
    service.handlers["users.threads.list"] = {
        "threads": [{"id": tid} for tid in ids]
    }
    service.handlers["users.threads.get"] = lambda kwargs: {
        "id": kwargs["id"],
        "snippet": "snippet text",
        "messages": [
            make_message(
                f"m{kwargs['id']}",
                thread_id=kwargs["id"],
                subject=f"Subject {kwargs['id'][-1]}",
            )
        ],
    }
    return ids


# -- listing -----------------------------------------------------------------


def test_ls_renders_a_table(env):
    seed_threads(env)
    result = invoke("ls")
    assert result.exit_code == 0, result.output
    assert "Subject 1" in result.output
    assert "Dana Whitfield" in result.output


def test_ls_json_is_parseable(env):
    seed_threads(env)
    result = invoke("--json", "ls")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload) == 3
    assert payload[0]["subject"] == "Subject 1"


def test_ls_records_the_listing_for_hash_references(env):
    ids = seed_threads(env)
    invoke("ls")

    from gmcli.cache import Cache

    kind, recorded = Cache(ACCOUNT).get_listing()
    assert kind == "thread"
    assert recorded == ids


def test_ls_defaults_to_the_inbox(env):
    seed_threads(env)
    invoke("ls")
    call = next(c for c in env.calls if c[0] == "users.threads.list")
    assert call[1]["labelIds"] == ["INBOX"]


def test_ls_all_drops_the_inbox_filter(env):
    seed_threads(env)
    invoke("ls", "--all")
    call = next(c for c in env.calls if c[0] == "users.threads.list")
    assert "labelIds" not in call[1]


def test_ls_unread_adds_the_query(env):
    seed_threads(env)
    invoke("ls", "--unread")
    call = next(c for c in env.calls if c[0] == "users.threads.list")
    assert call[1]["q"] == "is:unread"


def test_ls_label_is_resolved_to_an_id(env):
    seed_threads(env)
    invoke("ls", "--label", "finance")
    call = next(c for c in env.calls if c[0] == "users.threads.list")
    assert call[1]["labelIds"] == ["Label_7"]


def test_unknown_label_is_a_clear_error(env):
    seed_threads(env)
    result = invoke("ls", "--label", "nosuchlabel")
    assert result.exit_code == 4
    assert "No label" in result.output


def test_limit_is_passed_through(env):
    seed_threads(env)
    invoke("ls", "-n", "2")
    call = next(c for c in env.calls if c[0] == "users.threads.list")
    assert call[1]["maxResults"] == 2


# -- search ------------------------------------------------------------------


def test_search_passes_the_query_verbatim(env):
    seed_threads(env)
    invoke("search", "from:dana has:attachment")
    call = next(c for c in env.calls if c[0] == "users.threads.list")
    assert call[1]["q"] == "from:dana has:attachment"


def test_search_combines_query_with_flags(env):
    seed_threads(env)
    invoke("search", "from:dana", "--unread", "--after", "2026/01/01")
    call = next(c for c in env.calls if c[0] == "users.threads.list")
    assert call[1]["q"] == "from:dana is:unread after:2026/01/01"


def test_search_expands_a_configured_alias(env):
    from gmcli.config import Config

    config = Config.load()
    config.aliases = {"bills": "subject:invoice"}
    config.save()

    seed_threads(env)
    invoke("search", "bills")
    call = next(c for c in env.calls if c[0] == "users.threads.list")
    assert call[1]["q"] == "subject:invoice"


def test_empty_search_is_a_usage_error(env):
    result = invoke("search")
    assert result.exit_code == 2


def test_search_messages_mode_uses_the_message_endpoint(env):
    env.handlers["users.messages.list"] = {"messages": [{"id": "m1"}]}
    env.handlers["users.messages.get"] = lambda kwargs: make_message(kwargs["id"])
    result = invoke("search", "test", "--messages")
    assert result.exit_code == 0, result.output
    assert any(c[0] == "users.messages.list" for c in env.calls)


# -- read --------------------------------------------------------------------


def test_read_by_hash_reference(env):
    seed_threads(env)
    invoke("ls")
    result = invoke("read", "#2")
    assert result.exit_code == 0, result.output
    assert "Subject 2" in result.output
    assert "Hello there." in result.output


def test_read_without_a_listing_explains_itself(env):
    result = invoke("read", "#1")
    assert result.exit_code == 2
    assert "no previous listing" in result.output


def test_read_folds_quoted_history_by_default(env):
    env.handlers["users.threads.get"] = lambda kwargs: {
        "id": kwargs["id"],
        "messages": [
            make_message(
                "m1",
                body="My reply.\n\nOn Wed, 04 Mar 2026 at 09:14, Dana wrote:\n> old\n> older",
            )
        ],
    }
    result = invoke("read", "0000000000000001")
    assert "My reply." in result.output
    assert "quoted line" in result.output
    assert "> old" not in result.output


def test_read_show_quoted_expands_it(env):
    env.handlers["users.threads.get"] = lambda kwargs: {
        "id": kwargs["id"],
        "messages": [
            make_message(
                "m1",
                body="My reply.\n\nOn Wed, 04 Mar 2026 at 09:14, Dana wrote:\n> old",
            )
        ],
    }
    result = invoke("read", "0000000000000001", "--show-quoted")
    assert "> old" in result.output


# -- modify ------------------------------------------------------------------


def test_archive_removes_the_inbox_label(env):
    seed_threads(env)
    invoke("ls")
    env.handlers["users.threads.modify"] = {"id": "t"}

    result = invoke("archive", "#1")
    assert result.exit_code == 0, result.output
    call = next(c for c in env.calls if c[0] == "users.threads.modify")
    assert call[1]["body"] == {"removeLabelIds": ["INBOX"]}


def test_archive_accepts_a_range(env):
    seed_threads(env)
    invoke("ls")
    env.handlers["users.threads.modify"] = {"id": "t"}

    result = invoke("archive", "#1-3")
    assert result.exit_code == 0, result.output
    assert sum(1 for c in env.calls if c[0] == "users.threads.modify") == 3
    assert "3 conversations" in result.output


def test_mark_read_removes_unread(env):
    seed_threads(env)
    invoke("ls")
    env.handlers["users.threads.modify"] = {"id": "t"}

    invoke("mark", "read", "#1")
    call = next(c for c in env.calls if c[0] == "users.threads.modify")
    assert call[1]["body"] == {"removeLabelIds": ["UNREAD"]}


def test_star_adds_starred(env):
    seed_threads(env)
    invoke("ls")
    env.handlers["users.threads.modify"] = {"id": "t"}

    invoke("mark", "star", "#1")
    call = next(c for c in env.calls if c[0] == "users.threads.modify")
    assert call[1]["body"] == {"addLabelIds": ["STARRED"]}


def test_label_add_resolves_the_name(env):
    seed_threads(env)
    invoke("ls")
    env.handlers["users.threads.modify"] = {"id": "t"}

    invoke("label", "add", "#1", "--label", "finance")
    call = next(c for c in env.calls if c[0] == "users.threads.modify")
    assert call[1]["body"] == {"addLabelIds": ["Label_7"]}


def test_label_add_can_create_the_label(env):
    seed_threads(env)
    invoke("ls")
    env.handlers["users.labels.create"] = {
        "id": "Label_99", "name": "brandnew", "type": "user"
    }
    env.handlers["users.threads.modify"] = {"id": "t"}

    result = invoke("label", "add", "#1", "--label", "brandnew", "--create")
    assert result.exit_code == 0, result.output
    assert any(c[0] == "users.labels.create" for c in env.calls)


def test_trash_uses_the_trash_endpoint(env):
    seed_threads(env)
    invoke("ls")
    env.handlers["users.threads.trash"] = {"id": "t"}

    result = invoke("trash", "#1")
    assert result.exit_code == 0, result.output
    assert any(c[0] == "users.threads.trash" for c in env.calls)


def test_untrash_restores(env):
    seed_threads(env)
    invoke("ls")
    env.handlers["users.threads.untrash"] = {"id": "t"}

    result = invoke("untrash", "#1")
    assert result.exit_code == 0, result.output
    assert any(c[0] == "users.threads.untrash" for c in env.calls)


def test_there_is_no_permanent_delete_command(env):
    """The scope cannot do it, so the command must not exist."""
    result = invoke("delete", "#1")
    assert result.exit_code != 0
    assert "No such command" in result.output or "Usage" in result.output


def test_messages_flag_uses_batch_modify(env):
    env.handlers["users.messages.list"] = {"messages": [{"id": "m1"}, {"id": "m2"}]}
    env.handlers["users.messages.get"] = lambda kwargs: make_message(kwargs["id"])
    env.handlers["users.messages.batchModify"] = {}

    invoke("ls", "--messages")
    result = invoke("archive", "#1-2", "--messages")
    assert result.exit_code == 0, result.output
    call = next(c for c in env.calls if c[0] == "users.messages.batchModify")
    assert call[1]["body"]["ids"] == ["m1", "m2"]


# -- send --------------------------------------------------------------------


def test_send_dry_run_calls_nothing(env):
    result = invoke(
        "send", "--to", "a@example.com", "--subject", "Hi", "--body", "text",
        "--dry-run",
    )
    assert result.exit_code == 0, result.output
    assert not any(c[0] == "users.messages.send" for c in env.calls)
    assert "nothing was sent" in result.output


def test_send_posts_base64url_raw(env):
    env.handlers["users.messages.send"] = {"id": "sent1", "threadId": "t1"}

    result = invoke(
        "send", "--to", "a@example.com", "--subject", "Hi", "--body", "the body"
    )
    assert result.exit_code == 0, result.output

    call = next(c for c in env.calls if c[0] == "users.messages.send")
    raw = call[1]["body"]["raw"]
    pad = "=" * (-len(raw) % 4)
    decoded = base64.urlsafe_b64decode(raw + pad).decode()
    assert "To: a@example.com" in decoded
    assert "Subject: Hi" in decoded
    assert "the body" in decoded


def test_send_rejects_a_bad_address(env):
    result = invoke("send", "--to", "notanemail", "--subject", "x", "--body", "y")
    assert result.exit_code == 2
    assert "not a valid email" in result.output


def test_send_requires_a_recipient(env):
    result = invoke("send", "--subject", "x", "--body", "y")
    assert result.exit_code == 2


def test_send_attaches_a_file(env, tmp_path):
    env.handlers["users.messages.send"] = {"id": "s", "threadId": "t"}
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4")

    result = invoke(
        "send", "--to", "a@example.com", "--subject", "x", "--body", "y",
        "-a", str(doc),
    )
    assert result.exit_code == 0, result.output

    call = next(c for c in env.calls if c[0] == "users.messages.send")
    raw = call[1]["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "report.pdf" in decoded
    assert "application/pdf" in decoded


def test_send_json_reports_the_id(env):
    env.handlers["users.messages.send"] = {"id": "sent1", "threadId": "t1"}
    result = invoke(
        "--json", "send", "--to", "a@example.com", "--subject", "x", "--body", "y"
    )
    payload = json.loads(result.stdout)
    assert payload["id"] == "sent1"
    assert payload["thread_id"] == "t1"


def test_reply_threads_correctly(env):
    ids = seed_threads(env)
    invoke("ls")
    env.handlers["users.messages.send"] = {"id": "s", "threadId": ids[0]}

    result = invoke("reply", "#1", "--body", "Sounds good.")
    assert result.exit_code == 0, result.output

    call = next(c for c in env.calls if c[0] == "users.messages.send")
    assert call[1]["body"]["threadId"] == ids[0]

    raw = call[1]["body"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "In-Reply-To: <parent@mail.example.com>" in decoded
    assert "References: <parent@mail.example.com>" in decoded
    assert "Subject: Re: Subject 1" in decoded


# -- labels ------------------------------------------------------------------


def test_labels_list(env):
    result = invoke("labels", "list")
    assert result.exit_code == 0, result.output
    assert "finance" in result.output


def test_labels_list_user_only(env):
    result = invoke("labels", "list", "--user")
    assert "finance" in result.output
    assert "INBOX" not in result.output


def test_labels_create(env):
    env.handlers["users.labels.create"] = {
        "id": "Label_9", "name": "newone", "type": "user"
    }
    result = invoke("labels", "create", "newone")
    assert result.exit_code == 0, result.output


def test_labels_create_rejects_a_duplicate(env):
    result = invoke("labels", "create", "finance")
    assert result.exit_code == 2
    assert "already exists" in result.output


def test_system_labels_cannot_be_deleted(env):
    result = invoke("labels", "delete", "INBOX", "--yes")
    assert result.exit_code == 2
    assert "system label" in result.output


def test_system_labels_cannot_be_renamed(env):
    result = invoke("labels", "rename", "INBOX", "Mailbox")
    assert result.exit_code == 2


# -- attachments -------------------------------------------------------------


def test_attachments_list(env):
    env.handlers["users.threads.get"] = lambda kwargs: {
        "id": kwargs["id"],
        "messages": [
            make_message(
                "m1",
                attachments=[("report.pdf", "application/pdf", 1024)],
            )
        ],
    }
    result = invoke("attachments", "list", "0000000000000001")
    assert result.exit_code == 0, result.output
    assert "report.pdf" in result.output


def test_attachments_download(env, tmp_path):
    env.handlers["users.threads.get"] = lambda kwargs: {
        "id": kwargs["id"],
        "messages": [
            make_message("m1", attachments=[("report.pdf", "application/pdf", 8)])
        ],
    }
    env.handlers["users.messages.attachments.get"] = {
        "data": base64.urlsafe_b64encode(b"PDFBYTES").decode()
    }

    result = invoke(
        "attachments", "download", "0000000000000001", "--all", "-o", str(tmp_path)
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "report.pdf").read_bytes() == b"PDFBYTES"


def test_ambiguous_download_asks_for_a_selector(env, tmp_path):
    env.handlers["users.threads.get"] = lambda kwargs: {
        "id": kwargs["id"],
        "messages": [
            make_message(
                "m1",
                attachments=[
                    ("a.pdf", "application/pdf", 1),
                    ("b.pdf", "application/pdf", 1),
                ],
            )
        ],
    }
    result = invoke(
        "attachments", "download", "0000000000000001", "-o", str(tmp_path)
    )
    assert result.exit_code == 2
    assert "--all" in result.output


# -- auth and global behaviour ----------------------------------------------

def test_auth_status_reports_the_account(env):
    result = invoke("auth", "status")
    assert result.exit_code == 0, result.output
    assert ACCOUNT in result.output


def test_auth_status_json(env):
    result = invoke("--json", "auth", "status")
    payload = json.loads(result.stdout)
    assert payload["active_account"] == ACCOUNT
    assert payload["scopes"] == ["https://www.googleapis.com/auth/gmail.modify"]


def test_auth_list_marks_the_default(env):
    result = invoke("auth", "list")
    assert ACCOUNT in result.output


def test_auth_switch_rejects_an_unknown_account(env):
    result = invoke("auth", "switch", "nobody@example.com")
    assert result.exit_code == 2


def test_no_account_gives_an_actionable_error(isolated_dirs):
    result = invoke("ls")
    assert result.exit_code == 3
    assert "auth login" in result.output


def test_version():
    result = invoke("--version")
    assert result.exit_code == 0
    assert "gmcli" in result.output


def test_cache_clear(env):
    seed_threads(env)
    invoke("ls")
    result = invoke("cache", "clear")
    assert result.exit_code == 0, result.output

    from gmcli.cache import Cache

    assert Cache(ACCOUNT).get_listing() is None


# -- setup wizard ------------------------------------------------------------


def desktop_client_file(tmp_path):
    import json as _json

    path = tmp_path / "client_secret_999.json"
    path.write_text(
        _json.dumps(
            {
                "installed": {
                    "client_id": "999-xyz.apps.googleusercontent.com",
                    "client_secret": "shh",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    return path


@pytest.fixture
def fake_login(monkeypatch):
    """Stub the consent flow, which is the one thing that needs a browser."""
    from gmcli.auth.client_config import ClientConfig
    from gmcli.auth.store import FileStore
    from gmcli.commands import setup as setup_mod

    calls = []

    def _login(**kwargs):
        calls.append(kwargs)
        store = FileStore()
        store.save(ACCOUNT, {"token": "t", "refresh_token": "r"})
        return ACCOUNT, store, ClientConfig("id", "secret", "installed client")

    monkeypatch.setattr(setup_mod, "login", _login)
    return calls


def test_setup_is_registered(env):
    result = invoke("auth", "setup", "--help")
    assert result.exit_code == 0
    assert "step by step" in result.output.lower()


def test_setup_with_credentials_skips_the_walkthrough(
    isolated_dirs, tmp_path, fake_login
):
    path = desktop_client_file(tmp_path)
    result = invoke("auth", "setup", "--credentials", str(path))

    assert result.exit_code == 0, result.output
    assert "Installed OAuth client" in result.output
    assert f"Signed in as {ACCOUNT}" in result.output.replace("\n", " ")
    assert len(fake_login) == 1


def test_setup_installs_the_client_for_later_logins(
    isolated_dirs, tmp_path, fake_login
):
    from gmcli.auth.client_config import resolve_client
    from gmcli.config import client_secret_path

    invoke("auth", "setup", "--credentials", str(desktop_client_file(tmp_path)))

    assert client_secret_path().exists()
    assert resolve_client().client_id == "999-xyz.apps.googleusercontent.com"


def test_setup_sets_the_default_account(isolated_dirs, tmp_path, fake_login):
    from gmcli.config import Config

    invoke("auth", "setup", "--credentials", str(desktop_client_file(tmp_path)))
    assert Config.load().default_account == ACCOUNT


def test_setup_rejects_a_web_client(isolated_dirs, tmp_path):
    import json as _json

    path = tmp_path / "web.json"
    path.write_text(_json.dumps({"web": {"client_id": "x", "client_secret": "y"}}))

    result = invoke("auth", "setup", "--credentials", str(path))
    assert result.exit_code == 3
    assert "Web application" in result.output
    assert "Desktop app" in result.output


def test_setup_rejects_a_service_account_key(isolated_dirs, tmp_path):
    import json as _json

    path = tmp_path / "sa.json"
    path.write_text(_json.dumps({"type": "service_account", "private_key": "x"}))

    result = invoke("auth", "setup", "--credentials", str(path))
    assert result.exit_code == 3
    assert "service-account" in result.output


def test_setup_needs_a_terminal_when_interactive(isolated_dirs, monkeypatch):
    """Non-interactive callers get a pointer to the unattended path."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    result = invoke("auth", "setup")
    assert result.exit_code == 2
    assert "GMCLI_CLIENT_ID" in result.output


def test_setup_json_reports_the_client_source(isolated_dirs, tmp_path, fake_login):
    result = invoke(
        "--json", "auth", "setup", "--credentials", str(desktop_client_file(tmp_path))
    )
    payload = json.loads(result.stdout)
    assert payload["account"] == ACCOUNT
    assert payload["client_source"]


def test_status_reports_where_the_client_came_from(env, monkeypatch):
    monkeypatch.setenv("GMCLI_CLIENT_ID", "111-abc.apps.googleusercontent.com")
    result = invoke("--json", "auth", "status")

    payload = json.loads(result.stdout)
    assert payload["client_secret_configured"] is True
    assert "environment" in payload["client_source"]


def test_status_without_a_client_says_missing(env):
    result = invoke("--json", "auth", "status")
    payload = json.loads(result.stdout)
    assert payload["client_secret_configured"] is False
    assert payload["client_source"] is None


def test_doctor_points_at_the_wizard_when_no_client(isolated_dirs):
    result = invoke("auth", "doctor")
    assert "gmail auth setup" in result.output


def test_env_var_client_needs_no_file(isolated_dirs, monkeypatch):
    """A container can carry the client entirely in the environment."""
    from gmcli.auth.client_config import resolve_client
    from gmcli.config import client_secret_path

    monkeypatch.setenv("GMCLI_CLIENT_ID", "env-id")
    monkeypatch.setenv("GMCLI_CLIENT_SECRET", "env-secret")

    assert not client_secret_path().exists()
    assert resolve_client().client_id == "env-id"

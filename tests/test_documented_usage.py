"""Every command line printed in the README's Usage section, executed.

The README is the contract most users read first. This runs each documented
invocation against the fake service, so a flag cannot be renamed or dropped
without a test going red — the same guard ``test_output.py`` puts on the JSON
key names.

Sends run with ``--dry-run`` where the README shows a real send, since the
point here is that the command line parses and assembles, not that a fake
service accepts it.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from gmcli.cli import app

from conftest import FakeService, make_message

runner = CliRunner()
ACCOUNT = "me@example.com"


@pytest.fixture
def env(isolated_dirs, monkeypatch, tmp_path):
    from gmcli.api.client import GmailClient
    from gmcli.auth import store as store_mod
    from gmcli.config import Config

    store_mod.FileStore().save(ACCOUNT, {"token": "t", "refresh_token": "r"})
    store_mod.register_account(ACCOUNT, "file")
    config = Config()
    config.default_account = ACCOUNT
    config.save()

    service = FakeService()
    service.handlers["users.labels.list"] = {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "Label_7", "name": "finance", "type": "user"},
            {"id": "Label_8", "name": "triage", "type": "user"},
            {"id": "Label_9", "name": "clients/acme", "type": "user"},
        ]
    }
    service.handlers["users.labels.get"] = lambda kw: {
        "id": kw["id"], "name": kw["id"], "type": "system",
        "messagesTotal": 10, "messagesUnread": 2,
    }
    service.handlers["users.labels.create"] = lambda kw: {
        "id": "Label_new", "name": kw["body"]["name"], "type": "user"
    }
    ids = [f"{i:016x}" for i in range(1, 8)]
    service.handlers["users.threads.list"] = {"threads": [{"id": t} for t in ids]}
    service.handlers["users.threads.get"] = lambda kw: {
        "id": kw["id"], "snippet": "snippet",
        "messages": [
            make_message(
                f"m{kw['id']}", thread_id=kw["id"], subject=f"Subject {kw['id'][-1]}",
                attachments=[("report.pdf", "application/pdf", 1024),
                             ("notes.txt", "text/plain", 32)],
            )
        ],
    }
    service.handlers["users.messages.list"] = {"messages": [{"id": m} for m in ids]}
    service.handlers["users.messages.get"] = lambda kw: make_message(
        kw["id"], attachments=[("report.pdf", "application/pdf", 1024)]
    )
    service.handlers["users.messages.attachments.get"] = {"data": "aGVsbG8"}
    service.handlers["users.messages.batchModify"] = {}
    service.handlers["users.threads.modify"] = lambda kw: {"id": kw["id"]}
    service.handlers["users.threads.trash"] = lambda kw: {"id": kw["id"]}
    service.handlers["users.threads.untrash"] = lambda kw: {"id": kw["id"]}
    service.handlers["users.messages.send"] = {"id": "s1", "threadId": "t1"}
    service.handlers["users.drafts.create"] = {"id": "d1", "message": {"id": "m1"}}
    service.handlers["users.drafts.list"] = {"drafts": [{"id": "d1", "message": {"id": "m1"}}]}
    service.handlers["users.drafts.get"] = lambda kw: {
        "id": kw["id"], "message": make_message("m1", subject="Later")
    }
    service.handlers["users.drafts.send"] = {"id": "m1", "threadId": "t1"}

    monkeypatch.setattr(
        GmailClient, "for_account", classmethod(lambda cls, account: cls(service))
    )
    return service


def run(*args: str):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, (
        f"`gmail {' '.join(args)}` exited {result.exit_code}\n{result.output}"
        f"\n{result.exception!r}"
    )
    return result


# -- Reading ------------------------------------------------------------------


def test_reading_commands(env):
    run("ls")
    run("ls", "-n", "50", "--unread")
    run("ls", "--label", "finance")
    run("ls", "--messages")
    run("ls")
    run("read", "#3")
    run("read", "#3", "--latest")
    run("read", "#3", "--show-quoted")


def test_read_raw_dumps_the_original_source(env):
    import base64

    env.handlers["users.messages.get"] = lambda kw: {
        **make_message(kw["id"]),
        "raw": base64.urlsafe_b64encode(b"From: a@b\r\n\r\nbody").decode(),
    }
    run("ls")
    assert "From: a@b" in run("read", "#3", "--raw").output


# -- Searching ----------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("search", "from:dana after:2026/01/01 has:attachment"),
        ("search", "subject:invoice larger:5M"),
        ("search", '"exact phrase" -label:promotions'),
        ("search", "is:starred", "--limit", "100"),
        ("search", "from:github", "--unread", "--attachments"),
    ],
)
def test_every_documented_search(env, args):
    run(*args)


def test_convenience_flags_compose_with_a_raw_query(env):
    run("search", "from:github", "--unread", "--attachments")
    queries = [kw.get("q") for path, kw in env.calls if path == "users.threads.list"]
    assert queries[-1] == "from:github is:unread has:attachment"


# -- The #N shorthand ---------------------------------------------------------


def test_the_hash_shorthand_in_all_three_forms(env):
    run("ls")
    run("archive", "#1")
    run("label", "add", "#1-5", "--label", "triage")
    run("mark", "read", "#1,3,7")

    modified = [kw for path, kw in env.calls if path == "users.threads.modify"]
    assert modified[0]["body"] == {"removeLabelIds": ["INBOX"]}
    # `#1-5` expanded to a range of five, `#1,3,7` to a selection of three —
    # all as conversations, which is what these commands act on by default.
    assert sum(1 for kw in modified if kw["body"] == {"addLabelIds": ["Label_8"]}) == 5
    marked = [kw["id"] for kw in modified if kw["body"] == {"removeLabelIds": ["UNREAD"]}]
    assert marked == [f"{n:016x}" for n in (1, 3, 7)]


def test_full_ids_always_work_so_scripts_never_depend_on_state(env):
    run("archive", f"{1:016x}")


# -- Sending ------------------------------------------------------------------


def test_send_with_an_attachment(env, tmp_path):
    pdf = tmp_path / "q3.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    run("send", "--to", "dana@example.com", "--subject", "Q3",
        "--body", "Numbers attached.", "-a", str(pdf))


def test_recipients_repeat_or_comma_separate(env):
    run("send", "--to", "a@x.com", "--to", "b@x.com, c@x.com", "--subject", "Hi",
        "--body", "x", "--dry-run")


def test_body_from_stdin(env, monkeypatch):
    result = runner.invoke(
        app, ["send", "--to", "ops@example.com", "--subject", "Deploy OK"],
        input="deployed\n",
    )
    assert result.exit_code == 0


def test_body_from_a_file(env, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("# notes\n", encoding="utf-8")
    run("send", "--to", "dana@example.com", "--subject", "Notes",
        "--body-file", str(notes))


def test_dry_run_prints_the_mime_and_calls_nothing(env, tmp_path):
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4 fake")
    before = len(env.calls)
    result = run("send", "--to", "dana@example.com", "--subject", "Test",
                 "--body", "hi", "-a", str(report), "--dry-run")
    assert "Content-Type: multipart/mixed" in result.output
    assert "report.pdf" in result.output
    assert len(env.calls) == before


def test_replies_stay_in_the_conversation(env):
    run("ls")
    run("reply", "#2", "--body", "Sounds good.")
    sent = [kw for path, kw in env.calls if path == "users.messages.send"]
    assert sent[-1]["body"]["threadId"] == f"{2:016x}"

    import base64

    raw = sent[-1]["body"]["raw"]
    text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    assert "In-Reply-To:" in text and "References:" in text


def test_reply_all_and_forward(env):
    run("ls")
    run("reply", "#2", "--all", "--body", "Looping everyone in.")
    run("forward", "#2", "--to", "legal@example.com", "--body", "FYI")


def test_drafts(env):
    run("draft", "create", "--to", "dana@example.com", "--subject", "Later",
        "--body", "text")
    run("draft", "list")
    run("draft", "send", "d1")


# -- Organizing ---------------------------------------------------------------


def test_organizing_commands(env):
    run("ls")
    run("archive", "#1")
    run("unarchive", "#1")
    run("mark", "read", "#1-5")
    run("mark", "star", "#2")
    run("trash", "#4")
    run("untrash", "#4")


def test_label_commands(env):
    run("labels", "list")
    run("labels", "list", "--counts")
    run("labels", "create", "clients/acme2")
    run("ls")
    run("label", "add", "#1", "--label", "clients/acme")
    run("label", "add", "#1", "--label", "newthing", "--create")
    run("label", "remove", "#1", "--label", "triage")


def test_messages_flag_acts_on_individual_messages(env):
    run("ls")
    run("archive", "#1", "--messages")
    assert any(path == "users.messages.batchModify" for path, _ in env.calls)


# -- Attachments --------------------------------------------------------------


def test_attachment_commands(env, tmp_path):
    run("ls")
    run("attachments", "list", "#1")
    run("attachments", "download", "#1", "--all", "-o", str(tmp_path))
    run("attachments", "download", "#1", "--index", "2", "-o", str(tmp_path))
    run("attachments", "download", "#1", "--name", "*.pdf", "-o", str(tmp_path))
    assert (tmp_path / "report.pdf").exists()
    assert (tmp_path / "notes.txt").exists()


def test_colliding_filenames_get_a_suffix_rather_than_overwriting(env, tmp_path):
    run("ls")
    run("attachments", "download", "#1", "--name", "*.pdf", "-o", str(tmp_path))
    run("attachments", "download", "#1", "--name", "*.pdf", "-o", str(tmp_path))
    assert (tmp_path / "report.pdf").exists()
    assert (tmp_path / "report (1).pdf").exists()


# -- Install: staying up to date ----------------------------------------------


def test_the_upgrade_flag_documented_under_install(env, monkeypatch):
    """`gmail --upgrade` — the installer itself is stubbed, the flag is not."""
    from gmcli import update

    monkeypatch.setattr(update, "upgrade", lambda **kwargs: 0)
    result = runner.invoke(app, ["--upgrade"])
    assert result.exit_code == 0

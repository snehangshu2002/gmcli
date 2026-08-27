"""Tests for the interactive UI.

Offline like the rest of the suite. ``ScriptedKeys`` replaces the terminal, so
the real event loop, the real actions, and the real API calls against
``FakeService`` all run — only the keyboard is faked.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console
from typer.testing import CliRunner

from gmcli.cli import app as cli_app
from gmcli.config import Config
from gmcli.context import AppContext
from gmcli.ui import graphics, render
from gmcli.ui.app import MailApp
from gmcli.ui.keys import ESC, LineEditor, Mouse, ScriptedKeys, decode, parse
from gmcli.ui.state import HELP, LIST, READER, STANDARD_MAILBOXES, build_mailboxes

from conftest import FakeService, make_message

ACCOUNT = "me@example.com"


# -- key decoding -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("j", "j"),
        ("\r", "enter"),
        ("\n", "enter"),
        ("\t", "tab"),
        (" ", "space"),
        ("\x7f", "backspace"),
        ("\x04", "ctrl-d"),
        ("\x03", "ctrl-c"),
        ("?", "?"),
    ],
)
def test_plain_keys_decode_to_names(raw, expected):
    assert decode(raw) == expected


@pytest.mark.parametrize(
    "tail, expected",
    [
        ("[A", "up"),
        ("[B", "down"),
        ("[C", "right"),
        ("[D", "left"),
        ("OA", "up"),
        ("[5~", "pageup"),
        ("[6~", "pagedown"),
        ("[3~", "delete"),
        ("[Z", "shift-tab"),
        ("[1;5A", "up"),
    ],
)
def test_escape_sequences_decode(tail, expected):
    assert decode(ESC, iter(tail)) == expected


def test_a_lone_escape_is_escape_not_a_sequence():
    """Nothing follows a real Esc press, which is how it is told apart."""
    assert decode(ESC, iter("")) == "escape"


def test_unknown_sequences_do_not_masquerade_as_letters():
    assert decode(ESC, iter("[99x")) == "unknown"


# -- the footer line editor ---------------------------------------------------


def test_line_editor_types_and_submits():
    editor = LineEditor("search: ")
    for key in ("f", "r", "o", "m", ":", "space", "b"):
        assert editor.handle(key) is None
    assert editor.text == "from: b"
    assert editor.handle("enter") == "submit"


def test_line_editor_edits_in_the_middle():
    editor = LineEditor("q: ", "abcd")
    editor.handle("left")
    editor.handle("left")
    editor.handle("X")
    assert editor.text == "abXcd"
    editor.handle("backspace")
    assert editor.text == "abcd"


def test_line_editor_kill_word_and_line():
    editor = LineEditor("q: ", "from:dana has:attachment")
    editor.handle("ctrl-w")
    assert editor.text == "from:dana "
    editor.handle("ctrl-u")
    assert editor.text == ""


def test_line_editor_cancels_on_escape():
    assert LineEditor("q: ", "text").handle("escape") == "cancel"


# -- fixtures -----------------------------------------------------------------


@pytest.fixture
def service(isolated_dirs, monkeypatch) -> FakeService:
    from gmcli.api.client import GmailClient

    svc = FakeService()
    svc.handlers["users.labels.list"] = {
        "labels": [
            {"id": "INBOX", "name": "INBOX", "type": "system", "messagesUnread": 4},
            {"id": "STARRED", "name": "STARRED", "type": "system"},
            {"id": "Label_7", "name": "finance", "type": "user", "messagesUnread": 2},
        ]
    }
    ids = [f"{i:016x}" for i in range(1, 4)]
    svc.handlers["users.threads.list"] = {"threads": [{"id": t} for t in ids]}
    svc.handlers["users.threads.get"] = lambda kw: {
        "id": kw["id"],
        "snippet": "snippet",
        "messages": [
            make_message(
                f"m{kw['id']}",
                thread_id=kw["id"],
                subject=f"Subject {kw['id'][-1]}",
                body="Visible body.\n\nOn Mon, someone wrote:\n> older text\n> more",
            )
        ],
    }
    svc.handlers["users.labels.get"] = lambda kw: {
        "id": kw["id"], "name": kw["id"], "type": "system",
        "messagesTotal": 10, "messagesUnread": 4,
    }
    svc.handlers["users.messages.batchModify"] = {}
    svc.handlers["users.threads.modify"] = lambda kw: {"id": kw["id"]}
    svc.handlers["users.threads.trash"] = lambda kw: {"id": kw["id"]}
    svc.handlers["users.messages.list"] = {"messages": [{"id": "m1"}]}
    svc.handlers["users.messages.get"] = lambda kw: make_message(kw["id"])

    monkeypatch.setattr(
        GmailClient, "for_account", classmethod(lambda cls, account: cls(svc))
    )
    return svc


@pytest.fixture
def ctx(service) -> AppContext:
    app_ctx = AppContext(config=Config())
    app_ctx._account = ACCOUNT
    return app_ctx


def build(ctx: AppContext, keys: list[str]) -> MailApp:
    """A UI whose keystrokes are already queued, loaded but not yet run."""
    console = Console(
        file=io.StringIO(), width=100, height=30, force_terminal=True, color_system=None
    )
    ui = MailApp(ctx, console=console, keys=ScriptedKeys(keys))
    ui.load_mailboxes()
    ui.reload()
    return ui


def press(ui: MailApp, *keys: str) -> MailApp:
    for key in keys:
        ui.dispatch(key)
    return ui


def screen(ui: MailApp) -> str:
    console = Console(file=io.StringIO(), width=100, height=30, no_color=True)
    console.print(render.frame(ui.state, 100, 30))
    return console.file.getvalue()


def paths(service: FakeService) -> list[str]:
    return [path for path, _ in service.calls]


# -- mailboxes ----------------------------------------------------------------


def test_sidebar_lists_standard_mailboxes_then_user_labels():
    from gmcli.models import Label

    boxes = build_mailboxes(
        [
            Label("Label_9", "zebra", "user"),
            Label("INBOX", "INBOX", "system"),
            Label("Label_7", "finance", "user"),
        ]
    )
    assert [b.title for b in boxes[: len(STANDARD_MAILBOXES)]] == [
        b.title for b in STANDARD_MAILBOXES
    ]
    assert [b.title for b in boxes[len(STANDARD_MAILBOXES) :]] == ["finance", "zebra"]


def test_opening_loads_the_inbox(ctx, service):
    ui = build(ctx, [])
    assert len(ui.state.threads) == 3
    assert ui.state.mailbox.title == "Inbox"
    assert ("users.threads.list", {"userId": "me", "includeSpamTrash": False,
                                   "labelIds": ["INBOX"], "maxResults": 50}) in service.calls


def test_the_listing_is_shared_with_the_cli_numbering(ctx):
    """`#N` after quitting the UI means the rows the UI last showed."""
    ui = build(ctx, [])
    kind, ids = ctx.cache.get_listing()
    assert kind == "thread"
    assert ids == [t.id for t in ui.state.threads]


# -- moving -------------------------------------------------------------------


def test_cursor_moves_and_clamps(ctx):
    ui = build(ctx, [])
    press(ui, "j", "j", "j", "j")
    assert ui.state.cursor == 2
    press(ui, "k", "k", "k", "k")
    assert ui.state.cursor == 0


def test_g_and_shift_g_jump_to_the_ends(ctx):
    ui = build(ctx, [])
    press(ui, "G")
    assert ui.state.cursor == 2
    press(ui, "g")
    assert ui.state.cursor == 0


def test_tab_switches_panes_and_enter_opens_a_mailbox(ctx, service):
    ui = build(ctx, [])
    press(ui, "tab")
    assert ui.state.focus == "sidebar"
    press(ui, "j", "j", "enter")  # Inbox -> Unread -> Starred
    assert ui.state.mailbox.title == "Starred"
    assert ui.state.focus == "list"
    assert {"userId": "me", "includeSpamTrash": False, "labelIds": ["STARRED"],
            "maxResults": 50} in [
        kwargs for path, kwargs in service.calls if path == "users.threads.list"
    ]


def test_q_quits(ctx):
    ui = build(ctx, [])
    press(ui, "q")
    assert ui.state.quit is True


# -- reading ------------------------------------------------------------------


def test_enter_opens_the_conversation_and_marks_it_read(ctx, service):
    ui = build(ctx, [])
    press(ui, "enter")
    assert ui.state.view == READER
    assert ui.state.thread is not None
    modify = [kw for path, kw in service.calls if path == "users.messages.batchModify"]
    assert modify and modify[0]["body"]["removeLabelIds"] == ["UNREAD"]
    # And the row behind it updates without a re-listing.
    assert ui.state.threads[0].is_unread is False


def test_q_returns_from_the_reader_to_the_list(ctx):
    ui = build(ctx, [])
    press(ui, "enter", "q")
    assert ui.state.view == LIST
    assert ui.state.thread is None


def test_quoted_history_is_folded_until_asked_for(ctx):
    ui = build(ctx, [])
    press(ui, "enter")
    assert "quoted line" in screen(ui)
    assert "older text" not in screen(ui)
    press(ui, "Q")
    assert "older text" in screen(ui)


def test_reader_scrolls(ctx):
    ui = build(ctx, [])
    press(ui, "enter", "j", "j")
    assert ui.state.reader_offset == 2
    press(ui, "g")
    assert ui.state.reader_offset == 0


# -- acting -------------------------------------------------------------------


def test_archive_removes_the_row_from_the_inbox(ctx, service):
    ui = build(ctx, [])
    first = ui.state.threads[0].id
    press(ui, "a")
    modify = [kw for path, kw in service.calls if path == "users.threads.modify"]
    assert modify[0]["id"] == first
    assert modify[0]["body"] == {"removeLabelIds": ["INBOX"]}
    assert first not in [t.id for t in ui.state.threads]
    assert len(ui.state.threads) == 2


def test_marked_rows_are_what_actions_apply_to(ctx, service):
    ui = build(ctx, [])
    marked = [ui.state.threads[0].id, ui.state.threads[2].id]
    press(ui, "x")            # marks row 1, moves to row 2
    press(ui, "j", "x")       # marks row 3
    assert ui.state.selected == set(marked)
    press(ui, "a")
    archived = [kw["id"] for path, kw in service.calls if path == "users.threads.modify"]
    assert archived == marked
    assert ui.state.selected == set()


def test_v_clears_the_marks(ctx):
    ui = build(ctx, [])
    press(ui, "x", "x")
    assert ui.state.selected
    press(ui, "v")
    assert ui.state.selected == set()


def test_star_toggles_both_ways(ctx, service):
    ui = build(ctx, [])
    press(ui, "s")
    press(ui, "s")
    bodies = [kw["body"] for path, kw in service.calls if path == "users.threads.modify"]
    assert bodies == [{"addLabelIds": ["STARRED"]}, {"removeLabelIds": ["STARRED"]}]


def test_unread_toggles_back_after_opening(ctx, service):
    ui = build(ctx, [])
    press(ui, "enter")       # marks read
    press(ui, "u")           # and straight back
    bodies = [kw["body"] for path, kw in service.calls if path == "users.threads.modify"]
    assert bodies == [{"addLabelIds": ["UNREAD"]}]


def test_trash_asks_first(ctx, service):
    ui = build(ctx, [])
    press(ui, "d")
    assert ui.state.prompt is not None
    press(ui, "n", "enter")
    assert not [p for p in paths(service) if p == "users.threads.trash"]
    assert "Cancelled" in ui.state.status


def test_trash_proceeds_on_yes(ctx, service):
    ui = build(ctx, [])
    target = ui.state.threads[0].id
    press(ui, "d", "y", "enter")
    trashed = [kw["id"] for path, kw in service.calls if path == "users.threads.trash"]
    assert trashed == [target]
    assert target not in [t.id for t in ui.state.threads]
    assert "30 days" in ui.state.status


def test_labelling_resolves_an_existing_label(ctx, service):
    ui = build(ctx, [])
    press(ui, "L")
    for key in ("f", "i", "n", "a", "n", "c", "e"):
        ui.dispatch(key)
    press(ui, "enter")
    bodies = [kw["body"] for path, kw in service.calls if path == "users.threads.modify"]
    assert bodies == [{"addLabelIds": ["Label_7"]}]


def test_a_leading_minus_removes_the_label(ctx, service):
    ui = build(ctx, [])
    press(ui, "L")
    for key in ("-", "f", "i", "n", "a", "n", "c", "e"):
        ui.dispatch(key)
    press(ui, "enter")
    bodies = [kw["body"] for path, kw in service.calls if path == "users.threads.modify"]
    assert bodies == [{"removeLabelIds": ["Label_7"]}]


def test_an_unknown_label_reports_instead_of_crashing(ctx):
    ui = build(ctx, [])
    press(ui, "L")
    for key in ("n", "o", "p", "e"):
        ui.dispatch(key)
    press(ui, "-")  # not a valid removal target either, once submitted
    press(ui, "enter")
    assert ui.state.status_style == render.THEME["error"]
    assert ui.state.quit is False


# -- searching ----------------------------------------------------------------


def test_search_sends_the_query_straight_to_gmail(ctx, service):
    ui = build(ctx, [])
    press(ui, "/")
    for key in ("f", "r", "o", "m", ":", "b", "o", "b"):
        ui.dispatch(key)
    press(ui, "enter")
    assert ui.state.query == "from:bob"
    queries = [kw.get("q") for path, kw in service.calls if path == "users.threads.list"]
    assert "from:bob" in queries


def test_search_expands_a_configured_alias(ctx, service):
    ctx.config.aliases["boss"] = "from:boss@example.com is:unread"
    ui = build(ctx, [])
    press(ui, "/")
    for key in ("b", "o", "s", "s"):
        ui.dispatch(key)
    press(ui, "enter")
    assert ui.state.query == "from:boss@example.com is:unread"


def test_escape_leaves_a_search_before_it_quits(ctx):
    ui = build(ctx, [])
    press(ui, "/")
    ui.dispatch("x")
    press(ui, "enter")
    assert ui.state.query == "x"
    press(ui, "escape")
    assert ui.state.query is None
    assert ui.state.quit is False
    press(ui, "escape")
    assert ui.state.quit is True


def test_t_switches_between_conversations_and_messages(ctx, service):
    ui = build(ctx, [])
    press(ui, "t")
    assert ui.state.as_messages is True
    assert "users.messages.list" in paths(service)
    assert ctx.cache.get_listing()[0] == "message"


def test_the_fetch_limit_is_editable(ctx, service):
    ui = build(ctx, [])
    press(ui, "n")
    for key in ("1", "2"):
        ui.dispatch(key)
    press(ui, "ctrl-u")  # clear the pre-filled default that came before it
    for key in ("7",):
        ui.dispatch(key)
    press(ui, "enter")
    assert ui.state.limit == 7
    assert 7 in [kw.get("maxResults") for path, kw in service.calls
                 if path == "users.threads.list"]


# -- help ---------------------------------------------------------------------


def test_help_opens_scrolls_and_closes(ctx):
    ui = build(ctx, [])
    press(ui, "?")
    assert ui.state.view == HELP
    press(ui, "j", "j")
    assert ui.state.help_offset == 2
    press(ui, "q")
    assert ui.state.view == LIST
    assert ui.state.help_offset == 0


def test_help_documents_that_delete_is_impossible(ctx):
    ui = build(ctx, [])
    press(ui, "?")
    body = screen(ui)
    for _ in range(40):
        press(ui, "j")
    body += screen(ui)
    assert "never delete mail" in body


# -- rendering ----------------------------------------------------------------


def test_a_frame_is_exactly_as_tall_as_the_terminal(ctx):
    ui = build(ctx, [])
    for height in (8, 14, 24, 40):
        console = Console(file=io.StringIO(), width=90, height=height, no_color=True)
        console.print(render.frame(ui.state, 90, height))
        assert len(console.file.getvalue().rstrip("\n").split("\n")) == height


def test_the_listing_shows_index_sender_and_subject(ctx):
    ui = build(ctx, [])
    body = screen(ui)
    assert "Subject 1" in body and "Subject 3" in body
    assert "Dana Whitfield" in body
    assert " 1" in body and " 3" in body


def test_a_narrow_terminal_drops_the_sidebar_rather_than_wrapping(ctx):
    ui = build(ctx, [])
    console = Console(file=io.StringIO(), width=44, height=12, no_color=True)
    console.print(render.frame(ui.state, 44, 12))
    body = console.file.getvalue()
    lines = body.rstrip("\n").split("\n")
    assert len(lines) == 12
    assert all(len(line) <= 44 for line in lines)
    assert "MAILBOXES" not in body
    assert "Subject 1" in body


def test_an_empty_mailbox_says_so(ctx, service):
    service.handlers["users.threads.list"] = {"threads": []}
    ui = build(ctx, [])
    assert "Nothing here" in screen(ui)


# -- the command ---------------------------------------------------------------

runner = CliRunner()


def test_ui_refuses_json_mode(isolated_dirs, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    result = runner.invoke(cli_app, ["--json", "ui"])
    assert result.exit_code == 2


def test_ui_refuses_to_run_without_a_terminal(isolated_dirs):
    # CliRunner's streams are not ttys, which is exactly the piped case.
    result = runner.invoke(cli_app, ["ui"])
    assert result.exit_code == 2
    assert "terminal" in result.output


def test_ui_is_listed_in_help():
    result = runner.invoke(cli_app, ["--help"])
    assert "ui" in result.output
    assert result.exit_code == 0


def test_tab_does_not_focus_a_sidebar_that_is_not_drawn(ctx):
    ui = build(ctx, [])
    ui.console = Console(file=io.StringIO(), width=44, height=14, force_terminal=True)
    press(ui, "tab")
    assert ui.state.focus == "list"
    assert "too narrow" in ui.state.status


# -- mouse --------------------------------------------------------------------


def click(x: int, y: int, button: str = "left") -> Mouse:
    return Mouse(button=button, x=x, y=y, pressed=True)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("\x1b[<0;12;5M", Mouse("left", 11, 4, True)),
        ("\x1b[<0;12;5m", Mouse("left", 11, 4, False)),
        ("\x1b[<2;1;1M", Mouse("right", 0, 0, True)),
        ("\x1b[<64;9;9M", Mouse("wheel-up", 8, 8, True)),
        ("\x1b[<65;9;9M", Mouse("wheel-down", 8, 8, True)),
        ("\x1b[<32;4;4M", Mouse("left", 3, 3, True, motion=True)),
    ],
)
def test_sgr_mouse_reports_decode(raw, expected):
    event, used = parse(raw)
    assert event == expected
    assert used == len(raw)


def test_a_partial_sequence_asks_for_more_rather_than_guessing():
    assert parse("\x1b[<0;12") == (None, 0)
    assert parse("\x1b[") == (None, 0)
    assert parse("\x1b") == (None, 0)


def test_several_events_in_one_chunk_are_taken_one_at_a_time():
    """A wheel spin arrives as one burst; none of it may be dropped."""
    buffer = "\x1b[<64;5;5M\x1b[<64;5;5M\x1b[<64;5;5Mj"
    seen = []
    while buffer:
        event, used = parse(buffer)
        assert used, f"stalled on {buffer!r}"
        seen.append(event)
        buffer = buffer[used:]
    assert seen == [Mouse("wheel-up", 4, 4, True)] * 3 + ["j"]


def test_a_drag_is_not_a_click():
    assert Mouse("left", 1, 1, True, motion=True).is_click is False
    assert Mouse("left", 1, 1, False).is_click is False
    assert Mouse("left", 1, 1, True).is_click is True


def test_clicking_a_row_selects_it_and_double_clicking_opens_it(ctx):
    ui = build(ctx, [])
    press(ui, click(30, 4))  # third listed row: header row is at y=1
    assert ui.state.cursor == 2
    assert ui.state.view == LIST
    press(ui, click(30, 4))
    assert ui.state.view == READER


def test_two_slow_clicks_are_not_a_double_click(ctx, monkeypatch):
    ui = build(ctx, [])
    clock = iter([0.0, 10.0])
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    press(ui, click(30, 2), click(30, 2))
    assert ui.state.view == LIST


def test_right_click_marks_a_row(ctx):
    ui = build(ctx, [])
    press(ui, click(30, 3, "right"))
    assert ui.state.selected == {ui.state.threads[1].id}


def test_clicking_the_sidebar_switches_mailbox(ctx, service):
    ui = build(ctx, [])
    press(ui, click(4, 4))  # heading at y=1, Inbox y=2, Unread y=3, Starred y=4
    assert ui.state.mailbox.title == "Starred"
    assert ui.state.focus == "list"


def test_the_key_bar_along_the_bottom_is_clickable(ctx, service):
    ui = build(ctx, [])
    width, height = ui.console.size
    spans = render.key_hint_spans(ui.state, width)
    archive = next(start for start, _, key in spans if key == "a")
    press(ui, click(archive, height - 1))
    assert [kw["body"] for path, kw in service.calls if path == "users.threads.modify"] \
        == [{"removeLabelIds": ["INBOX"]}]


def test_the_wheel_scrolls_the_list_and_the_reader(ctx):
    ui = build(ctx, [])
    press(ui, Mouse("wheel-down", 30, 5, True))
    assert ui.state.cursor == 2  # clamped at the last of three rows
    press(ui, Mouse("wheel-up", 30, 5, True))
    assert ui.state.cursor == 0

    press(ui, "enter")
    press(ui, Mouse("wheel-down", 30, 5, True))
    assert ui.state.reader_offset == 3
    press(ui, Mouse("wheel-up", 30, 5, True))
    assert ui.state.reader_offset == 0


def test_a_release_event_does_nothing(ctx):
    ui = build(ctx, [])
    press(ui, Mouse("left", 30, 4, pressed=False))
    assert ui.state.cursor == 0


def test_dragging_does_not_open_anything(ctx):
    ui = build(ctx, [])
    press(ui, click(30, 2), Mouse("left", 30, 2, True, motion=True))
    assert ui.state.view == LIST


def test_m_toggles_mouse_reporting(ctx):
    ui = build(ctx, [])
    press(ui, "M")
    assert "Mouse" in ui.state.status


def test_hit_test_matches_what_frame_draws(ctx):
    """Clicks and pixels must agree about where the panes start."""
    ui = build(ctx, [])
    width, height = 100, 26
    assert render.hit_test(ui.state, width, height, 5, 0).region == "header"
    assert render.hit_test(ui.state, width, height, 5, 2).region == "sidebar"
    assert render.hit_test(ui.state, width, height, 40, 2) == render.Hit("list", 0)
    assert render.hit_test(ui.state, width, height, 5, height - 2).region == "status"
    assert render.hit_test(ui.state, width, height, 3, height - 1).region == "footer"


def test_a_narrow_terminal_has_no_sidebar_to_click(ctx):
    ui = build(ctx, [])
    assert render.hit_test(ui.state, 44, 20, 3, 3).region == "list"


# -- images -------------------------------------------------------------------


@pytest.mark.parametrize(
    "env, expected",
    [
        ({"TERM": "xterm-ghostty", "TERM_PROGRAM": "ghostty"}, graphics.KITTY),
        ({"TERM": "xterm-kitty"}, graphics.KITTY),
        ({"KITTY_WINDOW_ID": "1"}, graphics.KITTY),
        ({"TERM_PROGRAM": "WezTerm"}, graphics.KITTY),
        ({"TERM_PROGRAM": "iTerm.app"}, graphics.ITERM2),
        ({"TERM": "xterm-256color"}, graphics.NONE),
        ({"TERM": "xterm-ghostty", "GMCLI_IMAGE_PROTOCOL": "none"}, graphics.NONE),
        ({"GMCLI_IMAGE_PROTOCOL": "blocks"}, graphics.BLOCKS),
    ],
)
def test_image_protocol_detection(env, expected):
    assert graphics.detect_protocol(env) == expected


def test_a_truecolor_terminal_falls_back_to_blocks_only_with_pillow():
    env = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}
    expected = graphics.BLOCKS if graphics.have_pillow() else graphics.NONE
    assert graphics.detect_protocol(env) == expected


def png_bytes(size=(8, 6), color=(200, 30, 30)) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def noisy_png(size=(200, 150)) -> bytes:
    """A PNG that will not compress away, so chunking actually happens."""
    import io
    import random

    from PIL import Image

    rng = random.Random(0)
    img = Image.new("RGB", size)
    img.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                 for _ in range(size[0] * size[1])])
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def test_kitty_payload_is_chunked_within_the_protocol_limit():
    data = noisy_png()
    assert len(data) > 4096, "test needs an image big enough to span chunks"
    out = graphics.render(data, "image/png", cols=40, rows=20,
                          protocol=graphics.KITTY).payload
    escapes = out.split("\x1b_G")[1:]
    assert len(escapes) > 1, "a large image should span several chunks"
    for piece in escapes:
        control, _, payload = piece.partition(";")
        assert len(payload.rstrip("\x1b\\")) <= 4096
    # Only the first carries the placement, only the last says "done".
    assert escapes[0].startswith("a=T,f=100,t=d,c=40,r=20,m=1")
    assert escapes[-1].startswith("m=0")


def test_png_needs_no_decoding_at_all(monkeypatch):
    """The point of `f=100`: a PNG attachment shows with Pillow absent."""
    monkeypatch.setattr(graphics, "have_pillow", lambda: False)
    assert graphics.render(png_bytes(), "image/png", cols=10, rows=5,
                           protocol=graphics.KITTY) is not None


def test_other_formats_need_pillow_and_say_so(monkeypatch):
    monkeypatch.setattr(graphics, "have_pillow", lambda: False)
    assert graphics.render(b"\xff\xd8\xff", "image/jpeg", cols=10, rows=5,
                           protocol=graphics.KITTY) is None
    assert "gmcli[images]" in graphics.unavailable_reason("image/jpeg", graphics.KITTY)


def test_a_jpeg_is_re_encoded_when_pillow_is_available():
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (20, 10), (10, 120, 200)).save(buffer, format="JPEG")
    out = graphics.render(buffer.getvalue(), "image/jpeg", cols=10, rows=5,
                          protocol=graphics.KITTY)
    assert out is not None and out.payload.startswith("\x1b_G")


def test_iterm2_uses_its_own_escape():
    out = graphics.render(png_bytes(), "image/png", cols=10, rows=5,
                          protocol=graphics.ITERM2).payload
    assert out.startswith("\x1b]1337;File=inline=1;")
    assert "preserveAspectRatio=1" in out


def test_the_half_block_fallback_paints_truecolor_cells():
    out = graphics.render(png_bytes((8, 8)), "image/png", cols=8, rows=4,
                          protocol=graphics.BLOCKS)
    assert out is not None
    assert "▀" in out.payload
    assert "\x1b[38;2;200;30;30m" in out.payload
    assert out.note == "half-block preview"


def test_no_protocol_renders_nothing():
    assert graphics.render(png_bytes(), "image/png", cols=10, rows=5,
                           protocol=graphics.NONE) is None


@pytest.fixture
def service_with_image(service):
    service.handlers["users.threads.get"] = lambda kw: {
        "id": kw["id"], "snippet": "s",
        "messages": [make_message(
            f"m{kw['id']}", thread_id=kw["id"], subject="Photos",
            attachments=[("chart.png", "image/png", 120),
                         ("notes.txt", "text/plain", 20)],
        )],
    }
    import base64

    service.handlers["users.messages.attachments.get"] = {
        "data": base64.urlsafe_b64encode(png_bytes()).decode().rstrip("=")
    }
    return service


def test_i_draws_the_image_and_waits_for_a_key(ctx, service_with_image):
    ui = build(ctx, ["space"])  # the keystroke that dismisses the image
    ui.protocol = graphics.KITTY
    press(ui, "enter", "i")
    drawn = ui.console.file.getvalue()
    assert "\x1b_G" in drawn, "no image escape reached the terminal"
    assert "chart.png" in drawn
    assert "any key to go back" in drawn
    # And the placement is torn down again, since text does not erase it.
    assert "\x1b_Ga=d,d=A\x1b\\" in drawn
    assert ui.state.view == READER


def test_only_image_attachments_count(ctx, service):
    ui = build(ctx, [])
    ui.protocol = graphics.KITTY
    press(ui, "enter", "i")
    assert "No images here" in ui.state.status


def test_no_images_flag_explains_itself(ctx, service_with_image):
    ui = build(ctx, [])
    ui.protocol = graphics.NONE
    press(ui, "enter", "i")
    assert "no image protocol" in ui.state.status


def test_several_images_ask_which_one(ctx, service):
    service.handlers["users.threads.get"] = lambda kw: {
        "id": kw["id"], "snippet": "s",
        "messages": [make_message(
            f"m{kw['id']}", thread_id=kw["id"],
            attachments=[("a.png", "image/png", 10), ("b.png", "image/png", 10)],
        )],
    }
    ui = build(ctx, [])
    ui.protocol = graphics.KITTY
    press(ui, "enter", "i")
    assert ui.state.prompt is not None
    assert "which image" in ui.state.prompt.label


def test_image_attachments_are_flagged_in_the_reader(ctx, service_with_image):
    ui = build(ctx, [])
    press(ui, "enter")
    body = screen(ui)
    assert "chart.png" in body and "i to view" in body
    assert "notes.txt" in body


# -- hyperlinks ---------------------------------------------------------------


def test_urls_in_a_body_become_clickable_links(ctx, service):
    service.handlers["users.threads.get"] = lambda kw: {
        "id": kw["id"], "snippet": "s",
        "messages": [make_message(
            f"m{kw['id']}", thread_id=kw["id"],
            body="Build failed: https://ci.example.com/runs/42 — take a look.",
        )],
    }
    ui = build(ctx, [])
    press(ui, "enter")
    lines, _ = render.reader_lines(ui.state, 80)
    links = [
        str(span.style)
        for line in lines
        for span in line.spans
        if str(span.style).startswith("link ")
    ]
    assert "link https://ci.example.com/runs/42" in links


def test_trailing_punctuation_is_not_swallowed_into_the_url():
    from gmcli.ui.render import _linkify
    from rich.text import Text

    text = _linkify(Text("see https://example.com/a, and https://example.com/b."))
    links = {str(span.style) for span in text.spans}
    assert links == {"link https://example.com/a", "link https://example.com/b"}


# -- the full-width invariant -------------------------------------------------
#
# A line shorter than the terminal does not erase what was to the right of it,
# so whatever was on screen before shows through the gaps. This is the guard
# on that: every line of every frame, in every view, covers the whole width.


def frame_lines(ui: MailApp, width: int, height: int) -> list[str]:
    console = Console(file=io.StringIO(), width=width, height=height, no_color=True)
    console.print(render.frame(ui.state, width, height))
    return console.file.getvalue().rstrip("\n").split("\n")


def assert_covers_the_screen(ui: MailApp, width: int, height: int) -> None:
    lines = frame_lines(ui, width, height)
    assert len(lines) == height, f"frame is {len(lines)} lines, wanted {height}"
    short = [(n, len(line)) for n, line in enumerate(lines) if len(line) != width]
    assert not short, f"lines not exactly {width} cells: {short}"


@pytest.mark.parametrize("size", [(100, 30), (80, 24), (140, 50), (44, 12)])
def test_the_list_view_paints_every_cell(ctx, size):
    ui = build(ctx, [])
    assert_covers_the_screen(ui, *size)


@pytest.mark.parametrize("size", [(100, 30), (80, 24), (44, 12)])
def test_the_reader_paints_every_cell(ctx, size):
    """The view where it went wrong: a short message left most rows untouched."""
    ui = build(ctx, [])
    press(ui, "enter")
    assert_covers_the_screen(ui, *size)


def test_a_short_message_still_paints_every_cell(ctx, service):
    service.handlers["users.threads.get"] = lambda kw: {
        "id": kw["id"], "snippet": "s",
        "messages": [make_message(f"m{kw['id']}", thread_id=kw["id"],
                                  subject="Hii", body="Hii testing")],
    }
    ui = build(ctx, [])
    press(ui, "enter")
    assert_covers_the_screen(ui, 100, 30)


def test_the_help_and_prompt_views_paint_every_cell(ctx):
    ui = build(ctx, [])
    press(ui, "?")
    assert_covers_the_screen(ui, 100, 30)
    press(ui, "q", "/")
    assert ui.state.prompt is not None
    assert_covers_the_screen(ui, 100, 30)


def test_an_empty_mailbox_paints_every_cell(ctx, service):
    service.handlers["users.threads.list"] = {"threads": []}
    ui = build(ctx, [])
    assert_covers_the_screen(ui, 100, 30)


def test_exact_keeps_styles_and_links_while_padding():
    from rich.text import Text

    line = Text("see ")
    line.append("https://example.com", style="link https://example.com")
    padded = render.exact(line, 60)
    assert len(padded.plain) == 60
    assert "link https://example.com" in {str(span.style) for span in padded.spans}


def test_exact_crops_a_line_that_is_too_long():
    from rich.text import Text

    assert len(render.exact(Text("x" * 200), 40).plain) == 40


# -- refresh ------------------------------------------------------------------


def test_the_header_carries_a_clickable_refresh_control(ctx):
    ui = build(ctx, [])
    assert "⟳ refresh" in render.header(ui.state, 100).plain
    start, end = render.refresh_span(100)
    assert render.hit_test(ui.state, 100, 30, start + 1, 0).key == "ctrl-r"
    # Elsewhere on the header bar, nothing happens.
    assert render.hit_test(ui.state, 100, 30, 2, 0).key is None


def test_clicking_refresh_refetches_the_mailbox(ctx, service):
    ui = build(ctx, [])
    before = sum(1 for path, _ in service.calls if path == "users.threads.list")
    start, _ = render.refresh_span(ui.console.size[0])
    press(ui, click(start + 2, 0))
    after = sum(1 for path, _ in service.calls if path == "users.threads.list")
    assert after == before + 1
    assert "Refreshed" in ui.state.status


@pytest.mark.parametrize("key", ["ctrl-r", "."])
def test_refresh_keys(ctx, service, key):
    ui = build(ctx, [])
    before = sum(1 for path, _ in service.calls if path == "users.threads.list")
    press(ui, key)
    after = sum(1 for path, _ in service.calls if path == "users.threads.list")
    assert after == before + 1


def test_refresh_works_from_the_reader_too(ctx, service):
    ui = build(ctx, [])
    press(ui, "enter")
    before = sum(1 for path, _ in service.calls if path == "users.threads.list")
    press(ui, ".")
    assert sum(1 for path, _ in service.calls if path == "users.threads.list") == before + 1


def test_refresh_is_in_the_key_bar_and_clickable_there(ctx, service):
    ui = build(ctx, [])
    width, height = ui.console.size
    spans = render.key_hint_spans(ui.state, width)
    assert any(key == "ctrl-r" for _, _, key in spans)
    start = next(start for start, _, key in spans if key == "ctrl-r")
    before = sum(1 for path, _ in service.calls if path == "users.threads.list")
    press(ui, click(start, height - 1))
    assert sum(1 for path, _ in service.calls if path == "users.threads.list") == before + 1


def test_the_header_shows_when_the_view_was_last_loaded(ctx):
    ui = build(ctx, [])
    assert ui.state.last_refresh is not None
    assert ui.state.last_refresh.strftime("%H:%M") in render.header(ui.state, 100).plain


def test_a_failing_refresh_reports_instead_of_claiming_success(ctx, service):
    ui = build(ctx, [])

    def boom(kwargs):
        raise RuntimeError("network is down")

    service.handlers["users.threads.list"] = boom
    press(ui, ".")
    assert ui.state.status_style == render.THEME["error"]
    assert "Refreshed" not in ui.state.status
    assert ui.state.quit is False


# -- large images -------------------------------------------------------------


def kitty_payload(rendered) -> bytes:
    """Reassemble the base64 spread across a kitty escape's chunks."""
    import base64

    joined = "".join(
        piece.partition(";")[2].rstrip("\x1b\\")
        for piece in rendered.payload.split("\x1b_G")[1:]
    )
    return base64.b64decode(joined + "=" * (-len(joined) % 4))


def test_a_small_png_is_passed_through_untouched():
    data = png_bytes()
    out = graphics.render(data, "image/png", cols=20, rows=10, protocol=graphics.KITTY)
    assert kitty_payload(out) == data


def test_a_large_png_is_shrunk_before_it_is_transmitted():
    """A phone photo is megabytes; the terminal shows it in a few hundred cells."""
    data = noisy_png((1200, 900))
    assert len(data) > graphics.DOWNSCALE_ABOVE_BYTES
    out = graphics.render(data, "image/png", cols=98, rows=24, protocol=graphics.KITTY)
    import base64

    assert len(out.payload) < len(base64.b64encode(data)) / 2
    assert out.payload.count("\x1b_G") > 1


def test_a_large_png_still_shows_without_pillow(monkeypatch):
    """Slower, but a missing decoder must not mean a missing image."""
    monkeypatch.setattr(graphics, "have_pillow", lambda: False)
    data = noisy_png((1200, 900))
    out = graphics.render(data, "image/png", cols=98, rows=24, protocol=graphics.KITTY)
    assert out is not None
    assert kitty_payload(out) == data


def test_a_png_that_will_not_decode_is_still_sent_as_is():
    broken = b"\x89PNG\r\n\x1a\n" + b"\x00" * (graphics.DOWNSCALE_ABOVE_BYTES + 1)
    out = graphics.render(broken, "image/png", cols=20, rows=10,
                          protocol=graphics.KITTY)
    assert out is not None


# -- inline images are attachments --------------------------------------------


@pytest.fixture
def service_with_inline_image(service):
    """The shape Gmail's own composer produces: inline + Content-ID."""
    import base64

    def thread(kw):
        payload = make_message(f"m{kw['id']}", thread_id=kw["id"], subject="Hii",
                               body="Hii testing", html="<p>Hii testing</p>")
        payload["payload"]["parts"].append({
            "mimeType": "image/png",
            "filename": "gmail_images20260828_035406.png",
            "body": {"attachmentId": "a1", "size": 4_120_515},
            "headers": [
                {"name": "Content-Disposition",
                 "value": 'inline; filename="gmail_images20260828_035406.png"'},
                {"name": "Content-ID", "value": "<ii_1a04552d0ced5762e191>"},
            ],
        })
        return {"id": kw["id"], "snippet": "s", "messages": [payload]}

    service.handlers["users.threads.get"] = thread
    service.handlers["users.messages.attachments.get"] = {
        "data": base64.urlsafe_b64encode(png_bytes()).decode().rstrip("=")
    }
    return service


def test_an_image_attached_in_gmail_shows_up_in_the_reader(ctx, service_with_inline_image):
    ui = build(ctx, [])
    press(ui, "enter")
    body = screen(ui)
    assert "gmail_images20260828_035406.png" in body
    assert "i to view" in body


def test_i_can_view_an_image_attached_in_gmail(ctx, service_with_inline_image):
    ui = build(ctx, ["space"])
    ui.protocol = graphics.KITTY
    press(ui, "enter", "i")
    assert "\x1b_G" in ui.console.file.getvalue()


def test_such_a_message_can_have_its_attachments_downloaded(ctx, service_with_inline_image, tmp_path):
    ui = build(ctx, [])
    press(ui, "enter", "w")
    press(ui, "ctrl-u")  # clear the pre-filled ~/Downloads default
    for char in str(tmp_path):
        ui.dispatch(char)
    press(ui, "enter")
    assert (tmp_path / "gmail_images20260828_035406.png").exists()
    assert "Saved 1" in ui.state.status

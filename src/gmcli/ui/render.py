"""Turning :class:`UIState` into something rich can print.

Every function here is pure: state in, renderable out, no API calls and no
mutation. That is what lets a test render a screen and assert on the text.

Panes are built as explicit lists of one-line ``Text`` objects rather than as
rich ``Table``/``Layout`` objects. It is more code, but it means a pane is
always exactly the height it was asked for — which is what keeps the two
columns aligned and the footer pinned to the bottom row on every redraw.

``output.py`` is untouched by any of this. It owns what the *commands* print,
and its JSON contract must not acquire a second caller with different needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.cells import cell_len, set_cell_size
from rich.console import Console, Group, RenderableType
from rich.text import Text

from ..models import Message, Thread, split_quoted
from ..output import format_date, format_size, html_to_text
from .graphics import is_image
from .state import HELP, LIST, READER, STANDARD_MAILBOXES, UIState

SIDEBAR_WIDTH = 22
# Unread counts sit in their own right-aligned column in the sidebar.
COUNT_WIDTH = 4
# Below this the sidebar costs more than it is worth, so the list takes the
# whole width and mailboxes are reached with Tab-free keys instead.
SIDEBAR_MIN_WIDTH = 70

# One place for the colours, so the whole UI shifts together.
THEME = {
    "bar": "bold white on dark_blue",
    "button": "bold black on cyan",
    "cursor": "bold white on grey30",
    "cursor_idle": "on grey19",
    "unread": "bold",
    "read": "",
    "meta": "dim",
    "accent": "cyan",
    "star": "yellow",
    "mark": "green",
    "rule": "dim",
    "error": "bold red",
    "ok": "green",
    "warn": "yellow",
}

REFRESH = " ⟳ refresh "
STAR = "★"
CLIP = "\U0001f4ce"
PICTURE = "\U0001f5bc"
CURSOR = "▌"


def _fit(text: str, width: int) -> str:
    """Truncate or pad ``text`` to exactly ``width`` terminal cells."""
    if width <= 0:
        return ""
    text = " ".join((text or "").split())
    if cell_len(text) > width:
        return set_cell_size(text, max(width - 1, 0)) + "…"
    return set_cell_size(text, width)


def _rfit(text: str, width: int) -> str:
    """Right-align within exactly ``width`` cells."""
    if width <= 0:
        return ""
    text = (text or "").strip()
    if cell_len(text) > width:
        return set_cell_size(text, width)
    return " " * (width - cell_len(text)) + text


def _blank(width: int) -> Text:
    return Text(" " * max(width, 0))


def exact(line: Text, width: int) -> Text:
    """Force a line to exactly ``width`` cells, keeping its styles and links.

    Every line the frame emits goes through this. A line shorter than the
    terminal does not erase what was to the right of it — whatever that is,
    an earlier frame or another program's output — so it bleeds through the
    UI. Padding here rather than in each pane means no pane can forget.
    """
    line.truncate(width, overflow="crop", pad=True)
    return line


def _pad(lines: list[Text], height: int, width: int) -> list[Text]:
    """Force a pane to exactly ``height`` lines."""
    out = lines[:height]
    out.extend(_blank(width) for _ in range(height - len(out)))
    return out


def _window(count: int, cursor: int, height: int) -> int:
    """First visible index that keeps ``cursor`` on screen, with a margin."""
    if count <= height:
        return 0
    margin = 2 if height > 6 else 0
    top = max(0, min(cursor - height // 2, count - height))
    if cursor - margin < top:
        top = max(0, cursor - margin)
    elif cursor + margin >= top + height:
        top = min(count - height, cursor + margin - height + 1)
    return max(0, top)


# -- header, status, key hints -----------------------------------------------


def refresh_span(width: int) -> tuple[int, int]:
    """Where the header's refresh control sits, for both drawing and clicking."""
    start = max(width - cell_len(REFRESH), 0)
    return start, width


def header(state: UIState, width: int) -> Text:
    left = f" gmail · {state.account}"
    right = state.mailbox.title
    if state.query:
        right = f"search: {state.query}"
    if state.as_messages:
        right += "  [messages]"
    unread = state.unread_counts.get(state.mailbox.counter or "", 0)
    if unread:
        right += f"  {unread} unread"
    if state.last_refresh:
        right += f"  ·  {state.last_refresh.strftime('%H:%M')}"

    start, _ = refresh_span(width)
    gap = start - cell_len(left) - cell_len(right) - 1
    if gap < 1:
        line = Text(_fit(left, start), style=THEME["bar"])
    else:
        line = Text(f"{left}{' ' * gap}{right} ", style=THEME["bar"])
        line.truncate(start, overflow="crop", pad=True)
    line.append(REFRESH, style=THEME["button"])
    return line


def status_line(state: UIState, width: int) -> Text:
    if state.prompt is not None:
        editor = state.prompt
        line = Text(" ")
        line.append(editor.label, style=THEME["accent"])
        line.append(editor.text)
        # A block where the caret is, since the real cursor lives elsewhere.
        caret = editor.text[editor.cursor] if editor.cursor < len(editor.text) else " "
        line.append(caret, style="reverse")
        return line
    return Text(_fit(f" {state.status}", width), style=state.status_style)


def _keys_for(state: UIState) -> list[tuple[str, str, str]]:
    """Footer hints as (label, meaning, key to send when clicked)."""
    if state.view == HELP:
        return [("j/k", "scroll", "j"), ("any other key", "back", "q")]
    if state.view == READER:
        return [
            ("j/k", "scroll", "j"), ("n/p", "message", "n"), ("r", "reply", "r"),
            ("f", "forward", "f"), ("a", "archive", "a"), ("s", "star", "s"),
            ("i", "image", "i"), ("w", "save", "w"), ("Q", "quoted", "Q"),
            ("⟳", "refresh", "ctrl-r"), ("q", "back", "q"), ("?", "help", "?"),
        ]
    return [
        ("j/k", "move", "j"), ("↵", "open", "enter"), ("x", "mark", "x"),
        ("a", "archive", "a"), ("s", "star", "s"), ("u", "unread", "u"),
        ("L", "label", "L"), ("c", "compose", "c"), ("/", "search", "/"),
        ("⟳", "refresh", "ctrl-r"), ("?", "help", "?"), ("q", "quit", "q"),
    ]


def key_hint_spans(state: UIState, width: int) -> list[tuple[int, int, str]]:
    """``(start, end, key)`` for each hint, so the footer can be clicked."""
    spans: list[tuple[int, int, str]] = []
    column = 1
    for label, meaning, key in _keys_for(state):
        entry = f"{label} {meaning}  "
        if column + cell_len(entry) > width:
            break
        spans.append((column, column + cell_len(f"{label} {meaning}"), key))
        column += cell_len(entry)
    return spans


def key_hints(state: UIState, width: int) -> Text:
    line = Text(" ")
    for label, meaning, _ in _keys_for(state):
        if cell_len(line.plain) + cell_len(label) + cell_len(meaning) + 3 > width:
            break
        line.append(label, style=THEME["accent"])
        line.append(f" {meaning}  ", style=THEME["meta"])
    if cell_len(line.plain) < width:
        line.append(" " * (width - cell_len(line.plain)))
    return line


# -- sidebar ------------------------------------------------------------------


def sidebar(state: UIState, width: int, height: int) -> list[Text]:
    """The mailbox column: the standard set, a rule, then the user's labels."""
    split = len(STANDARD_MAILBOXES)
    rows: list[tuple[str, object]] = [("title", "MAILBOXES")]
    for index, box in enumerate(state.mailboxes):
        if index == split and split < len(state.mailboxes):
            rows.append(("rule", "LABELS"))
        rows.append(("box", (index, box)))

    focus_row = next(
        (n for n, row in enumerate(rows)
         if row[0] == "box" and row[1][0] == state.mailbox_index),
        0,
    )
    top = _window(len(rows), focus_row, height)

    lines: list[Text] = []
    active = state.focus == "sidebar"
    for kind, payload in rows[top : top + height]:
        if kind == "title":
            lines.append(Text(_fit(f" {payload}", width), style=THEME["meta"]))
            continue
        if kind == "rule":
            label = f" ── {payload} "
            lines.append(
                Text(label + "─" * max(width - cell_len(label), 0), style=THEME["rule"])
            )
            continue

        index, box = payload  # type: ignore[misc]
        count = state.unread_counts.get(box.counter or "", 0)
        marker = CURSOR if index == state.mailbox_index else " "
        line = Text(_fit(f"{marker}{box.title}", width - COUNT_WIDTH - 1))
        line.append(_rfit(str(count) if count else "", COUNT_WIDTH),
                    style=THEME["accent"] if count else "")
        line.append(" ")
        if index == state.mailbox_index:
            line.stylize(THEME["cursor"] if active else THEME["cursor_idle"])
        elif count:
            line.stylize("bold")
        lines.append(line)
    return _pad(lines, height, width)


# -- listing ------------------------------------------------------------------


def _row_columns(width: int) -> tuple[int, int, int, int, int]:
    """Widths for mark, index, flags, sender, date — subject takes the rest."""
    date = 10 if width >= 70 else 6
    sender = 22 if width >= 90 else (16 if width >= 70 else 12)
    return 1, 3, 3, sender, date


def listing(state: UIState, width: int, height: int) -> list[Text]:
    mark_w, idx_w, flag_w, from_w, date_w = _row_columns(width)
    subject_w = max(width - (mark_w + idx_w + flag_w + from_w + date_w + 4), 8)

    head = Text(
        f"{' ' * mark_w}{_rfit('#', idx_w)} {' ' * flag_w} "
        f"{_fit('From', from_w)} {_fit('Subject', subject_w)} {_rfit('Date', date_w)}",
        style=THEME["meta"],
    )
    rows = state.rows
    if not rows:
        return _pad(
            [head, Text(""), Text("  Nothing here.", style=THEME["meta"])],
            height,
            width,
        )

    body_height = height - 1
    top = _window(len(rows), state.cursor, body_height)
    lines = [head]
    active = state.focus == "list"

    for offset, row in enumerate(rows[top : top + body_height]):
        index = top + offset
        unread = row.is_unread
        marked = row.id in state.selected
        flags = (STAR if row.is_starred else " ") + (CLIP if row.has_attachments else " ")

        if isinstance(row, Thread):
            who = ", ".join(row.participants) or "(unknown)"
            if row.message_count > 1:
                who = f"{who} ({row.message_count})"
        else:
            who = row.sender_name or "(unknown)"

        line = Text()
        line.append("✓" if marked else " ", style=THEME["mark"])
        line.append(_rfit(str(index + 1), idx_w), style=THEME["meta"])
        line.append(" ")
        line.append(_fit(flags, flag_w), style=THEME["star"] if row.is_starred else THEME["meta"])
        line.append(" ")
        line.append(_fit(who, from_w), style=THEME["unread"] if unread else "")
        line.append(" ")
        line.append(_fit(row.subject, subject_w), style=THEME["unread"] if unread else "")
        line.append(" ")
        line.append(_rfit(format_date(row.date), date_w), style=THEME["meta"])

        if index == state.cursor:
            line.stylize(THEME["cursor"] if active else THEME["cursor_idle"])
        lines.append(line)

    return _pad(lines, height, width)


# -- reader -------------------------------------------------------------------


def reader_lines(state: UIState, width: int) -> tuple[list[Text], list[int]]:
    """Flatten the open conversation into wrapped lines, plus message offsets.

    The offsets are what ``n``/``p`` jump between, so navigating a twelve-message
    thread does not mean holding a scroll key.
    """
    thread = state.thread
    if thread is None:
        return [], []

    lines: list[Text] = []
    starts: list[int] = []

    for position, msg in enumerate(thread.messages):
        if position:
            lines.append(Text(""))
            lines.append(Text("─" * width, style=THEME["rule"]))
            lines.append(Text(""))
        starts.append(len(lines))
        lines.extend(_message_block(msg, width, show_quoted=state.show_quoted,
                                    position=position, total=len(thread.messages)))
    return lines, starts


# Deliberately conservative: trailing punctuation is far more often sentence
# punctuation than part of the URL.
_URL_RE = re.compile(r"""https?://[^\s<>"'`\]\)]+[^\s<>"'`\]\).,;:!?]""")


def _linkify(text: Text) -> Text:
    """Turn URLs into OSC 8 hyperlinks the terminal can open on click.

    Styling before wrapping is what makes this survive a URL that gets folded
    across two lines — ``Text.wrap`` carries the style onto both halves, so
    both remain clickable and both point at the whole address.
    """
    for match in _URL_RE.finditer(text.plain):
        text.stylize(f"link {match.group(0)}", match.start(), match.end())
    return text


def _wrap(text: str, width: int, style: str = "") -> list[Text]:
    """Wrap a block of text to ``width``, preserving blank lines."""
    out: list[Text] = []
    for raw in (text or "").splitlines() or [""]:
        chunk = _linkify(Text(raw.rstrip(), style=style))
        if not raw.strip():
            out.append(Text(""))
            continue
        out.extend(chunk.wrap(_MEASURE, width, overflow="fold"))
    return out


# ``Text.wrap`` needs a console only to read wrapping options off it; it never
# prints. One throwaway instance serves every call.
_MEASURE = Console(width=200, no_color=True)


def _message_block(
    msg: Message, width: int, *, show_quoted: bool, position: int, total: int
) -> list[Text]:
    lines: list[Text] = []
    counter = f"[{position + 1}/{total}] " if total > 1 else ""
    lines.extend(_wrap(f"{counter}{msg.subject}", width, "bold"))
    for label, value in (
        ("From", msg.sender), ("To", msg.to), ("Cc", msg.cc),
    ):
        if value:
            head = Text(f"{label}: ", style=THEME["meta"])
            head.append(_fit(value, max(width - len(label) - 2, 8)))
            lines.append(head)
    if msg.date:
        lines.append(
            Text(f"Date: {msg.date.astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
                 style=THEME["meta"])
        )
    if msg.is_unread or msg.is_starred:
        tags = " ".join(t for t in ("unread" if msg.is_unread else "",
                                    "starred" if msg.is_starred else "") if t)
        lines.append(Text(tags, style=THEME["warn"]))
    lines.append(Text(""))

    body = msg.body_text or (html_to_text(msg.body_html) if msg.body_html else None)
    if body is None:
        lines.append(Text("(no readable text body)", style=THEME["meta"]))
    else:
        visible, quoted = split_quoted(body)
        lines.extend(_wrap(visible, width))
        if quoted:
            if show_quoted:
                lines.extend(_wrap(quoted, width, THEME["meta"]))
            else:
                count = len(quoted.splitlines())
                lines.append(Text(""))
                lines.append(
                    Text(f"… {count} quoted line{'s' if count != 1 else ''} hidden "
                         "(Q to expand)", style=THEME["meta"])
                )

    if msg.attachments:
        lines.append(Text(""))
        for att in msg.attachments:
            glyph = PICTURE if is_image(att.mime_type, att.filename) else CLIP
            entry = Text(f"{glyph} [{att.index}] {att.filename}", style=THEME["accent"])
            entry.append(f"  {att.mime_type}, {format_size(att.size)}", style=THEME["meta"])
            if glyph == PICTURE:
                entry.append("  i to view", style=THEME["meta"])
            lines.append(entry)
    return lines


def reader(state: UIState, width: int, height: int) -> list[Text]:
    lines, _ = reader_lines(state, width)
    top = max(0, min(state.reader_offset, max(0, len(lines) - height)))
    return _pad(list(lines[top : top + height]), height, width)


# -- help ---------------------------------------------------------------------

HELP_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Moving", (
        ("j / k  ↓ ↑", "next / previous"),
        ("g / G", "first / last"),
        ("Ctrl-D / Ctrl-U", "half page down / up"),
        ("Tab", "switch between the sidebar and the list"),
        ("Enter / l", "open the conversation under the cursor"),
        ("q / Esc", "back, or quit from the list"),
    )),
    ("Finding", (
        ("/", "search with Gmail's own query syntax"),
        ("t", "toggle conversations ↔ individual messages"),
        ("n", "limit: how many to fetch"),
        ("Ctrl-R  or  .", "fetch the latest mail — or click ⟳ in the top right"),
    )),
    ("Mouse", (
        ("click", "select a row, or switch mailbox in the sidebar"),
        ("double-click", "open the conversation"),
        ("right-click", "mark a row"),
        ("wheel", "scroll the list, the reader, or the key reference"),
        ("click the bar", "the key hints along the bottom are buttons"),
        ("M", "turn mouse reporting off — restores text selection"),
    )),
    ("Acting", (
        ("x", "mark a row; actions then apply to every marked row"),
        ("v", "clear all marks"),
        ("a / A", "archive / move back to the inbox"),
        ("s", "toggle star"),
        ("u", "toggle read ↔ unread"),
        ("L", "add a label — prefix with '-' to remove one"),
        ("d", "move to Trash (confirmed; recoverable for 30 days)"),
        ("w", "download attachments"),
        ("i", "view an image attachment inline"),
    )),
    ("Writing", (
        ("c", "compose a new message"),
        ("r / R", "reply / reply to all"),
        ("f", "forward"),
        ("", "each opens $EDITOR and confirms before anything is sent"),
    )),
    ("Notes", (
        ("", "images need a terminal that draws them — Ghostty, Kitty,"),
        ("", "WezTerm, iTerm2. PNG works as-is; other formats want"),
        ("", "pip install 'gmcli[images]'. URLs in a body are clickable."),
        ("", "opening a conversation marks it read, as a mail client does;"),
        ("", "press u to put it back."),
        ("", "the rows here are the same #1, #2, #3 the CLI uses, so you can"),
        ("", "quit and run  gmail archive '#2'  on what you were just looking at."),
        ("", "gmcli holds the gmail.modify scope: it can never delete mail."),
    )),
)


def help_lines(width: int) -> list[Text]:
    lines: list[Text] = [Text("")]
    for title, items in HELP_SECTIONS:
        lines.append(Text(f"  {title}", style="bold"))
        for key, description in items:
            row = Text("    ")
            row.append(_fit(key, 18), style=THEME["accent"] if key else "")
            row.append(_fit(description, max(width - 24, 10)), style=THEME["meta"] if not key else "")
            lines.append(row)
        lines.append(Text(""))
    return lines


def help_pane(state: UIState, width: int, height: int) -> list[Text]:
    lines = help_lines(width)
    top = max(0, min(state.help_offset, max(0, len(lines) - height)))
    return _pad(list(lines[top : top + height]), height, width)


# -- the whole screen ---------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    """What sits under a given cell.

    ``region`` is one of ``header``, ``sidebar``, ``list``, ``reader``,
    ``help``, ``status``, ``footer``. ``index`` is the mailbox index, the row
    index, or ``None``; ``key`` is set only for a footer hint, and is the
    keystroke that hint stands for.
    """

    region: str
    index: int | None = None
    key: str | None = None


def sidebar_visible(state: UIState, width: int) -> bool:
    return state.view == LIST and width >= SIDEBAR_MIN_WIDTH


def hit_test(state: UIState, width: int, height: int, x: int, y: int) -> Hit:
    """Which part of the screen a click at cell ``(x, y)`` landed on.

    Deliberately built from the same constants ``frame`` lays out with — the
    two drifting apart would mean clicks quietly landing one row off.
    """
    width = max(width, 20)
    body_height = max(height - 3, 1)
    body_top = 1
    body_bottom = body_top + body_height  # exclusive

    if y < body_top:
        start, end = refresh_span(width)
        return Hit("header", key="ctrl-r" if start <= x < end else None)
    if y >= body_bottom + 1:
        return Hit("footer", key=_hint_at(state, width, x))
    if y >= body_bottom:
        return Hit("status")

    row = y - body_top
    if state.view == HELP:
        return Hit("help")
    if state.view == READER:
        return Hit("reader", index=state.reader_offset + row)

    if sidebar_visible(state, width) and x < SIDEBAR_WIDTH:
        return Hit("sidebar", index=_mailbox_at(state, body_height, row))
    if sidebar_visible(state, width) and x == SIDEBAR_WIDTH:
        return Hit("list")  # the divider itself

    # The list pane's first line is its column header.
    if row == 0:
        return Hit("list")
    pane_height = body_height - 1
    top = _window(len(state.rows), state.cursor, pane_height)
    index = top + row - 1
    return Hit("list", index=index if 0 <= index < len(state.rows) else None)


def _hint_at(state: UIState, width: int, x: int) -> str | None:
    for start, end, key in key_hint_spans(state, width):
        if start <= x < end:
            return key
    return None


def _mailbox_at(state: UIState, height: int, row: int) -> int | None:
    """Reverse the sidebar's title/rule/entry layout back to a mailbox index."""
    split = len(STANDARD_MAILBOXES)
    entries: list[int | None] = [None]  # the MAILBOXES heading
    for index in range(len(state.mailboxes)):
        if index == split and split < len(state.mailboxes):
            entries.append(None)  # the LABELS rule
        entries.append(index)

    focus_row = entries.index(state.mailbox_index)
    top = _window(len(entries), focus_row, height)
    visible = entries[top : top + height]
    return visible[row] if 0 <= row < len(visible) else None


def frame(state: UIState, width: int, height: int) -> RenderableType:
    """The complete screen, exactly ``height`` lines tall.

    Never taller: a frame that overflowed would make ``Live`` scroll the
    alternate screen, and the footer would walk off the bottom.
    """
    width = max(width, 20)
    body_height = max(height - 3, 1)  # header, status, key hints

    if state.view == HELP:
        body = help_pane(state, width, body_height)
    elif state.view == READER:
        body = reader(state, width - 2, body_height)
        body = [Text("  ").append_text(line) for line in body]
    elif not sidebar_visible(state, width):
        body = listing(state, width, body_height)
    else:
        pane_width = width - SIDEBAR_WIDTH - 1
        left = sidebar(state, SIDEBAR_WIDTH, body_height)
        right = listing(state, pane_width, body_height)
        body = [
            Text.assemble(l, ("│", THEME["rule"]), r)
            for l, r in zip(left, right)
        ]

    lines = [
        header(state, width),
        *body,
        status_line(state, width),
        key_hints(state, width),
    ]
    return Group(*(exact(line, width) for line in lines))

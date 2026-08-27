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
from email.utils import parseaddr

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
#
# The palette is deliberately narrow. Chrome sits close to the terminal's own
# background and everything structural is drawn in ``meta``/``rule`` grey, so
# ``accent`` — brass — is the only warm colour on screen and always means the
# same thing: mail that wants something from you. Unread counts, unread dots,
# the mailbox you are in, the keys you can press. Nothing decorative gets it.
#
# ``cursor`` sets a background and no foreground on purpose: the row under the
# cursor keeps its own colour coding instead of being flattened to white, and
# the brass bar in the first column carries the signal on terminals where the
# tint is too subtle to see.
THEME = {
    "bar": "on #1c1f26",
    "button": "#e0a85c on #2a2e38",
    "cursor": "on #2a2e38",
    "cursor_idle": "on #20232b",
    "unread": "bold",
    "read": "",
    "meta": "#8f8c84",
    # The reader is the one place that states both halves of its contrast.
    # Everywhere else inherits the terminal's own foreground, which is fine
    # for a row of chrome — but a message body is the thing you are actually
    # here to read, and inheriting left it at whatever washed-out grey the
    # terminal happened to use for default text. ``page`` is a background and
    # ``body`` a foreground, so the pair is legible whatever the terminal is
    # themed as, instead of only on the themes that happened to work.
    "page": "on #15171d",
    "body": "#e8e5de",
    "quote": "#a8a49a",
    "accent": "#e0a85c",
    "sender": "#93b8cc",
    "star": "#e0a85c",
    "mark": "#8fb08a",
    "rule": "#4a4e58",
    "error": "bold #c2726a",
    "ok": "#8fb08a",
    "warn": "#e0a85c",
}

REFRESH = " ⟳ refresh "
# A narrow geometric family, so nothing in a row is double-width and the grid
# never drifts. Emoji were the previous choice and cost two cells apiece.
STAR = "★"
UNREAD = "●"
CLIP = "▣"
PICTURE = "▨"
TICK = "✓"
CURSOR = "▎"          # the active row / mailbox marker
DIVIDER = "▏"         # the hairline between the two panes
SPINE = "│"           # the thread spine down the reader's gutter
NODE = "◆"            # one message hung off it
DOT = "·"


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
    """The chrome strip: who you are on the left, where you are on the right.

    Built from foreground-only spans over a background-only base style, so
    ``exact`` can pad the bar out to the terminal edge and the padding still
    carries the bar.
    """
    start, _ = refresh_span(width)

    left = Text(style=THEME["bar"])
    left.append(f" {CURSOR}", style=THEME["accent"])
    left.append("gmail", style=f"bold {THEME['accent']}")
    left.append(f"  {state.account}", style=THEME["meta"])

    right = Text(style=THEME["bar"])
    # Carried on the right-hand block so it survives the left being ellipsised
    # into it on a narrow terminal.
    right.append("  ")
    if state.query:
        right.append("search ", style=THEME["meta"])
        right.append(state.query, style="bold")
    else:
        right.append(state.mailbox.title, style="bold")
    if state.as_messages:
        right.append(f"  {DOT} messages", style=THEME["meta"])
    if state.view == LIST and (state.page > 1 or state.has_more):
        # Only once there is more than one page. On a mailbox that fits in a
        # single fetch this would be a permanent "page 1 of 1".
        right.append(f"  {DOT} page {state.page}", style=THEME["accent"])
    unread = state.unread_counts.get(state.mailbox.counter or "", 0)
    if unread:
        right.append(f"  {unread} unread", style=THEME["accent"])
    if state.last_refresh:
        right.append(f"  {DOT} {state.last_refresh.strftime('%H:%M')}",
                     style=THEME["meta"])
    right.append(" ")

    # The right-hand side is laid out first and the left is given whatever
    # is left over. On a narrow terminal that costs you the account name
    # rather than the mailbox you are standing in — which of the two you need
    # to see at a glance is not a close call.
    line = Text(style=THEME["bar"])
    line.append_text(left)
    line.truncate(max(start - cell_len(right.plain), 0),
                  overflow="ellipsis", pad=True)
    line.append_text(right)
    line.truncate(start, overflow="crop", pad=True)
    line.append(REFRESH, style=THEME["button"])
    return line


# What each status style leads with, so a result reads at a glance without
# depending on the terminal reproducing the colour faithfully.
_STATUS_GLYPH = {
    THEME["error"]: ("✗", THEME["error"]),
    THEME["ok"]: ("✓", THEME["ok"]),
    THEME["warn"]: ("!", THEME["warn"]),
}


def status_line(state: UIState, width: int) -> Text:
    if state.prompt is not None:
        editor = state.prompt
        line = Text(f" {CURSOR}", style=THEME["accent"])
        line.append(f"{editor.label}", style=f"bold {THEME['accent']}")
        line.append(editor.text)
        # A block where the caret is, since the real cursor lives elsewhere.
        caret = editor.text[editor.cursor] if editor.cursor < len(editor.text) else " "
        line.append(caret, style="reverse")
        return line
    if not state.status:
        return _blank(width)
    glyph, glyph_style = _STATUS_GLYPH.get(state.status_style, (DOT, THEME["meta"]))
    line = Text(f" {glyph} ", style=glyph_style)
    line.append(_fit(state.status, max(width - 3, 1)), style=state.status_style)
    return line


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
    # `/` and `]` sit early: search and paging are how you reach mail that is
    # not in the first fetch, and a hint the bar has no room for is a feature
    # nobody finds. Ordering costs nothing and the tail is what gets cut.
    hints = [
        ("j/k", "move", "j"), ("↵", "open", "enter"), ("/", "search", "/"),
    ]
    if state.has_more or state.page > 1:
        hints.append(("]/[", "page", "]"))
    hints += [
        ("x", "mark", "x"), ("a", "archive", "a"), ("s", "star", "s"),
        ("u", "unread", "u"), ("L", "label", "L"), ("c", "compose", "c"),
        ("⟳", "refresh", "ctrl-r"), ("?", "help", "?"), ("q", "quit", "q"),
    ]
    return hints


# Two cells between hints, and no separator glyph. A dim interpunct reads
# better but costs a third cell each time, and at eighty columns that is the
# difference between the bar showing "refresh / help / quit" and cutting them
# off. The brass-on-ash contrast already separates one hint from the next.
_HINT_GAP = "  "


def _hint_layout(state: UIState, width: int) -> list[tuple[int, int, str, str, str]]:
    """``(start, end, label, meaning, key)`` for every hint that fits.

    The single source of truth for the footer. ``key_hints`` draws from it and
    ``key_hint_spans`` clicks from it, so a click can never land on a hint the
    frame did not draw.
    """
    out: list[tuple[int, int, str, str, str]] = []
    column = 1
    for label, meaning, key in _keys_for(state):
        span = cell_len(label) + 1 + cell_len(meaning)
        if column + span > width:
            break
        out.append((column, column + span, label, meaning, key))
        column += span + cell_len(_HINT_GAP)
    return out


def key_hint_spans(state: UIState, width: int) -> list[tuple[int, int, str]]:
    """``(start, end, key)`` for each hint, so the footer can be clicked."""
    return [(start, end, key) for start, end, _, _, key in _hint_layout(state, width)]


def key_hints(state: UIState, width: int) -> Text:
    line = Text(" ")
    for position, (_, _, label, meaning, _) in enumerate(_hint_layout(state, width)):
        if position:
            line.append(_HINT_GAP)
        line.append(label, style=f"bold {THEME['accent']}")
        line.append(f" {meaning}", style=THEME["meta"])
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
            line = Text(" ")
            line.append(str(payload), style=THEME["meta"])
            line.append(" ")
            line.append("─" * max(width - cell_len(line.plain) - 1, 0),
                        style=THEME["rule"])
            line.append(" ")
            lines.append(line)
            continue

        index, box = payload  # type: ignore[misc]
        count = state.unread_counts.get(box.counter or "", 0)
        here = index == state.mailbox_index
        line = Text()
        line.append(CURSOR if here else " ", style=THEME["accent"] if here else "")
        line.append(
            _fit(box.title, width - COUNT_WIDTH - 2),
            style=f"bold {THEME['accent']}" if here else ("bold" if count else ""),
        )
        line.append(_rfit(str(count) if count else "", COUNT_WIDTH),
                    style=THEME["accent"] if count else "")
        line.append(" ")
        if here:
            # Background only — the row keeps its own colours underneath.
            line.stylize(THEME["cursor"] if active else THEME["cursor_idle"])
        lines.append(line)
    return _pad(lines, height, width)


# -- listing ------------------------------------------------------------------


def _row_columns(width: int) -> tuple[int, int, int, int, int]:
    """Widths for mark, index, flags, sender, date — subject takes the rest.

    ``mark`` is two cells: the cursor bar, then the selection tick, so a row
    can be both the one under the cursor and one of several marked for an
    action without either signal displacing the other.
    """
    date = 10 if width >= 70 else 8
    sender = 22 if width >= 90 else (16 if width >= 70 else 12)
    return 2, 3, 3, sender, date


# ``format_date`` compresses to a bare time today and to "Mar 05" this year,
# but mail older than that keeps a full YYYY-MM-DD. Cropping that to the narrow
# tier's column gives "2026-0", which is worse than useless — drop the century
# and it fits whole.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _date_cell(date, width: int) -> str:
    stamp = format_date(date)
    if cell_len(stamp) > width and _ISO_DATE.match(stamp):
        stamp = stamp[2:]
    return _rfit(stamp, width)


def _subject_cell(row, width: int, unread: bool) -> Text:
    """Subject, then as much of the snippet as the rest of the column allows.

    Gmail's own snippet is the cheapest triage signal there is — it already
    arrives with the metadata fetch — and a subject alone rarely fills the
    column. Always exactly ``width`` cells.
    """
    weight = THEME["unread"] if unread else THEME["read"]
    subject = " ".join((row.subject or "").split())
    snippet = " ".join((getattr(row, "snippet", "") or "").split())
    used = cell_len(subject)
    room = width - used - 2
    cell = Text()
    # Under a dozen cells a snippet is noise, not a preview. Show it or don't.
    if not snippet or room < 12:
        cell.append(_fit(subject, width), style=weight)
        return cell
    cell.append(subject, style=weight)
    cell.append("  ")
    cell.append(_fit(snippet, room), style=THEME["meta"])
    return cell


def listing(state: UIState, width: int, height: int) -> list[Text]:
    mark_w, idx_w, flag_w, from_w, date_w = _row_columns(width)
    subject_w = max(width - (mark_w + idx_w + flag_w + from_w + date_w + 4), 8)

    head = Text(
        f"{' ' * mark_w}{_rfit('#', idx_w)} {' ' * flag_w} "
        f"{_fit('FROM', from_w)} {_fit('SUBJECT', subject_w)} {_rfit('DATE', date_w)}",
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
        here = index == state.cursor

        if isinstance(row, Thread):
            who = ", ".join(row.participants) or "(unknown)"
            if row.message_count > 1:
                who = f"{who} ({row.message_count})"
        else:
            who = row.sender_name or "(unknown)"

        line = Text()
        line.append(CURSOR if here else " ", style=THEME["accent"] if here else "")
        line.append(TICK if marked else " ", style=THEME["mark"])
        line.append(_rfit(str(index + 1), idx_w), style=THEME["meta"])
        line.append(" ")
        line.append(UNREAD if unread else " ", style=THEME["accent"])
        line.append(STAR if row.is_starred else " ", style=THEME["star"])
        line.append(CLIP if row.has_attachments else " ", style=THEME["meta"])
        line.append(" ")
        line.append(
            _fit(who, from_w),
            style=f"bold {THEME['sender']}" if unread else THEME["sender"],
        )
        line.append(" ")
        line.append_text(_subject_cell(row, subject_w, unread))
        line.append(" ")
        line.append(_date_cell(row.date, date_w), style=THEME["meta"])

        if here:
            # Background only, so the flags and the sender keep their colour.
            line.stylize(THEME["cursor"] if active else THEME["cursor_idle"])
        lines.append(line)

    return _pad(lines, height, width)


# -- reader -------------------------------------------------------------------

# Three cells of gutter down the left of the reader — a cell of margin off
# the terminal edge, the spine, and a cell of air before the text.
GUTTER = 3


def reader_lines(state: UIState, width: int) -> tuple[list[Text], list[int]]:
    """Flatten the open conversation into wrapped lines, plus message offsets.

    The offsets are what ``n``/``p`` jump between, so navigating a twelve-message
    thread does not mean holding a scroll key.

    Every line carries a two-cell gutter. In a conversation of more than one
    message that gutter is the *spine*: a hairline running the height of the
    thread with a node at each message, so twelve messages read as one chain
    rather than as twelve slabs divided by rules. A single-message thread has
    no chain to draw, so its gutter is blank — the device only appears where
    there is something for it to say.
    """
    thread = state.thread
    if thread is None:
        return [], []

    content = max(width - GUTTER, 10)
    chained = len(thread.messages) > 1
    lines: list[Text] = []
    starts: list[int] = []

    for position, msg in enumerate(thread.messages):
        if position:
            lines.append(_hang(SPINE if chained else " ", Text("")))
        starts.append(len(lines))
        block = _message_block(msg, content, show_quoted=state.show_quoted,
                               position=position, total=len(thread.messages))
        for offset, line in enumerate(block):
            if not chained:
                glyph = " "
            else:
                glyph = NODE if offset == 0 else SPINE
            lines.append(_hang(glyph, line))
    return lines, starts


def _hang(glyph: str, line: Text) -> Text:
    """Hang one content line off the gutter.

    The gutter and the spine are appended as foreground spans over a
    *background-only* base style — the same trick the header uses. A base
    style carrying a foreground would reach everything appended after it,
    which is how the message body once came out in hairline grey; a base
    style carrying only a background reaches the padding ``exact`` adds and
    nothing else, so the reader is one continuous surface out to the right
    edge and every span keeps its own colour.
    """
    out = Text(style=THEME["page"])
    out.append(f" {glyph} ",
               style=THEME["accent"] if glyph == NODE else THEME["rule"])
    return out.append_text(line)


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


def _byline(msg: Message, width: int) -> Text:
    """Who sent it, from what address, and when — on one line.

    Replaces the ``From:`` / ``Date:`` label stack. The labels were four cells
    of chrome apiece restating what the shape of the value already says.
    """
    name, address = parseaddr(msg.sender)
    line = Text()
    line.append(name or address or msg.sender or "(unknown)",
                style=f"bold {THEME['sender']}")
    if name and address:
        line.append(f"  {address}", style=THEME["meta"])
    if msg.date:
        stamp = msg.date.astimezone().strftime("%d %b %Y %H:%M %Z").strip()
        line.append(f"  {DOT}  {stamp}", style=THEME["meta"])
    if msg.is_starred:
        line.append(f"  {STAR}", style=THEME["star"])
    if msg.is_unread:
        line.append(f"  {UNREAD}", style=THEME["accent"])
    line.truncate(width, overflow="ellipsis")
    return line


def _message_block(
    msg: Message, width: int, *, show_quoted: bool, position: int, total: int
) -> list[Text]:
    lines: list[Text] = []
    lines.extend(_wrap(msg.subject, width, f"bold {THEME['body']}"))
    lines.append(_byline(msg, width))
    for label, value in (("to", msg.to), ("cc", msg.cc)):
        if value:
            head = Text()
            head.append(f"{label} ", style=THEME["meta"])
            head.append(_fit(value, max(width - len(label) - 1, 8)),
                        style=THEME["meta"])
            lines.append(head)
    lines.append(Text(""))

    body = msg.body_text or (html_to_text(msg.body_html) if msg.body_html else None)
    if body is None:
        lines.append(Text("(no readable text body)", style=THEME["meta"]))
    else:
        visible, quoted = split_quoted(body)
        lines.extend(_wrap(visible, width, THEME["body"]))
        if quoted:
            if show_quoted:
                # Dimmer than the live text, but still a readable weight —
                # quoted history is context you sometimes need to read, not
                # decoration.
                lines.extend(_wrap(quoted, width, THEME["quote"]))
            else:
                count = len(quoted.splitlines())
                lines.append(Text(""))
                fold = Text()
                # A disclosure triangle, because that is what it is.
                fold.append("▸ ", style=THEME["accent"])
                fold.append(f"{count} quoted line{'s' if count != 1 else ''}"
                            f" {DOT} Q to expand", style=THEME["meta"])
                lines.append(fold)

    if msg.attachments:
        lines.append(Text(""))
        head = Text()
        head.append("ATTACHMENTS", style=THEME["meta"])
        # The keys go beside the thing they act on. Every attachment can be
        # written to disk — PDFs, archives, whatever — and `w` is the only way
        # to find that out without opening the key reference.
        head.append(f"  {DOT}  ", style=THEME["rule"])
        head.append("w", style=THEME["accent"])
        head.append(" to save", style=THEME["meta"])
        lines.append(head)
        for att in msg.attachments:
            image = is_image(att.mime_type, att.filename)
            entry = Text()
            entry.append(f"{PICTURE if image else CLIP} ", style=THEME["accent"])
            entry.append(f"[{att.index}] ", style=THEME["meta"])
            entry.append(att.filename, style=THEME["body"])
            entry.append(f"  {att.mime_type} {DOT} {format_size(att.size)}",
                         style=THEME["meta"])
            if image:
                entry.append("  i to view", style=THEME["accent"])
            entry.truncate(width, overflow="ellipsis")
            lines.append(entry)
    return lines


def reader(state: UIState, width: int, height: int) -> list[Text]:
    lines, _ = reader_lines(state, width)
    top = max(0, min(state.reader_offset, max(0, len(lines) - height)))
    visible = list(lines[top : top + height])
    # Blank rows below a short message are part of the page, not a hole in it.
    visible.extend(
        Text(style=THEME["page"]) for _ in range(height - len(visible))
    )
    return visible[:height]


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
        ("/", "search with Gmail's own query syntax — from:, has:attachment,"),
        ("", "newer_than:7d, is:unread, label:… ; Esc clears it again"),
        ("]  or  >", "next page — mail past the ones already fetched"),
        ("[  or  <", "previous page"),
        ("t", "toggle conversations ↔ individual messages"),
        ("n", "page size: how many to fetch at a time (up to 500)"),
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
        ("w", "save attachments — one of them, 1,3 / 2-4, or a for all"),
        ("i", "view an image attachment inline"),
    )),
    ("Writing", (
        ("c", "compose a new message"),
        ("r / R", "reply / reply to all"),
        ("f", "forward"),
        ("", "each opens $EDITOR and confirms before anything is sent"),
    )),
    ("Reading the list", (
        (UNREAD, "unread"),
        (STAR, "starred"),
        (CLIP, "has an attachment"),
        (TICK, "marked — the next action applies to it"),
    )),
    ("Notes", (
        ("", "images need a terminal that draws them — Ghostty, Kitty,"),
        ("", "WezTerm, iTerm2. PNG works as-is; other formats want"),
        ("", "pip install 'gmcli[images]'. URLs in a body are clickable."),
        ("", "opening a conversation marks it read, as a mail client does;"),
        ("", "press u to put it back."),
        ("", "the sidebar, / and ] are three ways at the same mailbox: a"),
        ("", "mailbox filters, a search queries, a page walks further back."),
        ("", "the rows here are the same #1, #2, #3 the CLI uses, so you can"),
        ("", "quit and run  gmail archive '#2'  on what you were just looking at."),
        ("", "gmcli holds the gmail.modify scope: it can never delete mail."),
    )),
)


def help_lines(width: int) -> list[Text]:
    lines: list[Text] = [Text("")]
    for title, items in HELP_SECTIONS:
        head = Text("  ")
        head.append(title.upper(), style=f"bold {THEME['accent']}")
        lines.append(head)
        for key, description in items:
            row = Text("    ")
            row.append(_fit(key, 18), style=THEME["accent"] if key else "")
            row.append(_fit(description, max(width - 24, 10)),
                       style=THEME["meta"] if not key else "")
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
        # The reader owns its own two-cell gutter — that is where the thread
        # spine is drawn — so it is handed the whole width, not an indent.
        body = reader(state, width, body_height)
    elif not sidebar_visible(state, width):
        body = listing(state, width, body_height)
    else:
        pane_width = width - SIDEBAR_WIDTH - 1
        left = sidebar(state, SIDEBAR_WIDTH, body_height)
        right = listing(state, pane_width, body_height)
        body = [
            Text.assemble(l, (DIVIDER, THEME["rule"]), r)
            for l, r in zip(left, right)
        ]

    lines = [
        header(state, width),
        *body,
        status_line(state, width),
        key_hints(state, width),
    ]
    return Group(*(exact(line, width) for line in lines))

"""The interactive mailbox: event loop, key bindings, and actions.

This is a second front end over the *same* four layers the commands use —
``api/`` for every call, ``idref``-compatible listing state in the cache, and
``models`` for every payload. It adds no Gmail capability the CLI lacks, which
is deliberate: the scope is still ``gmail.modify``, so the UI can archive and
trash but can never permanently delete, exactly like ``gmail trash``.

Two integration points are worth knowing about:

* Every listing the UI draws is written to the cache with
  ``cache.set_listing()``, so quitting the UI and running ``gmail archive '#2'``
  acts on the row that was on screen. The two halves share one numbering.
* Composing suspends the UI, hands the terminal to ``$EDITOR`` via
  ``commands.send.compose_in_editor``, and resumes — so a reply written here
  goes through the identical threading code as ``gmail reply``.

Network calls are made synchronously on the key that triggered them. A mail
client that fetched in the background would need a thread and a lock for state
that a person is editing at the same time; a "Loading…" line and a blocking
call is the honest trade at this size.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator, Sequence

from rich.console import Console
from rich.live import Live

from ..api import attachments as attachments_api
from ..api import compose
from ..api import labels as labels_api
from ..api import messages as messages_api
from ..api import threads as threads_api
from ..context import AppContext
from ..errors import GmcliError, UsageError
from ..models import Attachment, Message, Thread
from . import graphics, render
from .keys import KeyReader, LineEditor, Mouse
from .state import HELP, LIST, READER, UIState, build_mailboxes

class _Outcome:
    """Whether the block inside :meth:`MailApp.busy` got all the way through."""

    ok = True


# Keys that mean "go back / get out", in every view.
BACK_KEYS = {"q", "escape", "h", "left"}

# Two clicks on the same row inside this window open it.
DOUBLE_CLICK_SECONDS = 0.45
# How far one notch of the wheel moves.
WHEEL_ROWS = 3

# Kitty's "forget every image you are displaying". Text drawn over an image
# does not erase it, so a redraw after showing one has to say so explicitly.
KITTY_CLEAR = "\x1b_Ga=d,d=A\x1b\\"


class MailApp:
    """The UI's whole behaviour. One instance per ``gmail ui`` invocation."""

    def __init__(
        self,
        app_ctx: AppContext,
        *,
        console: Console | None = None,
        keys: object | None = None,
        limit: int = 50,
        mouse: bool = True,
        images: bool = True,
    ) -> None:
        self.ctx = app_ctx
        self.console = console or Console()
        self.keys = keys or KeyReader(mouse=mouse)
        self.live: Live | None = None
        self.state = UIState(account=app_ctx.account, limit=limit)
        self.images = images
        self.protocol = graphics.detect_protocol() if images else graphics.NONE
        # What to run when the footer prompt is submitted.
        self._on_submit: Callable[[str], None] | None = None
        self._last_click: tuple[int, float] = (-1, 0.0)
        # Where `w` saves, remembered across a session, and what it is about
        # to save once the folder prompt comes back.
        self._download_dir: Path = Path.home() / "Downloads"
        self._pending_download: list[Attachment] = []

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> None:
        self.load_mailboxes()
        # `labels.list` carries no counts — only `labels.get` does — so the
        # sidebar would start blank without this. One batched round trip.
        self.refresh_counts()
        self.reload()
        with self.keys:  # type: ignore[union-attr]
            with Live(
                console=self.console,
                screen=True,
                auto_refresh=False,
                transient=False,
                redirect_stdout=False,
                redirect_stderr=False,
            ) as live:
                self.live = live
                # `\x1b[?1049h` is specified to clear the alternate screen, but
                # not every host that echoes the sequence actually does — and a
                # frame drawn onto stale text is unreadable. One clear is cheap.
                self.console.clear()
                self.draw()
                while not self.state.quit:
                    try:
                        key = self.keys.read()  # type: ignore[union-attr]
                    except KeyboardInterrupt:
                        break
                    if key is None:
                        break
                    self.dispatch(key)
                    self.draw()

    def draw(self) -> None:
        width, height = self.console.size
        frame = render.frame(self.state, width, height)
        if self.live is not None:
            self.live.update(frame, refresh=True)

    @contextmanager
    def busy(self, message: str) -> Iterator["_Outcome"]:
        """Show a status while a blocking call runs, and report failures.

        The yielded outcome says whether the block got all the way through.
        Callers that carry on afterwards need it: the failure is swallowed
        here so one bad response cannot kill the UI, which means the code
        after the block would otherwise run on half-built state and overwrite
        the error on the status line with something misleading.
        """
        previous, previous_style = self.state.status, self.state.status_style
        outcome = _Outcome()
        self.state.note(f"{message}…", render.THEME["meta"])
        self.draw()
        try:
            yield outcome
        except GmcliError as exc:
            outcome.ok = False
            detail = f"{exc.message} {exc.hint or ''}".strip()
            self.state.note(detail, render.THEME["error"])
        except Exception as exc:  # noqa: BLE001 — a UI must not die on one bad call
            outcome.ok = False
            self.state.note(f"{type(exc).__name__}: {exc}", render.THEME["error"])
        else:
            if self.state.status == f"{message}…":
                self.state.note(previous, previous_style)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Hand the terminal to an external program, then take it back."""
        live, self.live = self.live, None
        if live is not None:
            live.stop()
        self.keys.pause()  # type: ignore[union-attr]
        try:
            yield
        finally:
            self.keys.resume()  # type: ignore[union-attr]
            if live is not None:
                live.start(refresh=False)
                self.live = live
            self.draw()

    # -- loading -------------------------------------------------------------

    def load_mailboxes(self) -> None:
        with self.busy("Loading labels"):
            labels = labels_api.fetch_labels(self.ctx.client, self.ctx.cache)
            self.state.mailboxes = build_mailboxes(labels)
            self.state.unread_counts = {
                lb.id: lb.messages_unread or 0 for lb in labels if lb.messages_unread
            }
        if not self.state.mailboxes:
            self.state.mailboxes = list(build_mailboxes([]))

    def refresh(self) -> None:
        """Re-fetch the current mailbox and the unread counts behind it."""
        # Re-fetching the page you are standing on, not the first one: a
        # refresh means "is this still current", not "take me back to the top".
        self.reload(keep_page=True)
        self.refresh_counts()
        if self.state.status_style != render.THEME["error"]:
            when = self.state.last_refresh
            stamp = f" at {when.strftime('%H:%M:%S')}" if when else ""
            self.state.note(f"Refreshed{stamp} — {self.state.status}",
                            render.THEME["ok"])

    def refresh_counts(self) -> None:
        """Re-read unread counts, which ``labels.list`` does not carry."""
        with self.busy("Counting"):
            ids = ["INBOX", "UNREAD", "STARRED", "DRAFT"]
            ids += [
                box.counter
                for box in self.state.mailboxes
                if box.counter and box.counter not in ids
            ]
            details = labels_api.fetch_label_details(self.ctx.client, ids)
            self.state.unread_counts = {
                lb.id: lb.messages_unread or 0 for lb in details
            }

    def reload(self, *, keep_page: bool = False) -> None:
        """Fetch the current mailbox (or search) and record it for ``#N``.

        ``keep_page`` fetches the window ``state.page_token`` points at rather
        than the newest one. Everything that changes *what* is being listed —
        a different mailbox, a new search, a new page size — leaves it false,
        because a token from the previous listing does not mean anything in
        the new one.
        """
        state = self.state
        if not keep_page:
            state.reset_paging()
        box = state.mailbox
        label_ids = list(box.label_ids) or None
        query = state.query if state.query is not None else box.query

        with self.busy(f"Loading {box.title if state.query is None else 'results'}"):
            if state.as_messages:
                ids, next_token = messages_api.list_message_ids_page(
                    self.ctx.client,
                    query=query,
                    label_ids=label_ids,
                    limit=state.limit,
                    include_spam_trash=box.include_spam_trash,
                    page_token=state.page_token,
                )
                state.messages = messages_api.get_messages_metadata(self.ctx.client, ids)
                state.threads = []
                self.ctx.cache.set_listing("message", [m.id for m in state.messages])
            else:
                ids, next_token = threads_api.list_thread_ids_page(
                    self.ctx.client,
                    query=query,
                    label_ids=label_ids,
                    limit=state.limit,
                    include_spam_trash=box.include_spam_trash,
                    page_token=state.page_token,
                )
                state.threads = threads_api.get_threads_metadata(self.ctx.client, ids)
                state.messages = []
                self.ctx.cache.set_listing("thread", [t.id for t in state.threads])

            state.next_token = next_token
            state.cursor = min(state.cursor, max(0, len(state.rows) - 1))
            state.selected.clear()
            state.last_refresh = datetime.now()
            state.note(self._listing_summary())

    def _listing_summary(self) -> str:
        """What the status line says after a fetch.

        It names the page and says there is more, because "50 conversations"
        on its own reads as "this mailbox holds fifty" — which was exactly the
        wrong impression before ``]`` existed.
        """
        state = self.state
        noun = "message" if state.as_messages else "conversation"
        count = len(state.rows)
        parts = [f"{count} {noun}{'' if count == 1 else 's'}"]
        if state.page > 1:
            parts.append(f"page {state.page}")
        if state.has_more:
            parts.append("] for the next page")
        elif state.page > 1:
            parts.append("[ to go back")
        return "  ·  ".join(parts)

    # -- paging --------------------------------------------------------------

    def next_page(self) -> None:
        """The window after this one, using the token Gmail handed back."""
        state = self.state
        if not state.has_more:
            state.note("Nothing after this page", render.THEME["warn"])
            return
        state.page_stack.append(state.page_token)
        state.page_token = state.next_token
        state.cursor = 0
        self.reload(keep_page=True)

    def previous_page(self) -> None:
        state = self.state
        if not state.page_stack:
            state.note("Already on the first page")
            return
        state.page_token = state.page_stack.pop()
        state.cursor = 0
        self.reload(keep_page=True)

    # -- dispatch ------------------------------------------------------------

    def dispatch(self, key: "str | Mouse") -> None:
        if isinstance(key, Mouse):
            self._mouse(key)
            return
        if self.state.prompt is not None:
            self._prompt_key(key)
            return
        if key == "ctrl-c":
            self.state.quit = True
            return
        if self.state.view == HELP:
            self._help_key(key)
            return
        if self.state.view == READER:
            self._reader_key(key)
            return
        self._list_key(key)

    def _help_key(self, key: str) -> None:
        """Scroll the key reference; anything else closes it."""
        page = max(1, (self.console.size[1] - 5) // 2)
        moves = {"j": 1, "down": 1, "k": -1, "up": -1,
                 "space": page, "ctrl-d": page, "pagedown": page,
                 "b": -page, "ctrl-u": -page, "pageup": -page}
        if key in moves:
            self.state.help_offset = max(0, self.state.help_offset + moves[key])
        elif key == "g":
            self.state.help_offset = 0
        elif key == "G":
            length = len(render.help_lines(self.console.size[0]))
            self.state.help_offset = max(0, length - (self.console.size[1] - 3))
        else:
            self.state.help_offset = 0
            self.state.view = LIST if self.state.thread is None else READER

    # -- mouse ---------------------------------------------------------------

    def _mouse(self, event: Mouse) -> None:
        """Route a click or a wheel notch to whatever is under the pointer.

        Only real presses act. Releases and drag reports are dropped, so
        selecting text with the mouse held down does not fire an action.
        """
        width, height = self.console.size
        hit = render.hit_test(self.state, width, height, event.x, event.y)

        if event.is_wheel:
            self._wheel(hit, up=event.button == "wheel-up")
            return
        if not event.is_click or self.state.prompt is not None:
            return

        if hit.region in ("footer", "header") and hit.key:
            self.dispatch(hit.key)
        elif hit.region == "sidebar" and hit.index is not None:
            self.state.focus = "sidebar"
            self.state.mailbox_index = hit.index
            self.state.query = None
            self.state.cursor = 0
            self.state.focus = "list"
            self.reload()
        elif hit.region == "list" and hit.index is not None:
            self._click_row(hit.index, event.button)
        elif hit.region in ("reader", "help"):
            # A click in the body is how you leave the key reference.
            if self.state.view == HELP:
                self._help_key("escape")

    def _click_row(self, index: int, button: str) -> None:
        import time

        self.state.focus = "list"
        if button == "right":
            row = self.state.rows[index]
            self.state.cursor = index
            self.state.toggle_selected(row.id)
            return

        previous, when = self._last_click
        now = time.monotonic()
        self.state.cursor = index
        if previous == index and now - when < DOUBLE_CLICK_SECONDS:
            self._last_click = (-1, 0.0)
            self.open_current()
            return
        self._last_click = (index, now)

    def _wheel(self, hit: render.Hit, *, up: bool) -> None:
        step = -WHEEL_ROWS if up else WHEEL_ROWS
        if self.state.view == HELP:
            self.state.help_offset = max(0, self.state.help_offset + step)
        elif self.state.view == READER:
            self.state.reader_offset = max(0, self.state.reader_offset + step)
        elif hit.region == "sidebar":
            count = len(self.state.mailboxes)
            self.state.mailbox_index = max(0, min(count - 1,
                                                  self.state.mailbox_index + step))
        else:
            self.state.move(step)

    def toggle_mouse(self) -> None:
        """Mouse reporting off hands click-drag selection back to the terminal."""
        on = self.keys.toggle_mouse()  # type: ignore[union-attr]
        self.state.note(
            "Mouse on — click to select, double-click to open"
            if on
            else "Mouse off — your terminal's own text selection works again"
        )

    # -- the footer prompt ---------------------------------------------------

    def ask(self, label: str, handler: Callable[[str], None], initial: str = "") -> None:
        self.state.prompt = LineEditor(label, initial)
        self._on_submit = handler

    def _prompt_key(self, key: str) -> None:
        editor = self.state.prompt
        assert editor is not None
        outcome = editor.handle(key)
        if outcome is None:
            return
        self.state.prompt = None
        handler, self._on_submit = self._on_submit, None
        if outcome == "cancel":
            self.state.note("Cancelled")
            return
        if handler is None:
            return
        try:
            handler(editor.text.strip())
        except GmcliError as exc:
            # Same contract as ``busy()``: a prompt handler runs on the key
            # that submitted it, so anything escaping here would take the
            # session down over one mistyped line.
            detail = f"{exc.message} {exc.hint or ''}".strip()
            self.state.note(detail, render.THEME["error"])
        except Exception as exc:  # noqa: BLE001 — a UI must not die on one bad line
            self.state.note(f"{type(exc).__name__}: {exc}", render.THEME["error"])

    def confirm(self, question: str, action: Callable[[], None]) -> None:
        """Ask for a y/n on the status line before doing something."""

        def answered(text: str) -> None:
            if text.lower() in ("y", "yes"):
                action()
            else:
                self.state.note("Cancelled")

        self.ask(f"{question} [y/N] ", answered)

    # -- list view -----------------------------------------------------------

    def _list_key(self, key: str) -> None:
        state = self.state
        page = max(1, (self.console.size[1] - 5) // 2)

        if state.focus == "sidebar" and key in ("j", "down", "k", "up", "enter", "l", "right"):
            self._sidebar_key(key)
            return

        if key in ("j", "down"):
            state.move(1)
        elif key in ("k", "up"):
            state.move(-1)
        elif key == "g":
            state.cursor = 0
        elif key == "G":
            state.cursor = max(0, len(state.rows) - 1)
        elif key in ("ctrl-d", "pagedown"):
            state.move(page)
        elif key in ("ctrl-u", "pageup"):
            state.move(-page)
        elif key == "tab":
            # The sidebar is not drawn on a narrow terminal, so focusing it
            # would move an invisible cursor.
            if self.console.size[0] < render.SIDEBAR_MIN_WIDTH:
                state.focus = "list"
                state.note("Terminal too narrow for the sidebar — use / to search")
            else:
                state.focus = "sidebar" if state.focus == "list" else "list"
        elif key in ("enter", "l", "right"):
            self.open_current()
        elif key == "q":
            state.quit = True
        elif key == "escape":
            if state.query is not None:
                state.query = None
                self.reload()
            else:
                state.quit = True
        elif key in ("x", "space"):
            row = state.current
            if row is not None:
                state.toggle_selected(row.id)
                state.move(1)
        elif key == "v":
            state.selected.clear()
            state.note("Selection cleared")
        elif key == "/":
            self.ask("search: ", self._do_search, state.query or "")
        elif key == "t":
            state.as_messages = not state.as_messages
            self.reload()
        elif key == "n":
            self.ask("fetch how many per page: ", self._do_limit, str(state.limit))
        elif key in ("]", ">"):
            self.next_page()
        elif key in ("[", "<"):
            self.previous_page()
        elif key in ("ctrl-r", "."):
            self.refresh()
        elif key == "?":
            state.view = HELP
        elif key == "c":
            self.compose_new()
        else:
            self._action_key(key)

    def _sidebar_key(self, key: str) -> None:
        state = self.state
        if key in ("j", "down"):
            state.mailbox_index = min(len(state.mailboxes) - 1, state.mailbox_index + 1)
        elif key in ("k", "up"):
            state.mailbox_index = max(0, state.mailbox_index - 1)
        elif key in ("enter", "l", "right"):
            state.focus = "list"
            state.query = None
            state.cursor = 0
            self.reload()

    def _do_search(self, text: str) -> None:
        if not text:
            self.state.note("Empty search — showing the mailbox again")
            self.state.query = None
        else:
            # Config aliases work here exactly as they do in `gmail search`.
            self.state.query = self.ctx.config.aliases.get(text, text)
        self.state.cursor = 0
        self.reload()

    def _do_limit(self, text: str) -> None:
        try:
            value = int(text)
        except ValueError:
            self.state.note(f"Not a number: {text!r}", render.THEME["error"])
            return
        self.state.limit = max(1, min(value, 500))
        self.state.cursor = 0
        # A page size change invalidates every token collected at the old one.
        self.reload()

    # -- reader view ---------------------------------------------------------

    def open_current(self) -> None:
        state = self.state
        row = state.current
        if row is None:
            return
        with self.busy("Opening"):
            if isinstance(row, Thread):
                thread = threads_api.get_thread(self.ctx.client, row.id)
            else:
                message = messages_api.get_message(
                    self.ctx.client, row.id, cache=self.ctx.cache
                )
                thread = Thread(id=message.thread_id, messages=[message], message_count=1)
            state.thread = thread
            state.reader_offset = 0
            state.show_quoted = False
            state.view = READER
            self._mark_open_as_read(thread)
            count = len(thread.messages)
            state.note(
                f"{count} message{'' if count == 1 else 's'}"
                + ("  ·  n/p to move between them" if count > 1 else "")
            )

    def _mark_open_as_read(self, thread: Thread) -> None:
        """Opening mail marks it read, the way every mail client does.

        The CLI deliberately does not (``gmail read`` shows without touching
        state, and ``--mark-read`` is opt-in) — but here the user is looking at
        the message, which is the same signal Gmail's own UI acts on. ``u``
        puts it straight back.
        """
        unread = [m.id for m in thread.messages if m.is_unread]
        if not unread:
            return
        messages_api.modify(self.ctx.client, unread, remove=["UNREAD"])
        for msg in thread.messages:
            if msg.id in unread:
                msg.label_ids = [lid for lid in msg.label_ids if lid != "UNREAD"]
        self._patch_rows(unread + [thread.id], remove=["UNREAD"])

    def _reader_key(self, key: str) -> None:
        state = self.state
        height = max(1, self.console.size[1] - 3)

        if key in BACK_KEYS:
            state.view = LIST
            state.thread = None
        elif key in ("j", "down"):
            state.reader_offset += 1
        elif key in ("k", "up"):
            state.reader_offset = max(0, state.reader_offset - 1)
        elif key in ("space", "ctrl-d", "pagedown"):
            state.reader_offset += height // 2
        elif key in ("b", "ctrl-u", "pageup"):
            state.reader_offset = max(0, state.reader_offset - height // 2)
        elif key == "g":
            state.reader_offset = 0
        elif key == "G":
            state.reader_offset = max(0, self._reader_length() - height)
        elif key in ("n", "p"):
            self._jump_message(1 if key == "n" else -1)
        elif key == "Q":
            state.show_quoted = not state.show_quoted
            state.note("Quoted history shown" if state.show_quoted else "Quoted history hidden")
        elif key == "?":
            state.view = HELP
        else:
            self._action_key(key)

    def _reader_length(self) -> int:
        lines, _ = render.reader_lines(self.state, self.console.size[0])
        return len(lines)

    def _jump_message(self, delta: int) -> None:
        _, starts = render.reader_lines(self.state, self.console.size[0])
        if not starts:
            return
        current = max((i for i, start in enumerate(starts)
                       if start <= self.state.reader_offset), default=0)
        target = max(0, min(len(starts) - 1, current + delta))
        self.state.reader_offset = starts[target]
        self.state.note(f"Message {target + 1} of {len(starts)}")

    # -- actions shared by both views ----------------------------------------

    def _action_key(self, key: str) -> None:
        handlers: dict[str, Callable[[], None]] = {
            "a": lambda: self.modify(remove=["INBOX"], verb="Archived"),
            "A": lambda: self.modify(add=["INBOX"], verb="Moved to inbox"),
            "s": self.toggle_star,
            "u": self.toggle_unread,
            "d": self.trash,
            "L": self.prompt_label,
            "w": self.prompt_download,
            "r": lambda: self.reply(all_recipients=False),
            "R": lambda: self.reply(all_recipients=True),
            "f": self.forward,
            "c": self.compose_new,
            "i": self.view_images,
            "M": self.toggle_mouse,
            "ctrl-r": self.refresh,
            ".": self.refresh,
            "?": lambda: setattr(self.state, "view", HELP),
        }
        handler = handlers.get(key)
        if handler is not None:
            handler()

    def _acting_on(self) -> list[str]:
        """Ids the next action applies to, in either view."""
        if self.state.view == READER and self.state.thread is not None:
            return [self.state.thread.id]
        return self.state.targets

    def _patch_rows(self, ids: Sequence[str], *, add: Sequence[str] = (),
                    remove: Sequence[str] = ()) -> None:
        """Apply a label change to the on-screen rows, so the list updates now.

        Re-listing after every keystroke would be correct and unusably slow;
        the next reload reconciles anyway.
        """
        touched = set(ids)
        for row in self.state.rows:
            members = [row] if isinstance(row, Message) else row.messages
            if row.id not in touched and not any(m.id in touched for m in members):
                continue
            for msg in members:
                labels = [lid for lid in msg.label_ids if lid not in remove]
                labels.extend(lid for lid in add if lid not in labels)
                msg.label_ids = labels

    def _drop_rows(self, ids: Sequence[str]) -> None:
        """Remove rows that no longer belong in this mailbox."""
        gone = set(ids)
        if self.state.as_messages:
            self.state.messages = [m for m in self.state.messages if m.id not in gone]
        else:
            self.state.threads = [t for t in self.state.threads if t.id not in gone]
        self.state.selected -= gone
        self.state.cursor = min(self.state.cursor, max(0, len(self.state.rows) - 1))
        self.ctx.cache.set_listing(
            "message" if self.state.as_messages else "thread",
            [row.id for row in self.state.rows],
        )

    def modify(self, *, add: Sequence[str] = (), remove: Sequence[str] = (),
               verb: str = "Updated", drop: bool = False) -> None:
        ids = self._acting_on()
        if not ids:
            return
        with self.busy(verb):
            if self.state.as_messages and self.state.view == LIST:
                count = messages_api.modify(
                    self.ctx.client, ids, add=list(add), remove=list(remove)
                )
            else:
                count = threads_api.modify_threads(
                    self.ctx.client, ids, add=list(add), remove=list(remove)
                )
            self._patch_rows(ids, add=add, remove=remove)
            if drop or self._leaves_mailbox(add, remove):
                self._drop_rows(ids)
                if self.state.view == READER:
                    self.state.view = LIST
                    self.state.thread = None
            self.state.selected.clear()
            noun = "message" if self.state.as_messages else "conversation"
            self.state.note(
                f"{verb} {count} {noun}{'' if count == 1 else 's'}",
                render.THEME["ok"],
            )

    def _leaves_mailbox(self, add: Sequence[str], remove: Sequence[str]) -> bool:
        """Whether the change means the row no longer belongs where it is."""
        box_labels = set(self.state.mailbox.label_ids)
        if self.state.query is not None:
            return False
        return bool(box_labels & set(remove))

    def toggle_star(self) -> None:
        rows = {row.id: row for row in self.state.rows}
        ids = self._acting_on()
        anchor = rows.get(ids[0]) if ids else None
        if anchor is None and self.state.thread is not None:
            anchor = self.state.thread
        starred = bool(anchor and anchor.is_starred)
        if starred:
            self.modify(remove=["STARRED"], verb="Unstarred")
        else:
            self.modify(add=["STARRED"], verb="Starred")

    def toggle_unread(self) -> None:
        rows = {row.id: row for row in self.state.rows}
        ids = self._acting_on()
        anchor = rows.get(ids[0]) if ids else None
        if anchor is None and self.state.thread is not None:
            anchor = self.state.thread
        if anchor is not None and anchor.is_unread:
            self.modify(remove=["UNREAD"], verb="Marked read")
        else:
            self.modify(add=["UNREAD"], verb="Marked unread")

    def trash(self) -> None:
        ids = self._acting_on()
        if not ids:
            return
        count = len(ids)
        noun = "message" if self.state.as_messages else "conversation"
        label = f"Trash {count} {noun}{'' if count == 1 else 's'}?"

        def do_it() -> None:
            with self.busy("Trashing"):
                if self.state.as_messages and self.state.view == LIST:
                    messages_api.trash(self.ctx.client, ids)
                else:
                    threads_api.trash_threads(self.ctx.client, ids)
                self._drop_rows(ids)
                if self.state.view == READER:
                    self.state.view = LIST
                    self.state.thread = None
                self.state.note(
                    f"Trashed {count} {noun}{'' if count == 1 else 's'} "
                    "— recoverable for 30 days",
                    render.THEME["ok"],
                )

        self.confirm(label, do_it)

    # -- labels --------------------------------------------------------------

    def prompt_label(self) -> None:
        self.ask("label (- to remove): ", self._do_label)

    def _do_label(self, text: str) -> None:
        if not text:
            return
        removing = text.startswith("-")
        name = text.lstrip("-").strip()
        if not name:
            return
        with self.busy("Labelling"):
            index = self.ctx.labels
            existing = index.get(name)
            if existing is None and removing:
                raise UsageError(f"No label named {name!r}.")
            if existing is None:
                label = labels_api.create_label(self.ctx.client, name, self.ctx.cache)
                self.ctx._labels = None  # force a rebuild with the new label
                label_id = label.id
            else:
                label_id = existing.id
            if removing:
                self.modify(remove=[label_id], verb=f"Removed {name} from")
            else:
                self.modify(add=[label_id], verb=f"Labelled {name} on")

    # -- attachments ---------------------------------------------------------

    def prompt_download(self) -> None:
        """Save attachments: any one of them, a subset, or the lot.

        Two prompts rather than one, and only when there is a choice to make.
        A conversation with a single attachment asks just for the folder — the
        common case stays one keystroke and one Enter — while one carrying a
        PDF, a spreadsheet and four images lets you say which, because
        "download everything or nothing" is not a real answer there.

        Nothing here is image- or PDF-specific: whatever Gmail lists as an
        attachment can be written to disk, under the filename rules in
        ``api/attachments.py``.
        """
        # Bound before the block, not inside it: `busy` swallows the failure
        # of the fetch below, and a name that only exists on the happy path
        # would take the whole UI down on the next line.
        items: list[Attachment] = []
        with self.busy("Looking for attachments") as fetch:
            items = self._attachments_here()
        if not fetch.ok:
            return  # the reason is already on the status line
        if not items:
            self.state.note("No attachments here", render.THEME["warn"])
            return
        if len(items) == 1:
            self._ask_folder(items)
            return

        self.state.note("  ".join(f"[{a.index}] {a.filename}" for a in items))

        def chosen(text: str) -> None:
            picked = _select_attachments(items, text)
            if not picked:
                self.state.note(f"Nothing matched {text!r}", render.THEME["error"])
                return
            self._ask_folder(picked)

        self.ask(f"save which [1-{len(items)}, a=all]: ", chosen, "a")

    def _ask_folder(self, items: Sequence[Attachment]) -> None:
        self._pending_download = list(items)
        what = (
            items[0].filename
            if len(items) == 1
            else f"{len(items)} files"
        )
        self.ask(f"save {what} to folder: ", self._do_download,
                 str(self._download_dir))

    def _do_download(self, text: str) -> None:
        items, self._pending_download = self._pending_download, []
        if not items:
            return
        target = Path(text or ".").expanduser()
        with self.busy(f"Saving {len(items)} file{'' if len(items) == 1 else 's'}"):
            written = attachments_api.download(self.ctx.client, items, target)
            # Remembered for the rest of the session, so saving a second
            # attachment somewhere is one Enter rather than a retyped path.
            self._download_dir = target
            names = ", ".join(path.name for _, path in written[:3])
            more = "" if len(written) <= 3 else f" (+{len(written) - 3} more)"
            self.state.note(
                f"Saved {len(written)} to {target}: {names}{more}", render.THEME["ok"]
            )

    def _attachments_here(self) -> list:
        """Attachments on the open conversation, or the row under the cursor."""
        thread = self.state.thread
        if thread is None:
            row = self.state.current
            if row is None:
                return []
            if isinstance(row, Message):
                thread = Thread(id=row.thread_id, messages=[
                    messages_api.get_message(self.ctx.client, row.id, cache=self.ctx.cache)
                ])
            else:
                thread = threads_api.get_thread(self.ctx.client, row.id)
        items = [att for msg in thread.messages for att in msg.attachments]
        # Renumber across the conversation, matching `gmail attachments list`.
        from ..models import Attachment

        return [
            Attachment(
                message_id=a.message_id, attachment_id=a.attachment_id,
                filename=a.filename, mime_type=a.mime_type, size=a.size, index=i,
            )
            for i, a in enumerate(items, start=1)
        ]

    # -- images --------------------------------------------------------------

    def view_images(self) -> None:
        """Show an image attachment inline, if the terminal can draw one."""
        if self.protocol == graphics.NONE:
            self.state.note(graphics.unavailable_reason("image/png", self.protocol),
                            render.THEME["warn"])
            return

        found: list[Attachment] = []
        with self.busy("Looking for images") as fetch:
            found = [
                att
                for att in self._attachments_here()
                if graphics.is_image(att.mime_type, att.filename)
            ]
        if not fetch.ok:
            return
        if not found:
            self.state.note("No images here", render.THEME["warn"])
            return
        if len(found) == 1:
            self._show_image(found[0])
            return

        listing = ", ".join(f"[{a.index}] {a.filename}" for a in found)
        self.state.note(listing)

        def chosen(text: str) -> None:
            by_index = {att.index: att for att in found}
            try:
                pick = by_index[int(text)]
            except (ValueError, KeyError):
                self.state.note(f"No image with index {text!r}", render.THEME["error"])
                return
            self._show_image(pick)

        self.ask(f"view which image [{found[0].index}-{found[-1].index}]: ", chosen)

    def _show_image(self, attachment: Attachment) -> None:
        payload: graphics.Rendered | None = None
        with self.busy(f"Fetching {attachment.filename}"):
            data = attachments_api.fetch_attachment(self.ctx.client, attachment)
            width, height = self.console.size
            payload = graphics.render(
                data,
                attachment.mime_type,
                # Leave a line for the caption and a line for the prompt.
                cols=max(width - 2, 4),
                rows=max(height - 4, 2),
                protocol=self.protocol,
            )
        if payload is None:
            self.state.note(
                graphics.unavailable_reason(attachment.mime_type, self.protocol),
                render.THEME["warn"],
            )
            return
        self._present(attachment, payload)

    def _present(self, attachment: Attachment, image: graphics.Rendered) -> None:
        """Draw the image over the UI and hold it until a key is pressed.

        The alternate screen is kept — dropping out of it would flash the
        user's shell. Live is only told to repaint on demand, so raw writes in
        between survive until the redraw at the end.
        """
        note = f" ({image.note})" if image.note else ""
        caption = f" {attachment.filename}  {attachment.mime_type}{note}"
        prompt = " any key to go back "

        live, self.live = self.live, None  # stop draw() painting over it
        try:
            self._emit("\x1b[2J\x1b[H")  # clear, cursor home
            self._emit(f"\x1b[1m{caption}\x1b[0m\r\n\r\n")
            self._emit(image.payload)
            self._emit(f"\r\n\x1b[7m{prompt}\x1b[0m")
            self.keys.read()  # type: ignore[union-attr]
        finally:
            if self.protocol == graphics.KITTY:
                self._emit(KITTY_CLEAR)
            self._emit("\x1b[2J\x1b[H")
            self.live = live
            self.state.note(f"Viewed {attachment.filename}")

    def _emit(self, text: str) -> None:
        """Write straight to the terminal, around rich."""
        try:
            self.console.file.write(text)
            self.console.file.flush()
        except (OSError, ValueError):  # pragma: no cover - closed stream
            pass

    # -- composing -----------------------------------------------------------

    def _current_message(self) -> Message | None:
        """The message a reply or forward should answer."""
        if self.state.thread is not None:
            return self.state.thread.latest
        row = self.state.current
        if row is None:
            return None
        if isinstance(row, Message):
            return messages_api.get_message(self.ctx.client, row.id, cache=self.ctx.cache)
        return threads_api.latest_message(self.ctx.client, row.id)

    def _edit(self, headers: str, initial: str = "") -> str | None:
        """Run ``$EDITOR`` outside the alternate screen. ``None`` if abandoned."""
        from ..commands.send import compose_in_editor

        body: str | None = None
        with self.suspended():
            try:
                body = compose_in_editor(initial, headers=headers)
            except GmcliError as exc:
                self.state.note(exc.message, render.THEME["warn"])
        return body

    def _ask_recipients(
        self,
        label: str,
        then: Callable[[list[str]], None],
        *,
        initial: str = "",
        empty_note: str,
        retry: bool = False,
    ) -> None:
        """Prompt for addresses, and ask again if what came back is not one.

        A typo in a footer prompt is the most ordinary thing a user can do, so
        it must cost them the typo and nothing else. ``validate_addresses``
        raises, and a prompt handler runs on the key that submitted it — so
        letting that escape would end the session and lose the draft. The
        complaint goes in the *label*: while a prompt is open it is what the
        footer shows, and the status line is not on screen to read.
        """

        def handler(text: str) -> None:
            if not text:
                # An empty line is how you back out, and the one case that is
                # not a typo to correct.
                self.state.note(empty_note, render.THEME["warn"])
                return
            try:
                recipients = compose.validate_addresses(
                    compose.split_addresses([text]), field="to"
                )
            except GmcliError as exc:
                self.state.note(exc.message, render.THEME["error"])
                recipients = []
            if not recipients:
                # ``split_addresses`` drops some malformed input outright
                # rather than raising — ``bob@`` parses to nothing at all —
                # so an empty result off a non-empty line is a typo too.
                self._ask_recipients(
                    label,
                    then,
                    initial=text,
                    empty_note=empty_note,
                    retry=True,
                )
                return
            then(recipients)

        # The complaint prefixes the label rather than replacing it, and the
        # unprefixed label is what recurses — two typos in a row must not read
        # "not an address — not an address — to: ".
        self.ask(f"not an address — {label}" if retry else label, handler, initial)

    def _send(self, message, *, thread_id: str | None, verb: str) -> None:
        summary = compose.describe(message)

        def do_it() -> None:
            with self.busy("Sending"):
                messages_api.send_raw(
                    self.ctx.client, compose.encode(message), thread_id=thread_id
                )
                self.state.note(f"{verb} to {summary['to']}", render.THEME["ok"])

        self.confirm(f"{verb.rstrip('ed')} to {summary['to']}?", do_it)

    def reply(self, *, all_recipients: bool) -> None:
        parent: Message | None = None
        with self.busy("Loading message"):
            parent = self._current_message()
        if parent is None:
            return
        to, cc = compose.reply_recipients(
            parent, reply_all=all_recipients, self_address=self.ctx.account
        )
        headers = _header_stub(
            to, cc, compose.reply_subject(parent.subject)
        )
        body = self._edit(headers)
        if not body:
            return
        with self.busy("Building reply"):
            message = compose.build_reply(
                parent, body=body, sender=self.ctx.account,
                reply_all=all_recipients, quote=True,
            )
            self._send(message, thread_id=parent.thread_id, verb="Replied")

    def forward(self) -> None:
        parent: Message | None = None
        with self.busy("Loading message"):
            parent = self._current_message()
        if parent is None:
            return

        def with_recipients(recipients: list[str]) -> None:
            body = self._edit(_header_stub(recipients, [],
                                           compose.forward_subject(parent.subject)))
            with self.busy("Building forward"):
                message = compose.build_forward(
                    parent, to=recipients, body=body or "", sender=self.ctx.account
                )
                self._send(message, thread_id=None, verb="Forwarded")

        self._ask_recipients(
            "forward to: ",
            with_recipients,
            empty_note="No recipients — nothing forwarded",
        )

    def compose_new(self) -> None:
        def with_recipients(recipients: list[str]) -> None:
            def with_subject(subject: str) -> None:
                body = self._edit(_header_stub(recipients, [], subject))
                if not body:
                    return
                with self.busy("Building message"):
                    signature = self.ctx.config.send.signature
                    if signature:
                        body_text = f"{body.rstrip()}\n\n-- \n{signature}"
                    else:
                        body_text = body
                    message = compose.build_message(
                        to=recipients, subject=subject, body_text=body_text,
                        sender=self.ctx.config.send.default_from,
                    )
                    self._send(message, thread_id=None, verb="Sent")

            self.ask("subject: ", with_subject)

        self._ask_recipients(
            "to: ", with_recipients, empty_note="No recipients — nothing sent"
        )


def _select_attachments(
    items: Sequence[Attachment], text: str
) -> list[Attachment]:
    """Which attachments ``1,3`` / ``2-4`` / ``a`` names, in listed order.

    The same shapes ``idref`` accepts for ``#1,3,7``, so "pick some of the
    numbered things on screen" is spelled one way everywhere in gmcli.
    """
    choice = (text or "").strip().lower()
    if choice in ("", "a", "all", "*"):
        return list(items)

    by_index = {att.index: att for att in items}
    wanted: list[int] = []
    for part in choice.replace(" ", ",").split(","):
        if not part:
            continue
        if "-" in part[1:]:
            start, _, end = part.partition("-")
            try:
                span = range(int(start), int(end) + 1)
            except ValueError:
                return []
            wanted.extend(span)
        else:
            try:
                wanted.append(int(part))
            except ValueError:
                return []
    # Listed order, never the order they were typed, and never twice.
    picked = {n for n in wanted if n in by_index}
    return [att for att in items if att.index in picked]


def _header_stub(to: Sequence[str], cc: Sequence[str], subject: str) -> str:
    """The commented header block ``$EDITOR`` opens with, as ``gmail send`` does."""
    return "\n".join(
        f"# {key}: {value}"
        for key, value in (
            ("To", ", ".join(to)), ("Cc", ", ".join(cc)), ("Subject", subject)
        )
        if value
    )


def run(app_ctx: AppContext, *, limit: int = 50) -> None:
    """Entry point used by ``gmail ui``."""
    MailApp(app_ctx, limit=limit).run()

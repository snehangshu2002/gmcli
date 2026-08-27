"""What the UI is currently showing.

Kept apart from both rendering and behaviour so the render layer can stay a
pure function of this object, and so a test can assert on the state after a
sequence of keystrokes without looking at pixels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

from ..models import Label, Message, Thread
from .keys import LineEditor

# The views the UI can be in. `reader` and `help` cover the whole body; `list`
# is the two-pane browser.
LIST = "list"
READER = "reader"
HELP = "help"


@dataclass(frozen=True)
class Mailbox:
    """One row in the sidebar: a saved way of asking Gmail for mail.

    A mailbox is either a label filter, a query, or both — the same two knobs
    ``gmail ls`` and ``gmail search`` expose, so nothing here can show mail the
    CLI could not.
    """

    title: str
    label_ids: tuple[str, ...] = ()
    query: str | None = None
    include_spam_trash: bool = False
    # Which label's unread count to show beside the title, when we know it.
    counter: str | None = None

    @property
    def key(self) -> str:
        return f"{','.join(self.label_ids)}|{self.query or ''}"


# The fixed part of the sidebar. User labels are appended after a separator.
STANDARD_MAILBOXES: tuple[Mailbox, ...] = (
    Mailbox("Inbox", label_ids=("INBOX",), counter="INBOX"),
    Mailbox("Unread", query="is:unread", counter="UNREAD"),
    Mailbox("Starred", label_ids=("STARRED",)),
    Mailbox("Sent", label_ids=("SENT",)),
    Mailbox("Drafts", label_ids=("DRAFT",), counter="DRAFT"),
    Mailbox("All Mail"),
    Mailbox("Trash", query="in:trash", include_spam_trash=True),
    Mailbox("Spam", query="in:spam", include_spam_trash=True),
)


def build_mailboxes(labels: Sequence[Label]) -> list[Mailbox]:
    """Standard mailboxes followed by every label the user created."""
    user = [
        Mailbox(lb.name, label_ids=(lb.id,), counter=lb.id)
        for lb in labels
        if lb.type == "user"
    ]
    user.sort(key=lambda m: m.title.lower())
    return [*STANDARD_MAILBOXES, *user]


@dataclass
class UIState:
    """Everything on screen, and nothing else."""

    account: str
    mailboxes: list[Mailbox] = field(default_factory=list)
    mailbox_index: int = 0

    # The listing pane.
    threads: list[Thread] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    as_messages: bool = False
    cursor: int = 0
    selected: set[str] = field(default_factory=set)
    query: str | None = None  # set when the pane is showing search results
    limit: int = 50

    # The reading pane.
    thread: Thread | None = None
    reader_offset: int = 0
    help_offset: int = 0
    show_quoted: bool = False
    message_starts: list[int] = field(default_factory=list)

    view: str = LIST
    focus: str = "list"  # "list" or "sidebar"
    sidebar_cursor: int = 0

    last_refresh: "datetime | None" = None
    status: str = ""
    status_style: str = "dim"
    prompt: LineEditor | None = None
    unread_counts: dict[str, int] = field(default_factory=dict)
    quit: bool = False

    # -- the rows currently listed ------------------------------------------

    @property
    def rows(self) -> list:
        """The listing, whichever kind it is holding."""
        return self.messages if self.as_messages else self.threads

    @property
    def current(self):
        rows = self.rows
        if not rows:
            return None
        return rows[min(self.cursor, len(rows) - 1)]

    @property
    def mailbox(self) -> Mailbox:
        if not self.mailboxes:
            return STANDARD_MAILBOXES[0]
        return self.mailboxes[min(self.mailbox_index, len(self.mailboxes) - 1)]

    # -- selection ----------------------------------------------------------

    @property
    def targets(self) -> list[str]:
        """What an action applies to: the marked rows, else the cursor row.

        Mirroring ``gmail archive '#1,3,7'`` — marking rows with ``x`` is the
        UI's spelling of a multi-id reference.
        """
        if self.selected:
            ordered = [row.id for row in self.rows if row.id in self.selected]
            return ordered or list(self.selected)
        row = self.current
        return [row.id] if row else []

    def toggle_selected(self, row_id: str) -> None:
        self.selected.symmetric_difference_update({row_id})

    def move(self, delta: int) -> None:
        count = len(self.rows)
        if count:
            self.cursor = max(0, min(count - 1, self.cursor + delta))

    def note(self, message: str, style: str = "dim") -> None:
        self.status = message
        self.status_style = style

"""Single-keystroke input for the interactive UI.

The CLI half of gmcli never needs this — every command reads its arguments and
exits. The UI does: it has to distinguish ``j`` from ``Down`` from ``Ctrl-D``
without waiting for a newline, which means putting the terminal into cbreak
mode and decoding escape sequences by hand.

cbreak rather than raw: it leaves ISIG on, so Ctrl-C still raises
``KeyboardInterrupt`` and a wedged UI can always be escaped the usual way.

Keys are normalized to lowercase names (``"up"``, ``"enter"``, ``"ctrl-d"``,
``"f5"``) or the literal character typed. Everything downstream compares
against those names, so the rest of the UI never sees a byte.

Mouse reports arrive on the same stream and come back as :class:`Mouse`
instead of a string. They are decoded in SGR form (``\x1b[<b;x;yM``) because
the older X10 encoding cannot express a column past 223 — which a wide
terminal reaches easily.

Input is parsed out of a persistent buffer rather than one ``read()`` per key.
A wheel spin delivers several complete reports in a single chunk, and key
repeat does the same for ordinary keys; parsing one event and keeping the rest
is what stops those from being dropped.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Iterable, Iterator

ESC = "\x1b"

# Button-event tracking plus SGR coordinates. 1002 reports presses, releases
# and drags; 1006 lifts the 223-column ceiling of the original encoding.
MOUSE_ON = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
MOUSE_OFF = "\x1b[?1006l\x1b[?1002l\x1b[?1000l"

# Low three bits of the SGR button field, plus the wheel's own codes.
_BUTTONS = {0: "left", 1: "middle", 2: "right", 64: "wheel-up", 65: "wheel-down"}

# CSI final bytes (and `~`-terminated numbers) that we care about. Anything not
# listed decodes to "unknown" and is ignored rather than mistaken for a letter.
_CSI = {
    "A": "up",
    "B": "down",
    "C": "right",
    "D": "left",
    "H": "home",
    "F": "end",
    "Z": "shift-tab",
    "1~": "home",
    "2~": "insert",
    "3~": "delete",
    "4~": "end",
    "5~": "pageup",
    "6~": "pagedown",
    "15~": "f5",
    "17~": "f6",
}

_SIMPLE = {
    "\r": "enter",
    "\n": "enter",
    "\t": "tab",
    "\x7f": "backspace",
    "\x08": "backspace",
    " ": "space",
    ESC: "escape",
}

# Control characters that already have a friendlier name above.
_NAMED_CONTROLS = {"\r", "\n", "\t", "\x08", ESC}


@dataclass(frozen=True)
class Mouse:
    """One mouse report, in zero-based cell coordinates."""

    button: str
    x: int
    y: int
    pressed: bool
    motion: bool = False

    @property
    def is_wheel(self) -> bool:
        return self.button.startswith("wheel")

    @property
    def is_click(self) -> bool:
        """A real button press, not a release and not a drag."""
        return self.pressed and not self.motion and not self.is_wheel


def _parse_mouse(body: str) -> "Mouse | None":
    """Decode an SGR report body: ``<button;col;row`` plus ``M`` or ``m``."""
    final = body[-1]
    try:
        button, col, row = (int(part) for part in body[1:-1].split(";"))
    except ValueError:
        return None
    # Bit 5 marks a motion report — the terminal sends those while a button
    # is held, and treating them as fresh clicks would fire actions on a drag.
    motion = bool(button & 32)
    name = _BUTTONS.get(button) or _BUTTONS.get(button & 0b11)
    if name is None:
        return None
    # The terminal counts from 1; everything above this line counts from 0.
    return Mouse(button=name, x=max(col - 1, 0), y=max(row - 1, 0),
                 pressed=final == "M", motion=motion)


def parse(buffer: str) -> "tuple[str | Mouse | None, int]":
    """Take one event off the front of ``buffer``.

    Returns ``(event, characters consumed)``. ``(None, 0)`` means the buffer
    holds only the start of a sequence and more input is needed — the caller
    decides whether to wait or to treat a lone ``Esc`` as the Esc key.
    """
    if not buffer:
        return None, 0
    head = buffer[0]
    if head != ESC:
        return decode(head), 1
    if len(buffer) == 1:
        return None, 0

    kind = buffer[1]
    if kind == "[":
        if len(buffer) == 2:
            return None, 0
        if buffer[2] == "<":  # SGR mouse
            for index in range(3, len(buffer)):
                if buffer[index] in "Mm":
                    event = _parse_mouse(buffer[2 : index + 1])
                    return (event if event is not None else "unknown"), index + 1
            return None, 0
        # An ordinary CSI runs until a final byte in the 0x40-0x7E range.
        for index in range(2, len(buffer)):
            if "\x40" <= buffer[index] <= "\x7e":
                return decode(ESC, iter(buffer[1 : index + 1])), index + 1
        return None, 0
    if kind == "O":
        if len(buffer) == 2:
            return None, 0
        return decode(ESC, iter(buffer[1:3])), 3
    return decode(ESC, iter(buffer[1:2])), 2


def decode(char: str, more: "Iterator[str] | None" = None) -> str:
    """Turn one keystroke (plus any escape-sequence tail) into a key name.

    ``more`` yields the bytes that were already waiting on the input — an
    escape sequence arrives as one burst, while a lone ``Esc`` arrives alone,
    which is exactly how the two are told apart.
    """
    if char in _SIMPLE and char != ESC:
        return _SIMPLE[char]

    if char == ESC:
        tail = "".join(more) if more is not None else ""
        if not tail:
            return "escape"
        # SS3 (`EscO A`) is what a terminal in application-cursor mode sends.
        if tail[0] in "[O":
            body = tail[1:]
            if body in _CSI:
                return _CSI[body]
            # Strip parameter bytes so `Esc[1;5A` (Ctrl-Up) still resolves.
            if body and body[-1] in "ABCDHFZ":
                return _CSI.get(body[-1], "unknown")
            return "unknown"
        return f"alt-{tail}" if len(tail) == 1 else "unknown"

    if len(char) == 1 and char not in _NAMED_CONTROLS:
        code = ord(char)
        if 1 <= code <= 26:
            return f"ctrl-{chr(code + 96)}"
        if code < 32:
            return "unknown"
    return char


class KeyReader:
    """Reads decoded key names from a terminal in cbreak mode.

    Use as a context manager. ``pause()``/``resume()`` hand the terminal back
    temporarily so an external program — ``$EDITOR`` — can own it, then take it
    back; that pairing is what makes composing from inside the UI possible.
    """

    def __init__(self, stream=None, *, mouse: bool = True) -> None:
        self.stream = stream or sys.stdin
        self.want_mouse = mouse
        self._fd: int | None = None
        self._saved = None
        self._mouse_on = False
        # Holds bytes read but not yet consumed: one chunk can carry several
        # complete events, and can also stop mid-sequence.
        self._buffer = ""

    # -- terminal mode -------------------------------------------------------

    def _isatty(self) -> bool:
        try:
            return self.stream.isatty()
        except (ValueError, AttributeError):  # pragma: no cover - closed stream
            return False

    def __enter__(self) -> "KeyReader":
        self.resume()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.pause()

    def resume(self) -> None:
        if os.name == "nt" or not self._isatty() or self._saved is not None:
            return
        import termios
        import tty

        self._fd = self.stream.fileno()
        self._saved = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self.set_mouse(self.want_mouse)

    def pause(self) -> None:
        """Give the terminal back — cooked mode, mouse reporting off.

        Both halves matter before handing over to ``$EDITOR``: leaving mouse
        tracking on would spray escape sequences into whatever runs next.
        """
        self.set_mouse(False)
        if self._saved is None or self._fd is None:
            return
        import termios

        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
        self._saved = None

    def set_mouse(self, on: bool) -> None:
        """Turn mouse reporting on or off, if this is a terminal at all."""
        if not self._isatty() or on == self._mouse_on:
            return
        try:
            sys.stdout.write(MOUSE_ON if on else MOUSE_OFF)
            sys.stdout.flush()
        except (OSError, ValueError):  # pragma: no cover - closed stdout
            return
        self._mouse_on = on

    def toggle_mouse(self) -> bool:
        """Flip mouse reporting. Off restores the terminal's own selection."""
        self.want_mouse = not self._mouse_on
        self.set_mouse(self.want_mouse)
        return self._mouse_on

    # -- reading -------------------------------------------------------------

    def read(self) -> "str | Mouse | None":
        """Block for one event. ``None`` means the input stream ended."""
        if os.name == "nt":  # pragma: no cover - exercised only on Windows
            return self._read_windows()
        return self._read_posix()

    def _read_chunk(self, timeout: float | None) -> str:
        """Read whatever is available, waiting up to ``timeout`` (None blocks)."""
        import select

        assert self._fd is not None
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            return ""
        try:
            data = os.read(self._fd, 1024)
        except (OSError, InterruptedError):  # pragma: no cover - signal race
            return ""
        return data.decode("utf-8", errors="replace")

    def _read_posix(self) -> "str | Mouse | None":
        if self._saved is None or self._fd is None:
            # Not a terminal (piped input): fall back to line-at-a-time reads
            # so the loop is still drivable from a script.
            char = self.stream.read(1)
            return decode(char) if char else None

        while True:
            event, used = parse(self._buffer)
            if used:
                self._buffer = self._buffer[used:]
                return event

            if self._buffer:
                # A partial sequence. Give the rest of it a moment to arrive;
                # if nothing does, a lone Esc really was the Esc key.
                more = self._read_chunk(0.05)
                if not more:
                    stalled, self._buffer = self._buffer, ""
                    return "escape" if stalled == ESC else "unknown"
                self._buffer += more
                continue

            chunk = self._read_chunk(None)
            if not chunk:
                return None
            self._buffer += chunk

    def _read_windows(self) -> str | None:  # pragma: no cover - Windows only
        import msvcrt

        char = msvcrt.getwch()
        if char in ("\x00", "\xe0"):
            second = msvcrt.getwch()
            return {
                "H": "up", "P": "down", "K": "left", "M": "right",
                "G": "home", "O": "end", "I": "pageup", "Q": "pagedown",
                "S": "delete", "?": "f5",
            }.get(second, "unknown")
        return decode(char)


class ScriptedKeys:
    """A ``KeyReader`` stand-in that replays a fixed list of key names.

    Lets the whole event loop run under test with no terminal at all.
    """

    def __init__(self, keys: "Iterable[str | Mouse]") -> None:
        self.queue: list[str | Mouse] = list(keys)
        self.consumed: list[str | Mouse] = []

    def __enter__(self) -> "ScriptedKeys":
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def pause(self) -> None:
        return None

    def resume(self) -> None:
        return None

    def set_mouse(self, on: bool) -> None:
        return None

    def toggle_mouse(self) -> bool:
        return False

    def read(self) -> "str | Mouse | None":
        if not self.queue:
            return None
        key = self.queue.pop(0)
        self.consumed.append(key)
        return key


class LineEditor:
    """A one-line text field rendered inside the UI's footer.

    Written by hand rather than shelling out to ``input()`` because the UI owns
    the alternate screen — dropping to a cooked-mode prompt would tear it down
    and rebuild it on every search.
    """

    def __init__(self, label: str, text: str = "") -> None:
        self.label = label
        self.text = text
        self.cursor = len(text)

    def handle(self, key: str) -> str | None:
        """Apply a key. Returns ``"submit"``, ``"cancel"``, or ``None``."""
        if key == "enter":
            return "submit"
        if key in ("escape", "ctrl-c", "ctrl-g"):
            return "cancel"
        if key == "backspace":
            if self.cursor:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                self.cursor -= 1
        elif key == "delete":
            self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]
        elif key == "left":
            self.cursor = max(0, self.cursor - 1)
        elif key == "right":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key in ("home", "ctrl-a"):
            self.cursor = 0
        elif key in ("end", "ctrl-e"):
            self.cursor = len(self.text)
        elif key == "ctrl-u":
            self.text = self.text[self.cursor :]
            self.cursor = 0
        elif key == "ctrl-w":
            head = self.text[: self.cursor].rstrip()
            cut = head.rfind(" ") + 1
            self.text = self.text[:cut] + self.text[self.cursor :]
            self.cursor = cut
        elif key == "space":
            self._insert(" ")
        elif len(key) == 1:
            self._insert(key)
        return None

    def _insert(self, char: str) -> None:
        self.text = self.text[: self.cursor] + char + self.text[self.cursor :]
        self.cursor += 1

"""Inline images, for terminals that can draw them.

Three ways to put a picture in a terminal, tried in that order:

* **Kitty graphics protocol** — Ghostty, Kitty, WezTerm, Konsole. PNG bytes go
  over the wire as-is (``f=100``), so a PNG attachment needs no decoding at all.
* **iTerm2 inline images** — iTerm2, WezTerm. Also takes the file bytes whole.
* **Half-block cells** — any truecolor terminal. Two pixels per cell using
  ``▀`` with a foreground and background colour. Needs Pillow to read pixels,
  so it is the one path with a dependency.

Detection is by environment variable. It is not perfect — a multiplexer can
hide the real terminal — so ``GMCLI_IMAGE_PROTOCOL`` overrides it, and
``gmail ui --no-images`` turns the whole thing off.

Nothing here writes to the terminal. These functions return the bytes to emit,
which keeps them testable and keeps the one place that owns the screen in
``app.py``.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

# What each protocol can be handed without decoding first.
_PASSTHROUGH = {"image/png"}
KITTY = "kitty"
ITERM2 = "iterm2"
BLOCKS = "blocks"
NONE = "none"

IMAGE_MIMES = {
    "image/png", "image/jpeg", "image/jpg", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
}

# The kitty protocol caps one escape's payload at 4096 base64 bytes.
_CHUNK = 4096

# Above this, an image is worth shrinking before it goes over the wire: a 4 MB
# phone photo is ~5.5 MB of base64 in some 1300 protocol chunks, and the
# terminal is going to scale it into a few hundred cells regardless.
DOWNSCALE_ABOVE_BYTES = 512_000
# Rough pixels per cell. Generous, so the result still looks sharp on a HiDPI
# display; the terminal does the final fit from the `c`/`r` keys.
CELL_PIXELS = (12, 24)


def is_image(mime_type: str, filename: str = "") -> bool:
    if (mime_type or "").lower() in IMAGE_MIMES:
        return True
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff"}


def have_pillow() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


def detect_protocol(env: dict[str, str] | None = None) -> str:
    """Which image protocol this terminal understands, best first."""
    env = os.environ if env is None else env

    forced = (env.get("GMCLI_IMAGE_PROTOCOL") or "").strip().lower()
    if forced in {KITTY, ITERM2, BLOCKS, NONE}:
        return forced

    term = (env.get("TERM") or "").lower()
    program = (env.get("TERM_PROGRAM") or "").lower()

    if (
        "kitty" in term
        or "ghostty" in term
        or program in {"ghostty", "wezterm"}
        or env.get("KITTY_WINDOW_ID")
        or env.get("GHOSTTY_RESOURCES_DIR")
    ):
        return KITTY
    if program == "iterm.app" or env.get("ITERM_SESSION_ID"):
        return ITERM2
    # A truecolor terminal can still show something, if Pillow is around to
    # turn the file into pixels.
    if env.get("COLORTERM") in {"truecolor", "24bit"} and have_pillow():
        return BLOCKS
    return NONE


@dataclass(frozen=True)
class Rendered:
    """An image ready to emit, and how much room it will take."""

    payload: str
    rows: int
    note: str = ""


def _kitty(data: bytes, cols: int, rows: int) -> str:
    """Transmit-and-display, chunked as the protocol requires.

    ``c``/``r`` ask the terminal to fit the image into that many cells while
    keeping its aspect ratio, which saves us from having to know the pixel
    dimensions.
    """
    encoded = base64.b64encode(data).decode("ascii")
    chunks = [encoded[i : i + _CHUNK] for i in range(0, len(encoded), _CHUNK)] or [""]

    out: list[str] = []
    for index, chunk in enumerate(chunks):
        more = 1 if index < len(chunks) - 1 else 0
        if index == 0:
            control = f"a=T,f=100,t=d,c={cols},r={rows},m={more}"
        else:
            control = f"m={more}"
        out.append(f"\x1b_G{control};{chunk}\x1b\\")
    return "".join(out)


def _iterm2(data: bytes, cols: int, rows: int) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return (
        f"\x1b]1337;File=inline=1;size={len(data)};"
        f"width={cols};height={rows};preserveAspectRatio=1:{encoded}\x07"
    )


def _to_png(data: bytes, mime_type: str, cols: int = 0, rows: int = 0) -> bytes | None:
    """PNG bytes for a protocol that wants PNG, shrunk when that is worth it.

    A PNG small enough to send as-is is passed straight through — that is what
    lets a PNG attachment display with Pillow absent. Everything else needs a
    decoder, and once we have one a large image is resampled to about the size
    it will actually occupy rather than sent whole.
    """
    passthrough = mime_type.lower() in _PASSTHROUGH
    oversized = len(data) > DOWNSCALE_ABOVE_BYTES
    if passthrough and not oversized:
        return data
    if not have_pillow():
        # No decoder available: a PNG still goes as-is, just slowly.
        return data if passthrough else None

    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as source:
            img = source.convert("RGBA")
            if oversized and cols and rows:
                img.thumbnail(
                    (cols * CELL_PIXELS[0], rows * CELL_PIXELS[1]), Image.LANCZOS
                )
            buffer = io.BytesIO()
            img.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 — a corrupt attachment is not our failure
        # A PNG we failed to re-encode can still be sent unchanged.
        return data if passthrough else None


def _blocks(data: bytes, cols: int, rows: int) -> str | None:
    """Draw with ``▀``: two vertical pixels per cell, as fg and bg colours."""
    if not have_pillow():
        return None
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as source:
            img = source.convert("RGB")
            # Each cell is one column and two rows of pixels; terminal cells
            # are about twice as tall as they are wide, so this comes out
            # close to square.
            img.thumbnail((cols, rows * 2), Image.LANCZOS)
            width, height = img.size
            pixels = img.load()
    except Exception:  # noqa: BLE001
        return None

    lines: list[str] = []
    for y in range(0, height - height % 2, 2):
        line: list[str] = []
        for x in range(width):
            top = pixels[x, y]
            bottom = pixels[x, y + 1]
            line.append(
                f"\x1b[38;2;{top[0]};{top[1]};{top[2]}m"
                f"\x1b[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m▀"
            )
        line.append("\x1b[0m")
        lines.append("".join(line))
    return "\n".join(lines)


def render(
    data: bytes,
    mime_type: str,
    *,
    cols: int,
    rows: int,
    protocol: str | None = None,
) -> Rendered | None:
    """Bytes to emit to draw this image, or ``None`` if it cannot be drawn."""
    protocol = protocol or detect_protocol()
    if protocol == NONE or not data:
        return None
    cols = max(1, cols)
    rows = max(1, rows)

    if protocol in (KITTY, ITERM2):
        png = _to_png(data, mime_type, cols, rows)
        if png is None:
            return None
        payload = (
            _kitty(png, cols, rows) if protocol == KITTY else _iterm2(png, cols, rows)
        )
        return Rendered(payload=payload, rows=rows)

    if protocol == BLOCKS:
        drawn = _blocks(data, cols, rows)
        if drawn is None:
            return None
        return Rendered(
            payload=drawn,
            rows=drawn.count("\n") + 1,
            note="half-block preview",
        )
    return None


def unavailable_reason(mime_type: str, protocol: str | None = None) -> str:
    """Why an image could not be shown, phrased as something to do about it."""
    protocol = protocol or detect_protocol()
    if protocol == NONE:
        return (
            "This terminal has no image protocol. Ghostty, Kitty, WezTerm and "
            "iTerm2 do; elsewhere `pip install 'gmcli[images]'` enables a "
            "half-block preview."
        )
    if mime_type.lower() not in _PASSTHROUGH and not have_pillow():
        return (
            f"{mime_type} needs decoding first — "
            "`pip install 'gmcli[images]'` (PNG works without it)."
        )
    return "That file could not be decoded as an image."

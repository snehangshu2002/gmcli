"""The interactive terminal UI behind ``gmail ui``.

A second front end over the same layers the commands use — nothing here talks
to Gmail directly, and nothing here can do anything ``gmail <command>`` cannot.

* ``keys``   — cbreak-mode input, decoded to key names; the footer line editor.
* ``state``  — what is on screen, and nothing else.
* ``render`` — pure state → renderable. No API calls, no mutation.
* ``app``    — the event loop, key bindings, and actions.
"""

from __future__ import annotations

from .app import MailApp, run

__all__ = ["MailApp", "run"]

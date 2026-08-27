"""Disposable on-disk cache.

Everything here is derivable from the API, so the whole directory can be
deleted at any time — ``gmail cache clear`` does exactly that. Nothing secret
is ever written here; tokens live in the data dir.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import cache_dir

# Label ids are stable but names can be renamed out from under us, so the map
# gets a short TTL rather than living forever.
LABEL_TTL_SECONDS = 3600


def _slug(account: str) -> str:
    return "".join(c if c.isalnum() or c in "@.-_" else "_" for c in account)


class Cache:
    """Per-account cache: label map, fetched bodies, and the last listing."""

    def __init__(self, account: str | None) -> None:
        self.account = account or "default"
        self.root = cache_dir() / _slug(self.account)

    # -- generic helpers -----------------------------------------------------

    def _read(self, name: str) -> Any | None:
        path = self.root / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt or missing cache entry is never fatal — just a miss.
            return None

    def _write(self, name: str, payload: Any) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / name
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # An unwritable cache degrades performance, not correctness.
            pass

    # -- label map -----------------------------------------------------------

    def get_labels(self) -> list[dict[str, Any]] | None:
        entry = self._read("labels.json")
        if not entry:
            return None
        if time.time() - entry.get("fetched_at", 0) > LABEL_TTL_SECONDS:
            return None
        return entry.get("labels")

    def set_labels(self, labels: list[dict[str, Any]]) -> None:
        self._write("labels.json", {"fetched_at": time.time(), "labels": labels})

    def invalidate_labels(self) -> None:
        try:
            (self.root / "labels.json").unlink(missing_ok=True)
        except OSError:
            pass

    # -- last listing (backs the #N shorthand) -------------------------------

    def set_listing(self, kind: str, ids: list[str]) -> None:
        """Record the ids shown by the most recent listing, in display order."""
        self._write("last_listing.json", {"kind": kind, "ids": ids, "at": time.time()})

    def get_listing(self) -> tuple[str, list[str]] | None:
        entry = self._read("last_listing.json")
        if not entry or not entry.get("ids"):
            return None
        return entry.get("kind", "thread"), list(entry["ids"])

    # -- message bodies ------------------------------------------------------

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        return self._read(f"bodies/{message_id}.json")

    def set_message(self, message_id: str, payload: dict[str, Any]) -> None:
        (self.root / "bodies").mkdir(parents=True, exist_ok=True)
        self._write(f"bodies/{message_id}.json", payload)

    # -- maintenance ---------------------------------------------------------

    def clear(self) -> int:
        """Delete every cached file. Returns how many were removed."""
        if not self.root.exists():
            return 0
        removed = 0
        for path in sorted(self.root.rglob("*"), reverse=True):
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                pass
        try:
            self.root.rmdir()
        except OSError:
            pass
        return removed

    @staticmethod
    def clear_all() -> int:
        root = cache_dir()
        if not root.exists():
            return 0
        removed = 0
        for path in sorted(root.rglob("*"), reverse=True):
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
                elif path.is_dir():
                    path.rmdir()
            except OSError:
                pass
        return removed


def cache_root() -> Path:
    return cache_dir()

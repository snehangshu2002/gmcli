"""The output contract.

Two properties are locked here because scripts depend on them:

1. JSON key names are public API. Renaming one breaks every `jq` pipeline
   anyone has written, so a rename must break a test first.
2. Under --json, stdout carries the JSON document and nothing else.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pytest

from gmcli.models import Label, Message, Thread
from gmcli.output import Renderer, format_date, format_size, html_to_text

from conftest import make_message

# Golden key sets. Adding a key is fine; removing or renaming one is breaking.
THREAD_KEYS = {
    "id", "subject", "participants", "message_count", "date", "snippet",
    "labels", "unread", "starred", "has_attachments",
}
MESSAGE_KEYS = {
    "id", "thread_id", "subject", "from", "to", "date", "snippet", "labels",
    "unread", "starred", "has_attachments",
}
LABEL_KEYS = {"id", "name", "type", "messages_total", "messages_unread"}
ATTACHMENT_KEYS = {
    "index", "filename", "mime_type", "size", "attachment_id", "message_id",
}


def capture(monkeypatch, fn) -> str:
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    fn()
    return buf.getvalue()


def test_thread_json_keys_are_stable(monkeypatch):
    thread = Thread.from_api(
        {"id": "t1", "snippet": "s", "messages": [make_message("m1", thread_id="t1")]}
    )
    out = capture(
        monkeypatch, lambda: Renderer(json_mode=True).threads([thread])
    )
    payload = json.loads(out)
    assert THREAD_KEYS.issubset(payload[0].keys()), (
        f"missing keys: {THREAD_KEYS - payload[0].keys()}"
    )


def test_message_json_keys_are_stable(monkeypatch):
    msg = Message.from_api(make_message())
    out = capture(monkeypatch, lambda: Renderer(json_mode=True).messages([msg]))
    payload = json.loads(out)
    assert MESSAGE_KEYS.issubset(payload[0].keys())


def test_label_json_keys_are_stable(monkeypatch):
    label = Label(id="Label_1", name="finance", type="user", messages_total=3)
    out = capture(monkeypatch, lambda: Renderer(json_mode=True).labels([label]))
    payload = json.loads(out)
    assert LABEL_KEYS == payload[0].keys()


def test_attachment_json_keys_are_stable(monkeypatch):
    msg = Message.from_api(
        make_message(attachments=[("r.pdf", "application/pdf", 10)])
    )
    out = capture(
        monkeypatch, lambda: Renderer(json_mode=True).attachments(msg.attachments)
    )
    payload = json.loads(out)
    assert ATTACHMENT_KEYS == payload[0].keys()


def test_message_detail_includes_body_only_when_asked(monkeypatch):
    msg = Message.from_api(make_message(body="the body"))
    out = capture(
        monkeypatch, lambda: Renderer(json_mode=True).message_detail([msg])
    )
    payload = json.loads(out)
    assert payload[0]["body_text"] == "the body"


def test_json_mode_emits_exactly_one_document(monkeypatch):
    msg = Message.from_api(make_message())
    renderer = Renderer(json_mode=True)

    def run():
        renderer.info("this must not reach stdout")
        renderer.success("nor this")
        renderer.warn("nor this warning")
        renderer.messages([msg])

    out = capture(monkeypatch, run)
    # Parses cleanly as one document — proof nothing else leaked onto stdout.
    payload = json.loads(out)
    assert isinstance(payload, list)


def test_empty_listing_is_an_empty_json_array(monkeypatch):
    out = capture(monkeypatch, lambda: Renderer(json_mode=True).threads([]))
    assert json.loads(out) == []


def test_result_emits_json_in_json_mode(monkeypatch):
    out = capture(
        monkeypatch,
        lambda: Renderer(json_mode=True).result({"action": "Archived", "count": 2}, "x"),
    )
    assert json.loads(out) == {"action": "Archived", "count": 2}


# -- formatting helpers ------------------------------------------------------


def test_format_date_shows_time_for_today():
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    assert ":" in format_date(datetime(2026, 3, 4, 9, 14, tzinfo=timezone.utc), now=now)


def test_format_date_drops_the_year_within_this_year():
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    assert "2026" not in format_date(
        datetime(2026, 1, 9, 9, 14, tzinfo=timezone.utc), now=now
    )


def test_format_date_keeps_the_year_for_older_mail():
    now = datetime(2026, 3, 4, 15, 0, tzinfo=timezone.utc)
    assert "2024" in format_date(
        datetime(2024, 5, 1, 9, 14, tzinfo=timezone.utc), now=now
    )


def test_format_date_handles_none():
    assert format_date(None) == ""


@pytest.mark.parametrize(
    "size,expected_unit", [(512, "B"), (2048, "KB"), (5 * 1024**2, "MB")]
)
def test_format_size(size, expected_unit):
    assert format_size(size).endswith(expected_unit)


def test_format_size_of_zero_is_blank():
    assert format_size(0) == ""


def test_html_to_text_flattens_markup():
    html = "<html><style>p{}</style><body><p>First</p><p>Second &amp; third</p></body></html>"
    text = html_to_text(html)
    assert "First" in text and "Second & third" in text
    assert "<p>" not in text
    assert "p{}" not in text, "style contents must be dropped"


def test_html_to_text_turns_breaks_into_newlines():
    assert "\n" in html_to_text("one<br>two")

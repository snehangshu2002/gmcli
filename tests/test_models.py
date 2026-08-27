"""Parsing of Gmail API payloads into typed models."""

from __future__ import annotations


from gmcli.models import Message, Thread, split_quoted

from conftest import b64, make_message


def test_parses_headers_and_body():
    msg = Message.from_api(make_message(subject="Hello", body="Body text."))
    assert msg.subject == "Hello"
    assert msg.body_text == "Body text."
    assert msg.sender == "Dana Whitfield <dana@example.com>"
    assert msg.sender_name == "Dana Whitfield"


def test_sender_name_falls_back_to_address():
    msg = Message.from_api(make_message(sender="bare@example.com"))
    assert msg.sender_name == "bare@example.com"


def test_missing_subject_has_a_placeholder():
    payload = make_message()
    payload["payload"]["headers"] = [
        h for h in payload["payload"]["headers"] if h["name"] != "Subject"
    ]
    assert Message.from_api(payload).subject == "(no subject)"


def test_label_flags():
    msg = Message.from_api(make_message(labels=["INBOX", "UNREAD", "STARRED"]))
    assert msg.is_unread and msg.is_starred and msg.in_inbox

    read = Message.from_api(make_message(labels=["INBOX"]))
    assert not read.is_unread and not read.is_starred


def test_date_prefers_the_header():
    msg = Message.from_api(make_message(date="Wed, 04 Mar 2026 09:14:00 +0000"))
    assert msg.date is not None
    assert msg.date.year == 2026 and msg.date.month == 3


def test_date_falls_back_to_internal_date():
    payload = make_message()
    payload["payload"]["headers"] = [
        h for h in payload["payload"]["headers"] if h["name"] != "Date"
    ]
    msg = Message.from_api(payload)
    assert msg.date is not None


def test_attachments_are_collected_with_indices():
    msg = Message.from_api(
        make_message(
            attachments=[
                ("report.pdf", "application/pdf", 1024),
                ("chart.png", "image/png", 2048),
            ]
        )
    )
    assert [a.filename for a in msg.attachments] == ["report.pdf", "chart.png"]
    assert [a.index for a in msg.attachments] == [1, 2]
    assert msg.attachments[0].size == 1024
    assert msg.has_attachments


def test_an_inline_image_is_still_an_attachment():
    """Gmail's own composer marks every image you attach `inline` + cid.

    Skipping those — which gmcli used to do, reasoning that a cid: image is
    page furniture — silently dropped ordinary attachments: a photo sent from
    the Gmail web UI arrived as `attachments == []`.
    """
    payload = make_message(html='<p>see <img src="cid:photo"></p>')
    payload["payload"]["parts"].append(
        {
            "mimeType": "image/png",
            "filename": "holiday.png",
            "body": {"attachmentId": "inline1", "size": 4_120_515},
            "headers": [
                {"name": "Content-Disposition", "value": "inline; filename=holiday.png"},
                {"name": "Content-ID", "value": "<photo>"},
            ],
        }
    )
    msg = Message.from_api(payload)
    assert [a.filename for a in msg.attachments] == ["holiday.png"]
    assert msg.attachments[0].inline is True
    assert msg.attachments[0].content_id == "photo"
    assert msg.has_attachments is True


def test_an_ordinary_attachment_is_not_marked_inline():
    msg = Message.from_api(make_message(attachments=[("r.pdf", "application/pdf", 9)]))
    assert msg.attachments[0].inline is False
    assert msg.attachments[0].content_id == ""


def test_a_part_with_no_filename_is_still_skipped():
    """What a tracking pixel looks like: a body part, not a file."""
    payload = make_message(html="<p>hi</p>")
    payload["payload"]["parts"].append(
        {
            "mimeType": "image/gif",
            "filename": "",
            "body": {"attachmentId": "pixel", "size": 43},
            "headers": [
                {"name": "Content-Disposition", "value": "inline"},
                {"name": "Content-ID", "value": "<pixel>"},
            ],
        }
    )
    assert Message.from_api(payload).attachments == []


def test_nested_multipart_is_walked():
    payload = make_message(html="<p>replaced below</p>")
    payload["payload"]["parts"] = [
        {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64("nested plain")}},
                {"mimeType": "text/html", "body": {"data": b64("<p>nested</p>")}},
            ],
        }
    ]
    msg = Message.from_api(payload)
    assert msg.body_text == "nested plain"
    assert msg.body_html == "<p>nested</p>"


def test_snippet_entities_are_unescaped():
    payload = make_message()
    payload["snippet"] = "Tom &amp; Jerry &lt;3"
    assert Message.from_api(payload).snippet == "Tom & Jerry <3"


# -- threads -----------------------------------------------------------------


def build_thread() -> Thread:
    return Thread.from_api(
        {
            "id": "t1",
            "snippet": "latest",
            "messages": [
                make_message("m1", thread_id="t1", sender="Dana <d@x.com>",
                             subject="Q3", labels=["INBOX"]),
                make_message("m2", thread_id="t1", sender="Priya <p@x.com>",
                             subject="Re: Q3", labels=["INBOX", "UNREAD"]),
            ],
        }
    )


def test_thread_subject_comes_from_the_first_message():
    assert build_thread().subject == "Q3"


def test_thread_is_unread_if_any_message_is():
    assert build_thread().is_unread


def test_thread_participants_are_distinct_and_ordered():
    assert build_thread().participants == ["Dana", "Priya"]


def test_thread_labels_are_the_union():
    assert set(build_thread().label_ids) == {"INBOX", "UNREAD"}


def test_thread_counts_messages():
    assert build_thread().message_count == 2


# -- quote folding -----------------------------------------------------------


def test_split_quoted_finds_the_on_wrote_marker():
    body = "My reply.\n\nOn Wed, 04 Mar 2026 at 09:14, Dana wrote:\n> original\n> more"
    visible, quoted = split_quoted(body)
    assert visible == "My reply."
    assert "> original" in quoted


def test_split_quoted_handles_original_message_divider():
    body = "Reply here.\n\n-----Original Message-----\nFrom: someone\nstuff"
    visible, quoted = split_quoted(body)
    assert visible == "Reply here."
    assert "Original Message" in quoted


def test_split_quoted_handles_bare_quote_block():
    body = "Short answer.\n\n> what they said\n> more of it"
    visible, quoted = split_quoted(body)
    assert visible == "Short answer."
    assert quoted.strip().startswith(">")


def test_split_quoted_returns_nothing_when_unquoted():
    body = "Just a normal message.\nWith two lines."
    visible, quoted = split_quoted(body)
    assert visible == body
    assert quoted == ""


def test_split_quoted_does_not_eat_a_line_merely_mentioning_wrote():
    body = "I wrote the report yesterday.\nIt is attached."
    visible, quoted = split_quoted(body)
    assert quoted == ""
    assert visible == body

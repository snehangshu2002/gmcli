"""MIME construction, with threading correctness as the headline case.

A reply with wrong In-Reply-To/References still *arrives*, it just detaches
from the conversation in the recipient's client — a failure nothing in the send
path would ever report. Hence direct coverage.
"""

from __future__ import annotations

import pytest

from gmcli.api import compose
from gmcli.errors import UsageError
from gmcli.models import Message

from conftest import make_message


def parent_message(**kwargs) -> Message:
    return Message.from_api(make_message(**kwargs))


# -- headers and encoding ----------------------------------------------------


def test_unicode_subject_is_rfc2047_encoded():
    msg = compose.build_message(
        to=["a@example.com"], subject="Q3 résumé ✓", body_text="hi"
    )
    # Header access returns the decoded value; encoding happens on
    # serialization, so the wire format is what has to be checked.
    assert msg["Subject"] == "Q3 résumé ✓"

    subject_line = next(
        line for line in msg.as_string().splitlines() if line.startswith("Subject:")
    )
    assert "résumé" not in subject_line
    assert "=?utf-8?" in subject_line

    # And the encoded form must decode back to the original.
    from email.header import decode_header, make_header

    encoded = subject_line.removeprefix("Subject:").strip()
    assert str(make_header(decode_header(encoded))) == "Q3 résumé ✓"


def test_unicode_body_survives_roundtrip():
    body = "Grüße — ünïcode ✓ 日本語"
    msg = compose.build_message(to=["a@example.com"], subject="s", body_text=body)
    import email

    parsed = email.message_from_bytes(msg.as_bytes())
    payload = parsed.get_payload(decode=True).decode("utf-8")
    assert body in payload


def test_message_always_has_date_and_message_id():
    msg = compose.build_message(to=["a@example.com"], subject="s", body_text="b")
    assert msg["Date"]
    assert msg["Message-ID"].startswith("<") and msg["Message-ID"].endswith(">")


def test_html_alternative_produces_both_parts():
    msg = compose.build_message(
        to=["a@example.com"],
        subject="s",
        body_text="plain version",
        body_html="<p>rich version</p>",
    )
    types = {p.get_content_type() for p in msg.walk()}
    assert "text/plain" in types and "text/html" in types
    assert "multipart/alternative" in types


# -- recipients --------------------------------------------------------------


def test_split_addresses_handles_repeats_and_commas():
    assert compose.split_addresses(["a@x.com", "b@x.com, c@x.com"]) == [
        "a@x.com",
        "b@x.com",
        "c@x.com",
    ]


def test_split_addresses_preserves_display_names():
    result = compose.split_addresses(["Dana Whitfield <dana@x.com>"])
    assert result == ["Dana Whitfield <dana@x.com>"]


@pytest.mark.parametrize("bad", ["notanemail", "@nope.com", "trailing@"])
def test_validate_addresses_rejects_malformed(bad):
    with pytest.raises(UsageError):
        compose.validate_addresses([bad], field="--to")


def test_build_message_requires_a_recipient():
    with pytest.raises(UsageError):
        compose.build_message(to=[], subject="s", body_text="b")


# -- attachments -------------------------------------------------------------


def test_attachment_gets_correct_mime_type(tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG fake")

    msg = compose.build_message(
        to=["a@x.com"], subject="s", body_text="b", attachments=[pdf, png]
    )
    by_name = {
        p.get_filename(): p.get_content_type()
        for p in msg.walk()
        if p.get_filename()
    }
    assert by_name["report.pdf"] == "application/pdf"
    assert by_name["chart.png"] == "image/png"


def test_attachment_content_survives(tmp_path):
    blob = b"\x00\x01\x02binary\xff"
    path = tmp_path / "data.bin"
    path.write_bytes(blob)
    msg = compose.build_message(
        to=["a@x.com"], subject="s", body_text="b", attachments=[path]
    )
    part = next(p for p in msg.walk() if p.get_filename() == "data.bin")
    assert part.get_payload(decode=True) == blob


def test_missing_attachment_is_a_usage_error(tmp_path):
    with pytest.raises(UsageError, match="not found"):
        compose.build_message(
            to=["a@x.com"],
            subject="s",
            body_text="b",
            attachments=[tmp_path / "nope.txt"],
        )


# -- threading ---------------------------------------------------------------


def test_reply_sets_in_reply_to_and_references():
    parent = parent_message(message_id_header="<first@mail.example.com>")
    reply = compose.build_reply(parent, body="ok", sender="me@example.com")

    assert reply["In-Reply-To"] == "<first@mail.example.com>"
    assert reply["References"] == "<first@mail.example.com>"


def test_reply_extends_an_existing_references_chain():
    parent = parent_message(
        message_id_header="<third@mail>",
        references="<first@mail> <second@mail>",
    )
    reply = compose.build_reply(parent, body="ok", sender="me@example.com")

    assert reply["References"] == "<first@mail> <second@mail> <third@mail>"
    assert reply["In-Reply-To"] == "<third@mail>"


def test_reply_subject_gets_one_re_prefix():
    assert compose.reply_subject("Lunch") == "Re: Lunch"


@pytest.mark.parametrize("subject", ["Re: Lunch", "RE: Lunch", "re : Lunch"])
def test_reply_subject_is_not_double_prefixed(subject):
    assert compose.reply_subject(subject) == subject


def test_reply_subject_does_not_match_a_word_starting_with_re():
    # "Renewal" starts with "re" but is not a reply prefix.
    assert compose.reply_subject("Renewal notice") == "Re: Renewal notice"


def test_forward_subject_is_not_double_prefixed():
    assert compose.forward_subject("Fwd: Notes") == "Fwd: Notes"
    assert compose.forward_subject("Notes") == "Fwd: Notes"


def test_reply_goes_to_the_sender():
    parent = parent_message(sender="Dana <dana@example.com>")
    reply = compose.build_reply(parent, body="ok", sender="me@example.com")
    assert "dana@example.com" in reply["To"]


def test_reply_prefers_reply_to_over_from():
    parent = parent_message(
        sender="noreply@example.com", reply_to="humans@example.com"
    )
    reply = compose.build_reply(parent, body="ok", sender="me@example.com")
    assert "humans@example.com" in reply["To"]
    assert "noreply@example.com" not in reply["To"]


def test_reply_all_carries_others_and_drops_self():
    parent = parent_message(
        sender="dana@example.com",
        to="me@example.com, priya@example.com",
        cc="marcus@example.com",
    )
    reply = compose.build_reply(
        parent, body="ok", sender="me@example.com", reply_all=True
    )
    combined = f"{reply['To']} {reply['Cc']}"
    assert "priya@example.com" in combined
    assert "marcus@example.com" in combined
    # We must not copy ourselves.
    assert "me@example.com" not in combined


def test_plain_reply_does_not_cc_anyone():
    parent = parent_message(to="me@example.com, priya@example.com")
    reply = compose.build_reply(parent, body="ok", sender="me@example.com")
    assert not reply["Cc"]


def test_reply_quotes_the_parent_by_default():
    parent = parent_message(body="Original text here.")
    reply = compose.build_reply(parent, body="My answer.", sender="me@example.com")
    payload = reply.get_payload(decode=True).decode("utf-8")
    assert "My answer." in payload
    assert "> Original text here." in payload
    assert "wrote:" in payload


def test_reply_can_skip_quoting():
    parent = parent_message(body="Original text here.")
    reply = compose.build_reply(
        parent, body="My answer.", sender="me@example.com", quote=False
    )
    payload = reply.get_payload(decode=True).decode("utf-8")
    assert "Original text here." not in payload


def test_forward_inlines_the_original_headers():
    parent = parent_message(subject="Invoice 4471", sender="billing@example.com")
    fwd = compose.build_forward(
        parent, to=["legal@example.com"], body="FYI", sender="me@example.com"
    )
    payload = fwd.get_payload(decode=True).decode("utf-8")
    assert "Forwarded message" in payload
    assert "billing@example.com" in payload
    assert fwd["Subject"] == "Fwd: Invoice 4471"


# -- encoding for the API ----------------------------------------------------


def test_encode_is_base64url_and_decodes_back():
    import base64

    msg = compose.build_message(to=["a@x.com"], subject="s", body_text="b")
    encoded = compose.encode(msg)
    assert "+" not in encoded and "/" not in encoded
    pad = "=" * (-len(encoded) % 4)
    assert base64.urlsafe_b64decode(encoded + pad) == msg.as_bytes()


def test_describe_summarizes_for_json(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("x")
    msg = compose.build_message(
        to=["a@x.com"], cc=["b@x.com"], subject="s", body_text="b", attachments=[path]
    )
    described = compose.describe(msg)
    assert described["to"] == "a@x.com"
    assert described["cc"] == "b@x.com"
    assert described["attachments"] == ["a.txt"]
    assert described["size_bytes"] > 0

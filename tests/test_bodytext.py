"""Making unreadable mail readable.

The cases here are taken from a real Google account notification: a message
whose `text/plain` part is a flattened copy of its HTML, complete with the
conditional-comment scaffolding, every paragraph twice, and hrefs glued to
their anchor text.
"""

from __future__ import annotations

import pytest

from rich.text import Text

from gmcli.bodytext import (
    clean_plain_text,
    footnote_targets,
    html_to_text,
    linkify,
    message_body,
)
from gmcli.models import Message

LONG_URL = "https://c.gle/" + "A" * 140
SHORT_URL = "https://example.com/hi"


def body(text: str) -> str:
    """The text without its trailing link footnotes."""
    return text.split("\nLinks\n")[0]


# -- plain text ---------------------------------------------------------------


def test_conditional_comment_markers_are_dropped():
    text = clean_plain_text(
        "<!--[if !mso]><!-->\n\nHello\n\n<!--[if false]><!-->\n\nWorld\n"
    )
    assert text == "Hello\n\nWorld"


def test_the_outlook_only_branch_is_dropped_whole():
    text = clean_plain_text(
        "Before\n\n<!--[if mso]>\n<v:roundrect href=x stroke=false>\n"
        "<w:anchorlock/>\n<![endif]-->\n\nAfter\n"
    )
    assert "roundrect" not in text and "anchorlock" not in text
    assert "Before" in text and "After" in text


def test_a_repeated_run_of_paragraphs_is_shown_once():
    once = "You're receiving this email.\n\nEdmingle\n\non 27 August."
    text = clean_plain_text(f"<!--[if false]><!-->\n\n{once}\n\n{once}\n")
    assert text.count("You're receiving this email.") == 1
    assert text.count("Edmingle") == 1


def test_a_lone_repeat_survives_deduplication():
    # The address is both the header line and the value under "Email address".
    # Dropping the second copy would leave the label with nothing under it.
    text = clean_plain_text(
        "<!--[if false]><!-->\n\nme@example.com\n\nSomething else\n\n"
        "Name\n\nme@example.com\n\nEmail address\n"
    )
    assert text.count("me@example.com") == 2


def test_repeats_are_left_alone_without_conditional_residue():
    # No residue means a human wrote this, and repetition is theirs to keep.
    text = clean_plain_text("Yes\n\nNo\n\nYes\n")
    assert text.count("Yes") == 2


def test_a_long_url_becomes_a_footnote():
    text = clean_plain_text(f"See <{LONG_URL}>\n")
    assert "[1]" in body(text)
    assert LONG_URL not in body(text)
    assert f"[1] {LONG_URL}" in text


def test_the_same_url_twice_gets_one_footnote():
    text = clean_plain_text(f"One <{LONG_URL}> and two <{LONG_URL}>\n")
    assert text.count(LONG_URL) == 1
    assert body(text).count("[1]") == 2


def test_a_short_url_stays_in_the_sentence():
    text = clean_plain_text(f"See {SHORT_URL} for more\n")
    assert SHORT_URL in text
    assert "Links" not in text


def test_an_href_glued_to_its_anchor_text_is_put_back_in_order():
    text = clean_plain_text(f"sign in to\n<{LONG_URL}>Edmingle\n")
    assert "Edmingle [1]" in body(text)


def test_soft_wrapped_lines_are_rejoined():
    text = clean_plain_text(
        "This email summarises the info that you shared. There's nothing  \n"
        "that you need to do right now.\n"
    )
    assert text.count("\n") == 0


def test_a_signature_delimiter_is_not_swallowed():
    text = clean_plain_text("Thanks\n\n-- \nSnehangshu\n")
    assert "--\nSnehangshu" in text


def test_quoted_lines_are_not_rejoined():
    text = clean_plain_text("> first line ending in a space \n> second line\n")
    assert text.splitlines() == ["> first line ending in a space", "> second line"]


def test_whitespace_only_lines_do_not_defeat_the_blank_line_collapse():
    # Layout tables flatten to lines holding a single space. They look blank,
    # so a run of them has to collapse like blank lines or the body arrives as
    # a column of holes.
    text = clean_plain_text("A\n \n \n \n \nB\n")
    assert text == "A\n\nB"


def test_zero_width_padding_is_removed():
    text = clean_plain_text("Preheader‌‌‌\n\nBody\n")
    assert text == "Preheader\n\nBody"


# -- HTML ---------------------------------------------------------------------


def test_table_cells_do_not_run_together():
    text = html_to_text(
        "<table><tr><td>Snehangshu Bhuin</td>"
        "<td>Name and profile picture</td></tr></table>"
    )
    assert "BhuinName" not in text
    assert "Snehangshu Bhuin" in text and "Name and profile picture" in text


def test_the_head_is_dropped_so_the_subject_is_not_repeated():
    text = html_to_text(
        "<html><head><title>Keep track of your data</title>"
        "<style>p{color:red}</style></head><body><p>Hello</p></body></html>"
    )
    assert text == "Hello"


def test_the_hidden_preheader_is_not_the_first_thing_you_read():
    text = html_to_text(
        '<div style="display:none;max-height:0;overflow:hidden">'
        "You recently signed in.</div><p>Real body</p>"
    )
    assert text == "Real body"


def test_source_line_breaks_do_not_break_the_sentence():
    text = html_to_text(
        "<td>You're receiving this email because you used\n"
        "   Sign in with Google to sign in\n   to Edmingle.</td>"
    )
    assert text == "You're receiving this email because you used Sign in with Google to sign in to Edmingle."


def test_links_survive_a_body_made_entirely_of_them():
    text = html_to_text(f'<p>Go <a href="{LONG_URL}">to your account</a></p>')
    assert "to your account [1]" in body(text)
    assert f"[1] {LONG_URL}" in text


def test_a_short_link_target_is_shown_beside_its_text():
    text = html_to_text(f'<p><a href="{SHORT_URL}">the docs</a></p>')
    assert text == f"the docs ({SHORT_URL})"


def test_a_link_is_not_dropped_for_naming_its_own_path():
    # `unsubscribe` appears in the URL's path; that is not the same thing as
    # the text being the URL.
    url = "https://myaccount.google.com/communication-preferences/unsubscribe/" + "g" * 90
    text = html_to_text(f'<p>you can <a href="{url}">unsubscribe</a>.</p>')
    assert "[1]" in body(text) and url in text


def test_a_link_whose_text_is_its_own_address_is_not_annotated():
    text = html_to_text('<a href="mailto:me@example.com">me@example.com</a>')
    assert text == "me@example.com"


def test_breaks_lists_and_rules_keep_their_shape():
    text = html_to_text("one<br>two<ul><li>first</li><li>second</li></ul>")
    assert "one\ntwo" in text
    assert "• first" in text and "• second" in text


def test_image_alt_text_is_kept_and_tracking_pixels_are_not():
    text = html_to_text(
        '<p><img src="logo.png" alt="Google"></p>'
        '<img src="p.gif" width="1" height="1" alt="">'
        '<p><img src="x.png" alt="hero_banner_final.png"></p>'
    )
    assert text == "Google"


def test_a_truncated_body_still_renders_what_parsed():
    text = html_to_text("<p>Half a message</p><div><span>and then")
    assert "Half a message" in text and "and then" in text


# -- choosing between them ----------------------------------------------------


def make(text: str | None, html: str | None) -> Message:
    return Message(id="m1", thread_id="t1", body_text=text, body_html=html)


def test_message_body_prefers_the_plain_part_and_cleans_it():
    msg = make("Hello there,  \nsecond half of the line.\n", "<p>Different</p>")
    assert message_body(msg) == "Hello there, second half of the line."


def test_message_body_falls_back_to_the_html_part():
    assert message_body(make(None, "<p>Only HTML</p>")) == "Only HTML"


def test_message_body_ignores_an_empty_plain_part():
    assert message_body(make("   \n\n", "<p>Only HTML</p>")) == "Only HTML"


def test_message_body_with_neither_part_is_none():
    assert message_body(make(None, None)) is None


def test_prefer_html_hands_back_the_source():
    msg = make("text", "<p>source</p>")
    assert message_body(msg, prefer_html=True) == "<p>source</p>"


@pytest.mark.parametrize("junk", ["", "   ", "<html></html>", "<p></p>"])
def test_an_empty_html_body_flattens_to_nothing(junk):
    assert html_to_text(junk) == ""


def test_a_flattened_plain_part_gives_way_to_the_html_it_came_from():
    msg = make(
        "<!--[if false]><!-->\n\nsign in to\n\nEdmingle\n\non 27 August.\n",
        "<p>sign in to <a href='#'>Edmingle</a> on 27 August.</p>",
    )
    assert message_body(msg) == "sign in to Edmingle on 27 August."


def test_but_only_when_the_html_yields_something():
    msg = make("<!--[if false]><!-->\n\nReal words\n", "<style>p{}</style>")
    assert message_body(msg) == "Real words"


# -- clickability -------------------------------------------------------------


def test_footnote_markers_carry_the_address_they_stand_for():
    body = clean_plain_text(f"Read <{LONG_URL}>the terms\n")
    targets = footnote_targets(body)
    assert targets == {"[1]": LONG_URL}

    line = linkify(Text("the terms [1] apply"), targets)
    assert [(s.start, s.end, str(s.style)) for s in line.spans] == [
        (10, 13, f"link {LONG_URL}")
    ]


def test_a_url_left_in_the_prose_is_linked_without_a_footnote_table():
    line = linkify(Text(f"see {SHORT_URL} for more"))
    assert [str(span.style) for span in line.spans] == [f"link {SHORT_URL}"]


def test_an_unknown_marker_is_left_alone():
    # `[2]` in a sender's own prose is not ours to turn into a link.
    line = linkify(Text("clause [2] of the contract"), {"[1]": SHORT_URL})
    assert line.spans == []

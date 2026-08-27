"""Attachment filename safety and download paths.

Filenames come from whoever sent the mail, so they are hostile input.
"""

from __future__ import annotations

import pytest

from gmcli.api.attachments import (
    resolve_output_path,
    sanitize_filename,
    unique_path,
)


@pytest.mark.parametrize(
    "hostile",
    [
        "../../.bashrc",
        "../../../etc/passwd",
        "/etc/passwd",
        "..\\..\\windows\\system32\\config",
        "....//....//escape.txt",
    ],
)
def test_traversal_attempts_are_flattened(hostile):
    safe = sanitize_filename(hostile)
    assert "/" not in safe
    assert "\\" not in safe
    assert ".." not in safe
    assert not safe.startswith(".")


def test_traversal_file_lands_inside_the_target_dir(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target_dir = tmp_path / "downloads"
    target_dir.mkdir()

    resolved = resolve_output_path(target_dir, "../../.bashrc")

    assert resolved.parent == target_dir.resolve()
    assert outside not in resolved.parents


def test_control_characters_are_stripped():
    assert "\x00" not in sanitize_filename("bad\x00name.txt")
    assert "\n" not in sanitize_filename("two\nlines.txt")


def test_ordinary_filename_is_untouched():
    assert sanitize_filename("Q3 report (final).pdf") == "Q3 report (final).pdf"


def test_empty_name_falls_back():
    assert sanitize_filename("") == "attachment"
    assert sanitize_filename("...") == "attachment"


def test_windows_reserved_names_are_escaped():
    assert sanitize_filename("CON.txt").startswith("_")
    assert sanitize_filename("nul").startswith("_")


def test_absurdly_long_names_are_truncated_keeping_the_extension():
    name = "a" * 500 + ".pdf"
    safe = sanitize_filename(name)
    assert len(safe) <= 200
    assert safe.endswith(".pdf")


def test_collisions_get_a_suffix_rather_than_overwriting(tmp_path):
    (tmp_path / "report.pdf").write_text("first")

    second = unique_path(tmp_path, "report.pdf")
    assert second.name == "report (1).pdf"

    second.write_text("second")
    third = unique_path(tmp_path, "report.pdf")
    assert third.name == "report (2).pdf"

    # The original is intact.
    assert (tmp_path / "report.pdf").read_text() == "first"


def test_download_writes_bytes_and_reports_paths(tmp_path, client, fake_service):
    import base64

    from gmcli.api.attachments import download
    from gmcli.models import Attachment

    blob = b"PDF-CONTENT"
    fake_service.handlers["users.messages.attachments.get"] = {
        "data": base64.urlsafe_b64encode(blob).decode()
    }

    attachment = Attachment(
        message_id="m1",
        attachment_id="a1",
        filename="../evil.pdf",
        mime_type="application/pdf",
        size=len(blob),
        index=1,
    )
    written = download(client, [attachment], tmp_path)

    assert len(written) == 1
    _, path = written[0]
    assert path.read_bytes() == blob
    assert path.parent == tmp_path.resolve()
    assert ".." not in path.name

import base64

from tests.conftest import load_fixture

from src.infrastructure.gmail.parser import parse_gmail_message


def _html_only(message: dict, html: str) -> dict:
    """Replace the body with a single text/html part, as an HTML-only mail arrives."""
    message["payload"] = {
        "mimeType": "text/html",
        "headers": message["payload"]["headers"],
        "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
    }
    return message


def test_parse_extracts_expected_fields():
    doc = parse_gmail_message(load_fixture("gmail_message_sample.json"))

    assert doc.source == "gmail"
    assert doc.source_id == "msg_001"
    assert doc.subject == "Damaged order #4521"
    assert doc.sender == "Jordan Lee"
    assert doc.sender_email == "jordan@example.com"
    assert "order #4521 arrived damaged" in doc.body_text
    assert doc.metadata["thread_id"] == "thread_001"
    # Set by the sending server, so the same value appears in every recipient's mailbox.
    assert doc.metadata["message_id"] == "<CADnq5_abc123@mail.example.com>"
    assert doc.attachments == []


def test_parse_prefers_plain_text_over_html():
    message = load_fixture("gmail_message_sample.json")
    message["payload"] = {
        "mimeType": "multipart/alternative",
        "headers": message["payload"]["headers"],
        "parts": [
            {"mimeType": "text/html", "body": {"data": "PGgxPkhUTUwgYm9keTwvaDE+"}},
            message["payload"],
        ],
    }

    doc = parse_gmail_message(message)

    assert "order #4521 arrived damaged" in doc.body_text
    assert "HTML body" not in doc.body_text


def test_parse_strips_tags_from_an_html_only_body():
    message = _html_only(
        load_fixture("gmail_message_sample.json"),
        "<html><body><p>The invoice <b>total</b> is wrong.</p></body></html>",
    )

    doc = parse_gmail_message(message)

    assert "The invoice total is wrong." in doc.body_text
    assert "<" not in doc.body_text


def test_parse_drops_style_and_script_from_an_html_only_body():
    """The classifier sees only the first characters of the body, so a <style> block long
    enough to fill that budget would leave it judging the mail on CSS alone."""
    message = _html_only(
        load_fixture("gmail_message_sample.json"),
        """<html><head><style>.x{color:red;padding:20px}</style></head>
           <body><script>track()</script><p>Payment failed.</p></body></html>""",
    )

    doc = parse_gmail_message(message)

    assert doc.body_text == "Payment failed."


def test_parse_drops_the_zero_width_padding_mail_templates_emit():
    """Templates pad the preview line with hundreds of invisible characters so the inbox
    preview shows no body text. They are not whitespace, so they survive that collapse."""
    message = _html_only(
        load_fixture("gmail_message_sample.json"),
        "<p>Invoice overdue.</p>" + "‌ ​" * 400 + "<p>Pay by Friday.</p>",
    )

    doc = parse_gmail_message(message)

    assert doc.body_text == "Invoice overdue. Pay by Friday."


def test_parse_keeps_words_either_side_of_a_tag_apart():
    message = _html_only(
        load_fixture("gmail_message_sample.json"),
        "<p><b>Order</b>failed</p>",
    )

    doc = parse_gmail_message(message)

    assert "Orderfailed" not in doc.body_text
    assert "Order failed" == doc.body_text


def test_parse_collects_attachments_but_not_the_body_parts():
    message = load_fixture("gmail_message_sample.json")
    message["payload"] = {
        "mimeType": "multipart/mixed",
        "headers": message["payload"]["headers"],
        "parts": [
            message["payload"],
            {
                "mimeType": "application/pdf",
                "filename": "spec.pdf",
                "body": {"attachmentId": "att_1", "size": 2048},
            },
            # An inline image carries no filename, so it is not treated as an attachment.
            {
                "mimeType": "image/png",
                "filename": "",
                "body": {"attachmentId": "att_2", "size": 99},
            },
        ],
    }

    doc = parse_gmail_message(message)

    assert len(doc.attachments) == 1
    attachment = doc.attachments[0]
    assert attachment.filename == "spec.pdf"
    assert attachment.mime_type == "application/pdf"
    assert attachment.size_bytes == 2048
    assert attachment.remote_id == "att_1"
    # The body part has no attachmentId, so it never appears as a file.
    assert "order #4521 arrived damaged" in doc.body_text


def test_parse_survives_a_message_with_no_message_id_header():
    message = load_fixture("gmail_message_sample.json")
    message["payload"]["headers"] = [
        h for h in message["payload"]["headers"] if h["name"] != "Message-ID"
    ]

    assert parse_gmail_message(message).metadata["message_id"] == ""

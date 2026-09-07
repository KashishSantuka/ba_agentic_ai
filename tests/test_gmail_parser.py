from tests.conftest import load_fixture

from src.infrastructure.gmail.parser import parse_gmail_message


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

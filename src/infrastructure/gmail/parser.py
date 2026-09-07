"""Turns Gmail's API shape into the domain's RawDocument."""

import base64
from datetime import datetime
from email.utils import parseaddr

from dateutil import parser as dateutil_parser

from src.domain.entities import Attachment, RawDocument


def _header(headers: list[dict], name: str) -> str:
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


def _extract_text(payload: dict) -> str:
    """Walk the MIME tree, preferring text/plain and falling back to text/html."""
    plain_text: str | None = None
    html_text: str | None = None

    def walk(part: dict) -> None:
        nonlocal plain_text, html_text
        mime_type = part.get("mimeType", "")
        data = part.get("body", {}).get("data")

        if mime_type == "text/plain" and data and plain_text is None:
            plain_text = _decode(data)
        elif mime_type == "text/html" and data and html_text is None:
            html_text = _decode(data)

        for sub_part in part.get("parts", []):
            walk(sub_part)

    walk(payload)
    return plain_text if plain_text is not None else (html_text or "")


def _extract_attachments(payload: dict) -> list[Attachment]:
    """Collect every part Gmail marks as a file.

    A part is an attachment when it carries a filename and an attachmentId — inline
    images and the text/plain and text/html bodies have neither, so the body itself is
    never mistaken for a file.
    """
    attachments: list[Attachment] = []

    def walk(part: dict) -> None:
        body = part.get("body", {})
        filename = part.get("filename") or ""
        remote_id = body.get("attachmentId")

        if filename and remote_id:
            attachments.append(
                Attachment(
                    filename=filename,
                    mime_type=part.get("mimeType", "application/octet-stream"),
                    size_bytes=int(body.get("size", 0)),
                    remote_id=remote_id,
                )
            )

        for sub_part in part.get("parts", []):
            walk(sub_part)

    walk(payload)
    return attachments


def parse_gmail_message(message: dict) -> RawDocument:
    payload = message["payload"]
    headers = payload.get("headers", [])

    sender_name, sender_email = parseaddr(_header(headers, "From"))

    try:
        sent_at = dateutil_parser.parse(_header(headers, "Date"))
    except (ValueError, TypeError):
        # Gmail always supplies internalDate, so a malformed Date header is recoverable.
        sent_at = datetime.utcfromtimestamp(int(message["internalDate"]) / 1000)

    return RawDocument(
        source="gmail",
        source_id=message["id"],
        subject=_header(headers, "Subject"),
        sender=sender_name or sender_email,
        sender_email=sender_email,
        sent_at=sent_at,
        body_text=_extract_text(payload),
        attachments=_extract_attachments(payload),
        metadata={
            "thread_id": message.get("threadId"),
            "message_id": _header(headers, "Message-ID"),
            "label_ids": message.get("labelIds", []),
        },
    )

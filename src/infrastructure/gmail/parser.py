"""Turns Gmail's API shape into the domain's RawDocument."""

import base64
import re
from datetime import datetime
from email.utils import parseaddr

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from src.domain.entities import Attachment, RawDocument


def _header(headers: list[dict], name: str) -> str:
    for header in headers:
        if header["name"].lower() == name.lower():
            return header["value"]
    return ""


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")


# Zero-width space, non-joiner, joiner, word joiner, BOM and soft hyphen: characters a
# reader never sees, which mail templates emit in bulk.
_ZERO_WIDTH = re.compile(r"[​‌‍⁠﻿­]")


def _html_to_text(html: str) -> str:
    """Reduce an HTML body to the words a reader would actually see.

    Stored markup would otherwise reach the classifier as the body. That matters more
    than it looks: the classifier is given only the first SCOPE_BODY_CHAR_LIMIT
    characters, and a marketing email opens with a <style> block long enough to fill
    that budget on its own — the model would judge the mail having seen nothing but CSS.

    script, style and head are dropped rather than flattened for the same reason: their
    text is never shown to a reader, so it is noise competing for the same budget.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()

    text = soup.get_text(separator=" ")

    # Zero-width characters, stripped before the whitespace collapse below because they
    # are not whitespace and would survive it. Marketing mail pads its preview line with
    # hundreds of them so the inbox preview shows no body text; left in, they are
    # invisible to a reader and still consume the classifier's budget in full.
    text = _ZERO_WIDTH.sub("", text)
    text = text.replace(" ", " ")

    # A separator, or words either side of a tag boundary run together — "Order</b>failed"
    # would otherwise become "Orderfailed". Runs of whitespace then collapse to one
    # space, since HTML indentation is not meaningful and does compete for the budget.
    return re.sub(r"\s+", " ", text).strip()


def _extract_text(payload: dict) -> str:
    """Walk the MIME tree, preferring text/plain and falling back to stripped text/html."""
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

    if plain_text is not None:
        return plain_text
    return _html_to_text(html_text) if html_text else ""


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

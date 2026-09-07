"""Reading new mail from connected mailboxes and storing it."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from src.domain.entities import Attachment, EmailConnection, RawDocument, SyncBookmark
from src.domain.ports import (
    AttachmentRepository,
    AttachmentStore,
    BookmarkRepository,
    ConnectionRepository,
    DocumentRepository,
    MailboxReader,
)

logger = logging.getLogger(__name__)


class ReaderFactory(Protocol):
    def __call__(self, connection: EmailConnection) -> MailboxReader: ...


@dataclass
class MailboxSyncResult:
    mailbox_email: str
    new_messages: int = 0
    new_attachments: int = 0
    documents: list[RawDocument] = field(default_factory=list)


@dataclass
class SyncMailbox:
    bookmarks: BookmarkRepository
    documents: DocumentRepository
    attachments: AttachmentRepository
    attachment_store: AttachmentStore
    reader_factory: ReaderFactory
    default_lookback_days: int
    attachment_max_bytes: int

    def execute(self, connection: EmailConnection) -> MailboxSyncResult:
        bookmark = self.bookmarks.get(connection.id) or SyncBookmark(connection_id=connection.id)
        reader = self.reader_factory(connection)

        documents, next_cursor = reader.fetch_since(
            bookmark,
            self.default_lookback_days,
            # `documents` is its own dedup record: a message already stored is one we
            # have already handled, so the provider's window may overlap freely.
            should_skip=lambda message_id: self.documents.exists(connection.id, message_id),
        )

        result = MailboxSyncResult(mailbox_email=connection.mailbox_email)
        for document in documents:
            # Committed per message rather than in a batch at the end: an interrupted run
            # keeps everything it finished.
            document_id = self.documents.save(connection.id, document)
            result.new_messages += 1
            result.documents.append(document)

            for attachment in document.attachments:
                if self._store_attachment(reader, connection.id, document_id, document, attachment):
                    result.new_attachments += 1

        self.bookmarks.save(
            SyncBookmark(
                connection_id=connection.id,
                last_history_id=next_cursor,
                last_synced_at=datetime.now(timezone.utc),
            )
        )

        logger.info(
            "synced %s: %d new, %d attachment(s)",
            connection.mailbox_email,
            result.new_messages,
            result.new_attachments,
        )
        return result

    def _store_attachment(
        self,
        reader: MailboxReader,
        connection_id: int,
        document_id: int,
        document: RawDocument,
        attachment: Attachment,
    ) -> bool:
        """Fetch and store one file. Returns whether its bytes were written.

        A row is recorded either way, so a message whose file was too large is still
        known to have carried it.
        """
        if self.attachments.exists(document_id, attachment.remote_id):
            return False

        if attachment.size_bytes > self.attachment_max_bytes:
            logger.warning(
                "skipping %s on %s: %d bytes exceeds the limit",
                attachment.filename,
                document.source_id,
                attachment.size_bytes,
            )
            self.attachments.save(document_id, attachment, location=None)
            return False

        content = reader.fetch_attachment(document.source_id, attachment.remote_id)
        location = self.attachment_store.save(
            connection_id, document.source_id, attachment, content
        )
        self.attachments.save(document_id, attachment, location)
        return True


@dataclass
class SyncAllMailboxes:
    connections: ConnectionRepository
    sync_mailbox: SyncMailbox

    def execute(self) -> list[MailboxSyncResult]:
        return [self.sync_mailbox.execute(c) for c in self.connections.list_all()]

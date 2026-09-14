"""Composition root.

The single place that binds domain ports to concrete implementations. Both entry points
(the API and the CLI) build their use cases from here, so wiring lives in one place and
neither interface reaches into the other.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from src.application.use_cases.classify_documents import ClassifyPendingDocuments
from src.application.use_cases.connect_mailbox import (
    CompleteMailboxConnection,
    StartMailboxConnection,
)
from src.application.use_cases.sync_mailbox import SyncAllMailboxes, SyncMailbox
from src.domain.entities import EmailConnection
from src.domain.ports import ConnectionRepository, MailboxReader
from src.infrastructure.config import settings
from src.infrastructure.gmail.client import GmailClient
from src.infrastructure.gmail.oauth import GmailOAuthProvider, credentials_from_tokens
from src.infrastructure.gmail.reader import GmailMailboxReader
from src.infrastructure.persistence.repositories import (
    SqlAlchemyAttachmentRepository,
    SqlAlchemyBookmarkRepository,
    SqlAlchemyClassificationAuditRepository,
    SqlAlchemyConnectionRepository,
    SqlAlchemyDocumentRepository,
    SqlAlchemyOAuthStateStore,
    SqlAlchemyScopeQueueRepository,
)
from src.infrastructure.storage.files import FileAttachmentStore


def build_connection_repository(session: Session) -> ConnectionRepository:
    return SqlAlchemyConnectionRepository(session)


def build_start_connection(session: Session) -> StartMailboxConnection:
    return StartMailboxConnection(
        provider=GmailOAuthProvider(settings),
        states=SqlAlchemyOAuthStateStore(session),
    )


def build_complete_connection(session: Session) -> CompleteMailboxConnection:
    return CompleteMailboxConnection(
        provider=GmailOAuthProvider(settings),
        states=SqlAlchemyOAuthStateStore(session),
        connections=SqlAlchemyConnectionRepository(session),
    )


def build_sync_mailbox(session: Session) -> SyncMailbox:
    connections = SqlAlchemyConnectionRepository(session)
    provider = GmailOAuthProvider(settings)

    def reader_factory(connection: EmailConnection) -> MailboxReader:
        tokens = connection.tokens
        if tokens.is_expired() and tokens.refresh_token:
            tokens = provider.refresh(tokens)
            # Persisted straight away so a later failure in this sync does not leave a
            # stale token behind for the next run.
            connections.update_tokens(connection.id, tokens)

        return GmailMailboxReader(GmailClient(credentials_from_tokens(settings, tokens)))

    return SyncMailbox(
        bookmarks=SqlAlchemyBookmarkRepository(session),
        documents=SqlAlchemyDocumentRepository(session),
        attachments=SqlAlchemyAttachmentRepository(session),
        attachment_store=FileAttachmentStore(settings.attachment_dir),
        reader_factory=reader_factory,
        default_lookback_days=settings.gmail_sync_lookback_days,
        attachment_max_bytes=settings.attachment_max_bytes,
    )


def build_sync_all(session: Session) -> SyncAllMailboxes:
    return SyncAllMailboxes(
        connections=SqlAlchemyConnectionRepository(session),
        sync_mailbox=build_sync_mailbox(session),
    )


def build_classify_pending(session: Session) -> ClassifyPendingDocuments:
    if not settings.gemini_api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and add a key from "
            "https://aistudio.google.com/apikey"
        )

    # Imported here rather than at module scope so the API and the sync CLI still start
    # without the Gemini SDK installed or a key configured.
    from google import genai

    from src.infrastructure.gemini.classifier import GeminiDocumentClassifier

    return ClassifyPendingDocuments(
        queue=SqlAlchemyScopeQueueRepository(session),
        audit=SqlAlchemyClassificationAuditRepository(session),
        classifier=GeminiDocumentClassifier(
            client=genai.Client(api_key=settings.gemini_api_key),
            model=settings.gemini_model,
            body_char_limit=settings.scope_body_char_limit,
        ),
        batch_size=settings.scope_batch_size,
        stale_after=timedelta(minutes=settings.scope_stale_after_minutes),
    )


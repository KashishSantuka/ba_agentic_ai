"""SQLAlchemy implementations of the domain's repository ports.

Mapping between ORM rows and domain entities happens here and nowhere else, so the
domain never sees a database type and the database is free to change shape.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from src.domain.entities import (
    Attachment,
    ClaimedDocument,
    ClassificationAttempt,
    EmailConnection,
    OAuthTokens,
    RawDocument,
    ScopeStatus,
    SyncBookmark,
)
from src.domain.ports import (
    AttachmentRepository,
    BookmarkRepository,
    ClassificationAuditRepository,
    ConnectionRepository,
    DocumentRepository,
    OAuthStateStore,
    ScopeQueueRepository,
)
from src.infrastructure.persistence import orm

STATE_TTL = timedelta(minutes=10)


def _to_entity(row: orm.EmailConnection) -> EmailConnection:
    return EmailConnection(
        id=row.id,
        user_id=row.user_id,
        provider=row.provider,
        mailbox_email=row.mailbox_email,
        tokens=OAuthTokens(
            access_token=row.access_token,
            refresh_token=row.refresh_token,
            expires_at=row.expires_at,
            scope=row.scope,
        ),
        connected_at=row.connected_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyConnectionRepository(ConnectionRepository):
    def __init__(self, session: Session):
        self._session = session

    def upsert(self, connection: EmailConnection) -> EmailConnection:
        row = self._session.execute(
            select(orm.EmailConnection).where(
                orm.EmailConnection.provider == connection.provider,
                orm.EmailConnection.mailbox_email == connection.mailbox_email,
            )
        ).scalar_one_or_none()

        if row is None:
            row = orm.EmailConnection(
                provider=connection.provider, mailbox_email=connection.mailbox_email
            )
            self._session.add(row)

        row.user_id = connection.user_id
        row.access_token = connection.tokens.access_token
        # Google omits the refresh token when re-approving an existing grant; keeping the
        # stored one avoids turning a working connection into one that cannot refresh.
        if connection.tokens.refresh_token:
            row.refresh_token = connection.tokens.refresh_token
        row.expires_at = connection.tokens.expires_at
        row.scope = connection.tokens.scope

        self._session.commit()
        self._session.refresh(row)
        return _to_entity(row)

    def update_tokens(self, connection_id: int, tokens: OAuthTokens) -> None:
        row = self._session.get(orm.EmailConnection, connection_id)
        if row is None:
            return

        row.access_token = tokens.access_token
        row.expires_at = tokens.expires_at
        if tokens.refresh_token:
            row.refresh_token = tokens.refresh_token
        self._session.commit()

    def get(self, connection_id: int) -> EmailConnection | None:
        row = self._session.get(orm.EmailConnection, connection_id)
        return _to_entity(row) if row else None

    def list_all(self) -> list[EmailConnection]:
        rows = self._session.execute(select(orm.EmailConnection)).scalars()
        return [_to_entity(r) for r in rows]


class SqlAlchemyBookmarkRepository(BookmarkRepository):
    def __init__(self, session: Session):
        self._session = session

    def get(self, connection_id: int) -> SyncBookmark | None:
        row = self._session.execute(
            select(orm.IngestionState).where(orm.IngestionState.connection_id == connection_id)
        ).scalar_one_or_none()
        if row is None:
            return None

        return SyncBookmark(
            connection_id=row.connection_id,
            last_history_id=row.last_history_id,
            last_synced_at=row.last_synced_at,
        )

    def save(self, bookmark: SyncBookmark) -> None:
        row = self._session.execute(
            select(orm.IngestionState).where(
                orm.IngestionState.connection_id == bookmark.connection_id
            )
        ).scalar_one_or_none()

        if row is None:
            row = orm.IngestionState(connection_id=bookmark.connection_id)
            self._session.add(row)

        row.last_history_id = bookmark.last_history_id
        row.last_synced_at = bookmark.last_synced_at
        self._session.commit()


class SqlAlchemyOAuthStateStore(OAuthStateStore):
    def __init__(self, session: Session):
        self._session = session

    def issue(self, state: str) -> None:
        self._session.add(orm.OAuthState(state=state))
        self._session.commit()

    def consume(self, state: str) -> bool:
        row = self._session.execute(
            select(orm.OAuthState).where(orm.OAuthState.state == state)
        ).scalar_one_or_none()
        if row is None:
            return False

        fresh = datetime.now(timezone.utc) - row.created_at < STATE_TTL
        # Deleted either way — a state is single-use, so a replayed callback cannot succeed.
        self._session.delete(row)
        self._session.commit()
        return fresh

    def purge_expired(self) -> None:
        cutoff = datetime.now(timezone.utc) - STATE_TTL
        self._session.execute(delete(orm.OAuthState).where(orm.OAuthState.created_at < cutoff))
        self._session.commit()


class SqlAlchemyDocumentRepository(DocumentRepository):
    def __init__(self, session: Session):
        self._session = session

    def save(self, connection_id: int, document: RawDocument) -> int:
        # The provider's window overlaps what we already hold, so a message can be
        # offered twice. Returning the existing id keeps save idempotent rather than
        # tripping the (connection_id, source_id) unique constraint.
        row = self._row(connection_id, document.source_id)
        if row is not None:
            return row.id

        row = orm.Document(
            connection_id=connection_id,
            source=document.source,
            source_id=document.source_id,
            thread_id=document.metadata.get("thread_id"),
            message_id=document.metadata.get("message_id") or None,
            subject=document.subject,
            sender=document.sender,
            sender_email=document.sender_email,
            sent_at=document.sent_at,
            body_text=document.body_text,
        )
        self._session.add(row)
        self._session.commit()
        return row.id

    def exists(self, connection_id: int, source_id: str) -> bool:
        return self._row(connection_id, source_id) is not None

    def _row(self, connection_id: int, source_id: str) -> orm.Document | None:
        return self._session.execute(
            select(orm.Document).where(
                orm.Document.connection_id == connection_id,
                orm.Document.source_id == source_id,
            )
        ).scalar_one_or_none()


_CLAIMED_COLUMNS = (
    orm.Document.id,
    orm.Document.connection_id,
    orm.Document.source,
    orm.Document.source_id,
    orm.Document.subject,
    orm.Document.sender,
    orm.Document.sender_email,
    orm.Document.sent_at,
    orm.Document.body_text,
)


def _to_claimed(row) -> ClaimedDocument:
    return ClaimedDocument(
        id=row.id,
        connection_id=row.connection_id,
        document=RawDocument(
            source=row.source,
            source_id=row.source_id,
            subject=row.subject,
            sender=row.sender,
            sender_email=row.sender_email,
            sent_at=row.sent_at,
            body_text=row.body_text,
        ),
    )


class SqlAlchemyScopeQueueRepository(ScopeQueueRepository):
    def __init__(self, session: Session):
        self._session = session

    def claim_pending(self, limit: int) -> list[ClaimedDocument]:
        # Selecting and updating in one statement is what makes the claim safe. Reading
        # the pending rows and writing them back separately would leave a gap in which a
        # second worker reads the same rows, and both would classify them.
        #
        # SKIP LOCKED covers only the instant this statement runs; the move to
        # PROCESSING is what keeps other workers off these rows for the minutes of model
        # calls that follow, once this transaction has committed and no lock is held.
        pending = (
            select(orm.Document.id)
            .where(orm.Document.scope_status == ScopeStatus.PENDING)
            .order_by(orm.Document.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        return self._take(orm.Document.id.in_(pending), ScopeStatus.PROCESSING)

    def reclaim_stale(self, older_than: timedelta, limit: int) -> list[ClaimedDocument]:
        # Taken over rather than released: these stay PROCESSING with the clock reset, so
        # they belong to this worker while it records the lost attempt. Putting them
        # straight back to PENDING would let another worker claim one and write the same
        # attempt number this one is about to write.
        cutoff = datetime.now(timezone.utc) - older_than
        stale = (
            select(orm.Document.id)
            .where(
                orm.Document.scope_status == ScopeStatus.PROCESSING,
                orm.Document.scope_updated_at < cutoff,
            )
            .order_by(orm.Document.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        return self._take(orm.Document.id.in_(stale), ScopeStatus.PROCESSING)

    def mark(self, document_id: int, status: str) -> None:
        self._session.execute(
            update(orm.Document)
            .where(orm.Document.id == document_id)
            .values(scope_status=status, scope_updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        self._session.commit()

    def _take(self, condition, status: str) -> list[ClaimedDocument]:
        rows = self._session.execute(
            update(orm.Document)
            .where(condition)
            .values(scope_status=status, scope_updated_at=func.now())
            .returning(*_CLAIMED_COLUMNS)
            .execution_options(synchronize_session=False)
        ).all()

        # Committed before the caller starts work: the claim must be visible to other
        # workers immediately, and no database transaction should stay open across the
        # model calls that follow.
        self._session.commit()
        return [_to_claimed(row) for row in rows]


class SqlAlchemyClassificationAuditRepository(ClassificationAuditRepository):
    def __init__(self, session: Session):
        self._session = session

    def append(self, attempt: ClassificationAttempt) -> None:
        self._session.add(
            orm.ClassificationAudit(
                document_id=attempt.document_id,
                connection_id=attempt.connection_id,
                stage=attempt.stage,
                attempt=attempt.attempt,
                status=attempt.status,
                classification=attempt.result.label if attempt.result else None,
                confidence=attempt.result.confidence if attempt.result else None,
                reason=attempt.result.reason if attempt.result else attempt.error,
                model_version=attempt.model_version,
                prompt_version=attempt.prompt_version,
            )
        )
        self._session.commit()

    def last_attempt(self, document_id: int, stage: str) -> int:
        highest = self._session.execute(
            select(func.max(orm.ClassificationAudit.attempt)).where(
                orm.ClassificationAudit.document_id == document_id,
                orm.ClassificationAudit.stage == stage,
            )
        ).scalar_one()
        return highest or 0


class SqlAlchemyAttachmentRepository(AttachmentRepository):
    def __init__(self, session: Session):
        self._session = session

    def save(self, document_id: int, attachment: Attachment, location: str | None) -> None:
        if self.exists(document_id, attachment.remote_id):
            return

        self._session.add(
            orm.Attachment(
                document_id=document_id,
                remote_id=attachment.remote_id,
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
                location=location,
            )
        )
        self._session.commit()

    def exists(self, document_id: int, remote_id: str) -> bool:
        return (
            self._session.execute(
                select(orm.Attachment).where(
                    orm.Attachment.document_id == document_id,
                    orm.Attachment.remote_id == remote_id,
                )
            ).scalar_one_or_none()
            is not None
        )

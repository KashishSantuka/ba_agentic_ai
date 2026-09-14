from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class EmailConnection(Base):
    """One row per connected mailbox. Replaces the old credentials/token.json file —
    a server serves many mailboxes, which a single file cannot represent.

    user_id is intentionally nullable: there is no signup/login yet, but the column is
    here so adding a users table later needs no migration of this table.
    """

    __tablename__ = "email_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(default=None, index=True)

    provider: Mapped[str]                       # "gmail"
    mailbox_email: Mapped[str] = mapped_column(index=True)

    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    scope: Mapped[str] = mapped_column(Text)

    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("provider", "mailbox_email"),)

    ingestion_state: Mapped["IngestionState | None"] = relationship(
        back_populates="connection", cascade="all, delete-orphan", uselist=False
    )


class OAuthState(Base):
    """Short-lived record of an in-flight OAuth attempt.

    Google echoes this value back to the callback; matching it against a row we created
    proves the callback belongs to a flow we actually started (CSRF protection).
    """

    __tablename__ = "oauth_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestionState(Base):
    """The sync 'bookmark' — one per connected mailbox, not one globally, so mailboxes
    never overwrite each other's position."""

    __tablename__ = "ingestion_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("email_connections.id", ondelete="CASCADE"), unique=True
    )
    last_history_id: Mapped[str | None] = mapped_column(default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    connection: Mapped[EmailConnection] = relationship(back_populates="ingestion_state")


class Document(Base):
    """The raw record of every message fetched from a mailbox.

    The message itself — subject, sender, sent_at, body_text and its attachments — is
    written once and never updated, so the table stays a faithful copy of what arrived.
    The unique constraint below also makes it the record of what has already been
    handled: a message stored here is never fetched again.

    scope_status is the deliberate exception. It is ours rather than the sender's:
    processing state that records how far this message has got through classification,
    and it is expected to change. Nothing that came from the mailbox changes with it.
    """

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("email_connections.id", ondelete="CASCADE"), index=True
    )

    source: Mapped[str]
    source_id: Mapped[str]
    thread_id: Mapped[str | None] = mapped_column(default=None, index=True)

    # The RFC 5322 Message-ID, set by the sending server and identical in every
    # recipient's mailbox — unlike source_id, which only means anything within one
    # account. Nullable because a malformed message may carry no such header.
    message_id: Mapped[str | None] = mapped_column(Text, default=None, index=True)

    subject: Mapped[str] = mapped_column(Text)
    sender: Mapped[str] = mapped_column(Text)
    sender_email: Mapped[str] = mapped_column(index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    body_text: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # pending -> processing -> completed | review. Set by the classifier, never by the
    # mailbox; see the note in the class docstring about why this one column may change.
    scope_status: Mapped[str] = mapped_column(default="pending", server_default="pending")

    # When scope_status last moved. Without it there is no way to tell a document a
    # worker is busy with from one whose worker died holding it.
    scope_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        UniqueConstraint("connection_id", "source_id"),
        # Partial: the classifier only ever searches for pending work, and indexing the
        # completed rows too would grow the index with every document forever.
        Index(
            "ix_documents_pending",
            "scope_status",
            postgresql_where=text("scope_status = 'pending'"),
        ),
    )


class ClassificationAudit(Base):
    """One row per classification attempt, appended and never updated.

    Kept separate from the document's own scope_status because that column holds only
    where a document is now; this holds how it got there. A run months from now that
    classifies differently is explainable only if the model and prompt behind each past
    answer were recorded at the time, so both are stored per attempt rather than assumed.

    Failures are recorded as fully as successes: an attempt that produced no answer still
    used up one of the document's three, and a row that is never written is a document
    that retries forever.
    """

    __tablename__ = "classification_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("email_connections.id", ondelete="CASCADE"), index=True
    )

    # Which classifier ran. Documents pass through more than one stage, so the attempt
    # number below counts within a stage rather than across the document's whole life.
    stage: Mapped[str]
    attempt: Mapped[int]

    status: Mapped[str]                          # success | failed

    # Null on a failed attempt — there was no answer to record.
    classification: Mapped[str | None] = mapped_column(default=None)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)

    # The model's justification on success, the error on failure. One column because both
    # answer the same question when reading the log: why did this attempt end this way?
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    model_version: Mapped[str]
    prompt_version: Mapped[str]

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # A retry that fires twice cannot write two rows both claiming to be attempt 2 — the
    # second fails loudly instead of quietly corrupting the count that decides retries.
    __table_args__ = (UniqueConstraint("document_id", "stage", "attempt"),)


class Attachment(Base):
    """One file carried by a message. The bytes live outside the database; this row
    records what was carried and where it was put, so a message's attachments are known
    even when the file itself was skipped."""

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    remote_id: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger)

    # Null when the file was not written — too large, or the download failed. The row is
    # kept either way so the message's contents are still described accurately.
    location: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("document_id", "remote_id"),)

"""Ports — the interfaces the domain depends on.

The domain declares what it needs; infrastructure supplies the implementations. This is
what keeps business rules independent of Postgres, Google, or FastAPI, and what lets a
second mail provider be added without touching anything above this line.
"""

from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Callable, Iterator

from src.domain.entities import (
    Attachment,
    ClaimedDocument,
    Classification,
    ClassificationAttempt,
    EmailConnection,
    OAuthTokens,
    RawDocument,
    SyncBookmark,
)


class ConnectionRepository(ABC):
    @abstractmethod
    def upsert(self, connection: EmailConnection) -> EmailConnection: ...

    @abstractmethod
    def update_tokens(self, connection_id: int, tokens: OAuthTokens) -> None: ...

    @abstractmethod
    def get(self, connection_id: int) -> EmailConnection | None: ...

    @abstractmethod
    def list_all(self) -> list[EmailConnection]: ...


class BookmarkRepository(ABC):
    @abstractmethod
    def get(self, connection_id: int) -> SyncBookmark | None: ...

    @abstractmethod
    def save(self, bookmark: SyncBookmark) -> None: ...


class OAuthStateStore(ABC):
    """Tracks in-flight authorisation attempts so a callback can be proven to belong to
    a flow we actually started."""

    @abstractmethod
    def issue(self, state: str) -> None: ...

    @abstractmethod
    def consume(self, state: str) -> bool: ...

    @abstractmethod
    def purge_expired(self) -> None: ...


class OAuthProvider(ABC):
    """A provider we can obtain mailbox access from."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def authorization_url(self, state: str, login_hint: str | None = None) -> str: ...

    @abstractmethod
    def exchange_code(self, code: str) -> tuple[OAuthTokens, str]:
        """Trade the callback code for tokens. Returns (tokens, mailbox_email).

        The mailbox is reported by the provider rather than chosen by us — only the
        account holder can decide which mailbox they granted.
        """

    @abstractmethod
    def refresh(self, tokens: OAuthTokens) -> OAuthTokens: ...


class MailboxReader(ABC):
    """Reads messages from one authorised mailbox."""

    @abstractmethod
    def fetch_since(
        self,
        bookmark: SyncBookmark,
        default_lookback_days: int,
        should_skip: Callable[[str], bool] | None = None,
    ) -> tuple[Iterator[RawDocument], str]:
        """Yield messages newer than the bookmark, plus the cursor to store next.

        `should_skip` is consulted with a message id before its body is downloaded, so
        already-processed messages cost no additional provider calls on a re-run.
        """

    @abstractmethod
    def fetch_attachment(self, source_id: str, remote_id: str) -> bytes:
        """Download one attachment's bytes. Separate from `fetch_since` because providers
        charge a call per attachment, which only a caller that wants the file should pay."""


class DocumentClassifier(ABC):
    """Decides what a document is, so only what matters is stored and analysed."""

    @property
    @abstractmethod
    def model_version(self) -> str:
        """The exact model answering, never a floating alias — an alias silently
        repoints and the audit log would then credit a model that never ran."""

    @property
    @abstractmethod
    def prompt_version(self) -> str:
        """Which revision of the instructions produced the answer, so a later shift in
        the relevant/non_relevant ratio can be attributed to a prompt change."""

    @abstractmethod
    def classify(self, document: RawDocument) -> Classification:
        """Raise ClassificationFailed if no usable answer came back. Returning a guess
        would record a decision nobody made."""


class ScopeQueueRepository(ABC):
    """Hands out documents awaiting classification, one batch at a time, and records
    where each one ended up."""

    @abstractmethod
    def claim_pending(self, limit: int) -> list[ClaimedDocument]:
        """Take up to `limit` pending documents and mark them in progress, atomically.

        Runs may overlap — a batch of slow model calls can outlast the interval that
        started it — so claiming has to be the thing that excludes a second worker,
        rather than the caller checking first and writing afterwards.
        """

    @abstractmethod
    def mark(self, document_id: int, status: str) -> None: ...

    @abstractmethod
    def reclaim_stale(self, older_than: timedelta, limit: int) -> list[ClaimedDocument]:
        """Take over documents left in progress by a worker that died, so they are
        retried instead of stranded. The caller records the lost attempt.

        Bounded like a claim: repeated crashes accumulate stale documents, and an
        unbounded sweep would make the next run's length depend on how bad the last
        outage was.
        """


class ClassificationAuditRepository(ABC):
    """The append-only record of every classification attempt."""

    @abstractmethod
    def append(self, attempt: ClassificationAttempt) -> None: ...

    @abstractmethod
    def last_attempt(self, document_id: int, stage: str) -> int:
        """The highest attempt number recorded for this document at this stage, or 0."""


class DocumentRepository(ABC):
    """Stores the raw content of every message fetched, and doubles as the record of
    which messages a mailbox has already handled."""

    @abstractmethod
    def save(self, connection_id: int, document: RawDocument) -> int:
        """Store the body and return its row id. Idempotent: a message already stored is
        left untouched and its existing id returned, so an overlapping provider window
        does not collide with a row it already wrote."""

    @abstractmethod
    def exists(self, connection_id: int, source_id: str) -> bool: ...


class AttachmentStore(ABC):
    """Holds attachment bytes outside the database, which keeps rows small and lets the
    backing store change without a schema migration."""

    @abstractmethod
    def save(self, connection_id: int, source_id: str, attachment: Attachment, content: bytes) -> str:
        """Write the bytes and return a locator the repository can store."""


class AttachmentRepository(ABC):
    """Records what each message carried, and where those bytes were put."""

    @abstractmethod
    def save(self, document_id: int, attachment: Attachment, location: str | None) -> None:
        """Idempotent per (document, remote_id). `location` is None when the file was
        skipped, so the row still records that the message carried it."""

    @abstractmethod
    def exists(self, document_id: int, remote_id: str) -> bool: ...

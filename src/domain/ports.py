"""Ports — the interfaces the domain depends on.

The domain declares what it needs; infrastructure supplies the implementations. This is
what keeps business rules independent of Postgres, Google, or FastAPI, and what lets a
second mail provider be added without touching anything above this line.
"""

from abc import ABC, abstractmethod
from typing import Callable, Iterator

from src.domain.entities import (
    Attachment,
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

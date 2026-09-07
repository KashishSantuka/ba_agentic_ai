"""Pure business objects.

Nothing in this module imports a framework, a database, or an HTTP client. These types
describe what the business deals in, independent of how any of it is stored or fetched.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Attachment:
    """A file carried by a message, described but not yet downloaded.

    `remote_id` is the provider's handle for the bytes: Gmail returns attachment metadata
    inline with the message but the content only on a second, separate call, so fetching
    stays the caller's decision rather than a cost every message pays.
    """

    filename: str
    mime_type: str
    size_bytes: int
    remote_id: str


@dataclass
class RawDocument:
    """A message pulled from any mailbox, normalised to a source-agnostic shape.

    Gmail, Outlook and IMAP connectors all produce this. Everything downstream works
    against it, so adding a provider never touches downstream code.
    """

    source: str
    source_id: str
    subject: str
    sender: str
    sender_email: str
    sent_at: datetime
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class OAuthTokens:
    """Credentials issued by a provider for one mailbox."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(timezone.utc)) >= self.expires_at


@dataclass
class EmailConnection:
    """A mailbox someone has authorised us to read.

    user_id is nullable on purpose: there is no signup/login yet, but connections are
    already shaped to belong to a user so adding one later changes nothing here.
    """

    provider: str
    mailbox_email: str
    tokens: OAuthTokens
    id: int | None = None
    user_id: int | None = None
    connected_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class SyncBookmark:
    """How far through a mailbox we have already read.

    Keeps syncing incremental and repeatable: a crash or restart resumes here rather
    than re-reading, and never silently skips messages.
    """

    connection_id: int
    last_history_id: str | None = None
    last_synced_at: datetime | None = None

    def lookback_days(self, default_days: int, now: datetime | None = None) -> int:
        """How far back to search when the provider's cursor is unusable.

        Uses the real elapsed gap when we have synced before — asking for a fixed window
        would either miss messages after a long pause or re-scan far more than necessary.
        """
        if self.last_synced_at is None:
            return default_days

        elapsed = (now or datetime.now(timezone.utc)) - self.last_synced_at
        return max(elapsed.days, 1)

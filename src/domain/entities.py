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


class ScopeStatus:
    """How far a document has got through scope classification.

    pending -> processing -> completed | review. `processing` is what keeps two
    overlapping runs off the same document once the claiming transaction has committed
    and no database lock is held any more. `review` is terminal: the attempts ran out
    and a person needs to look.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    REVIEW = "review"


class ClassificationLabel:
    """The only three answers the classifier may give. Anything else is a failed attempt
    rather than a result, so a model that invents a label cannot poison the audit log."""

    RELEVANT = "relevant"
    NON_RELEVANT = "non_relevant"
    JUNK = "junk"

    ALL = frozenset({RELEVANT, NON_RELEVANT, JUNK})


class AttemptStatus:
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class Classification:
    """What the classifier decided about one document — nothing more.

    Deliberately free of model and prompt identifiers: this is the answer, not the
    circumstances it was produced under. Those live on the attempt, which needs them even
    when no answer came back at all.
    """

    label: str
    confidence: float
    reason: str


@dataclass
class ClassificationAttempt:
    """One try at classifying one document, successful or not.

    Written once per attempt and never updated, which is what makes the log a truthful
    history rather than a current-state table. `attempt` is stored rather than counted
    because the same document later passes through further stages, and counting rows
    would mix those in.
    """

    document_id: int
    connection_id: int
    stage: str
    attempt: int
    status: str
    model_version: str
    prompt_version: str
    result: Classification | None = None
    error: str | None = None
    created_at: datetime | None = None


@dataclass
class ClaimedDocument:
    """A document handed to the classifier by the queue, with the row id needed to record
    the outcome against it."""

    id: int
    connection_id: int
    document: RawDocument


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

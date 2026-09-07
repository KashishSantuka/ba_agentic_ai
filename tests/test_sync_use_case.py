"""Use-case tests. Every port is faked, so these run with no database and no network."""

from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from src.application.use_cases.sync_mailbox import SyncMailbox
from src.domain.entities import (
    Attachment,
    EmailConnection,
    OAuthTokens,
    RawDocument,
    SyncBookmark,
)
from src.domain.ports import (
    AttachmentRepository,
    AttachmentStore,
    BookmarkRepository,
    DocumentRepository,
    MailboxReader,
)


class InMemoryBookmarks(BookmarkRepository):
    def __init__(self, bookmark: SyncBookmark | None = None):
        self._bookmark = bookmark

    def get(self, connection_id: int) -> SyncBookmark | None:
        return self._bookmark

    def save(self, bookmark: SyncBookmark) -> None:
        self._bookmark = bookmark


class InMemoryDocuments(DocumentRepository):
    def __init__(self, already_stored: set[str] | None = None):
        self.saved: dict[str, RawDocument] = {}
        self._already_stored = already_stored or set()

    def save(self, connection_id: int, document: RawDocument) -> int:
        # Idempotent, as the port requires: a second save leaves the stored body alone.
        self.saved.setdefault(document.source_id, document)
        return list(self.saved).index(document.source_id) + 1

    def exists(self, connection_id: int, source_id: str) -> bool:
        return source_id in self.saved or source_id in self._already_stored


class InMemoryAttachments(AttachmentRepository):
    def __init__(self):
        self.rows: dict[tuple[int, str], str | None] = {}

    def save(self, document_id: int, attachment: Attachment, location: str | None) -> None:
        self.rows.setdefault((document_id, attachment.remote_id), location)

    def exists(self, document_id: int, remote_id: str) -> bool:
        return (document_id, remote_id) in self.rows


class InMemoryAttachmentStore(AttachmentStore):
    def __init__(self):
        self.written: dict[str, bytes] = {}

    def save(self, connection_id, source_id, attachment, content: bytes) -> str:
        location = f"{connection_id}/{source_id}/{attachment.filename}"
        self.written[location] = content
        return location


class FakeReader(MailboxReader):
    """Records how it was called, so the tests can assert on what was asked of Gmail."""

    def __init__(self, documents: list[RawDocument], cursor: str = "cursor_new"):
        self._documents = documents
        self._cursor = cursor
        self.lookback_days_used: int | None = None
        self.history_id_used: str | None = None
        self.attachments_fetched: list[tuple[str, str]] = []

    def fetch_since(
        self,
        bookmark: SyncBookmark,
        default_lookback_days: int,
        should_skip: Callable[[str], bool] | None = None,
    ) -> tuple[Iterator[RawDocument], str]:
        self.history_id_used = bookmark.last_history_id
        if not bookmark.last_history_id:
            self.lookback_days_used = bookmark.lookback_days(default_lookback_days)

        def documents() -> Iterator[RawDocument]:
            for document in self._documents:
                if should_skip is not None and should_skip(document.source_id):
                    continue
                yield document

        return documents(), self._cursor

    def fetch_attachment(self, source_id: str, remote_id: str) -> bytes:
        self.attachments_fetched.append((source_id, remote_id))
        return b"file-bytes"


def _connection(connection_id: int = 1) -> EmailConnection:
    return EmailConnection(
        id=connection_id,
        provider="gmail",
        mailbox_email="a@example.com",
        tokens=OAuthTokens(
            access_token="t",
            refresh_token="r",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            scope="readonly",
        ),
    )


def _document(source_id: str, attachments: list[Attachment] | None = None) -> RawDocument:
    return RawDocument(
        source="gmail",
        source_id=source_id,
        subject=f"subject {source_id}",
        sender="Priya Nair <priya@acme.com>",
        sender_email="priya@acme.com",
        sent_at=datetime.now(timezone.utc),
        body_text="body",
        attachments=attachments or [],
    )


def _attachment(name="spec.pdf", size=1024, remote_id="att_1") -> Attachment:
    return Attachment(
        filename=name, mime_type="application/pdf", size_bytes=size, remote_id=remote_id
    )


def _use_case(
    reader: FakeReader,
    bookmarks,
    documents=None,
    attachments=None,
    attachment_store=None,
    attachment_max_bytes=25 * 1024 * 1024,
) -> SyncMailbox:
    return SyncMailbox(
        bookmarks=bookmarks,
        documents=documents or InMemoryDocuments(),
        attachments=attachments or InMemoryAttachments(),
        attachment_store=attachment_store or InMemoryAttachmentStore(),
        reader_factory=lambda _connection: reader,
        default_lookback_days=30,
        attachment_max_bytes=attachment_max_bytes,
    )


def test_every_fetched_message_is_stored():
    reader = FakeReader([_document("m1"), _document("m2")])
    documents = InMemoryDocuments()

    result = _use_case(reader, InMemoryBookmarks(), documents).execute(_connection())

    assert result.new_messages == 2
    assert set(documents.saved) == {"m1", "m2"}
    assert [d.source_id for d in result.documents] == ["m1", "m2"]


def test_a_stored_message_is_skipped_before_its_body_is_downloaded():
    reader = FakeReader([_document("old"), _document("new")])
    documents = InMemoryDocuments(already_stored={"old"})

    result = _use_case(reader, InMemoryBookmarks(), documents).execute(_connection())

    # The overlap costs one listing call and nothing else: `old` is never fetched.
    assert result.new_messages == 1
    assert set(documents.saved) == {"new"}


def test_a_second_run_over_the_same_window_stores_nothing_new():
    documents = InMemoryDocuments()
    bookmarks = InMemoryBookmarks()

    _use_case(FakeReader([_document("m1")]), bookmarks, documents).execute(_connection())
    second = _use_case(FakeReader([_document("m1")]), bookmarks, documents).execute(_connection())

    assert second.new_messages == 0
    assert set(documents.saved) == {"m1"}


def test_the_bookmark_advances_to_the_cursor_the_provider_returned():
    reader = FakeReader([_document("m1")], cursor="cursor_99")
    bookmarks = InMemoryBookmarks()

    _use_case(reader, bookmarks).execute(_connection())

    assert bookmarks.get(1).last_history_id == "cursor_99"


def test_the_bookmark_advances_even_when_nothing_new_arrived():
    reader = FakeReader([], cursor="cursor_99")
    bookmarks = InMemoryBookmarks()

    result = _use_case(reader, bookmarks).execute(_connection())

    assert result.new_messages == 0
    assert bookmarks.get(1).last_history_id == "cursor_99"


def test_a_first_run_falls_back_to_the_default_lookback_window():
    reader = FakeReader([])

    _use_case(reader, InMemoryBookmarks()).execute(_connection())

    assert reader.history_id_used is None
    assert reader.lookback_days_used == 30


def test_an_existing_bookmark_is_used_instead_of_a_date_window():
    reader = FakeReader([])
    bookmarks = InMemoryBookmarks(SyncBookmark(1, "h_42", datetime.now(timezone.utc)))

    _use_case(reader, bookmarks).execute(_connection())

    assert reader.history_id_used == "h_42"
    assert reader.lookback_days_used is None


def test_a_long_gap_widens_the_window_to_cover_it():
    reader = FakeReader([])
    bookmarks = InMemoryBookmarks(
        SyncBookmark(1, None, datetime.now(timezone.utc) - timedelta(days=9))
    )

    _use_case(reader, bookmarks).execute(_connection())

    assert reader.lookback_days_used == 9


def test_an_attachment_is_downloaded_and_stored():
    reader = FakeReader([_document("m1", [_attachment()])])
    attachments, store = InMemoryAttachments(), InMemoryAttachmentStore()

    result = _use_case(
        reader, InMemoryBookmarks(), attachments=attachments, attachment_store=store
    ).execute(_connection())

    assert result.new_attachments == 1
    assert reader.attachments_fetched == [("m1", "att_1")]
    assert list(store.written.values()) == [b"file-bytes"]
    assert list(attachments.rows.values()) == ["1/m1/spec.pdf"]


def test_a_message_with_no_attachments_costs_no_extra_calls():
    reader = FakeReader([_document("m1")])

    result = _use_case(reader, InMemoryBookmarks()).execute(_connection())

    assert result.new_attachments == 0
    assert reader.attachments_fetched == []


def test_an_oversized_attachment_is_recorded_but_not_downloaded():
    reader = FakeReader([_document("m1", [_attachment(size=50_000_000)])])
    attachments, store = InMemoryAttachments(), InMemoryAttachmentStore()

    result = _use_case(
        reader,
        InMemoryBookmarks(),
        attachments=attachments,
        attachment_store=store,
        attachment_max_bytes=1_000_000,
    ).execute(_connection())

    assert result.new_attachments == 0
    # Never fetched, so the bytes cost nothing — but the message is still known to
    # have carried the file.
    assert reader.attachments_fetched == []
    assert store.written == {}
    assert attachments.rows == {(1, "att_1"): None}


def test_two_files_of_the_same_name_are_both_stored():
    document = _document(
        "m1",
        [_attachment(remote_id="att_1"), _attachment(remote_id="att_2")],
    )
    attachments = InMemoryAttachments()

    result = _use_case(
        FakeReader([document]), InMemoryBookmarks(), attachments=attachments
    ).execute(_connection())

    # Keyed by the provider's id, not the filename, so a repeated name is not a duplicate.
    assert result.new_attachments == 2
    assert set(attachments.rows) == {(1, "att_1"), (1, "att_2")}

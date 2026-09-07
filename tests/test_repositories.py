from datetime import datetime, timezone

from sqlalchemy import select

from src.domain.entities import (
    Attachment,
    EmailConnection,
    OAuthTokens,
    RawDocument,
    SyncBookmark,
)
from src.infrastructure.persistence import orm
from src.infrastructure.persistence.repositories import (
    SqlAlchemyAttachmentRepository,
    SqlAlchemyBookmarkRepository,
    SqlAlchemyConnectionRepository,
    SqlAlchemyDocumentRepository,
    SqlAlchemyOAuthStateStore,
)


def _connection(mailbox_email="a@example.com", refresh_token="refresh_1") -> EmailConnection:
    return EmailConnection(
        provider="gmail",
        mailbox_email=mailbox_email,
        tokens=OAuthTokens(
            access_token="token_1",
            refresh_token=refresh_token,
            expires_at=datetime.now(timezone.utc),
            scope="https://www.googleapis.com/auth/gmail.readonly",
        ),
    )


def test_upsert_updates_existing_connection_instead_of_duplicating(db_session):
    repo = SqlAlchemyConnectionRepository(db_session)

    first = repo.upsert(_connection())
    second = repo.upsert(_connection(refresh_token=None))

    assert first.id == second.id
    assert len(repo.list_all()) == 1
    # Google omits the refresh token when re-approving; discarding the stored one would
    # leave a connection that can never refresh again.
    assert second.tokens.refresh_token == "refresh_1"


def test_bookmark_round_trips_and_updates(db_session):
    repo = SqlAlchemyConnectionRepository(db_session)
    bookmarks = SqlAlchemyBookmarkRepository(db_session)
    connection = repo.upsert(_connection())

    assert bookmarks.get(connection.id) is None

    now = datetime.now(timezone.utc)
    bookmarks.save(SyncBookmark(connection.id, last_history_id="h1", last_synced_at=now))
    assert bookmarks.get(connection.id).last_history_id == "h1"

    bookmarks.save(SyncBookmark(connection.id, last_history_id="h2", last_synced_at=now))
    assert bookmarks.get(connection.id).last_history_id == "h2"


def test_oauth_state_is_single_use(db_session):
    states = SqlAlchemyOAuthStateStore(db_session)
    states.issue("abc123")

    assert states.consume("abc123") is True
    # A replayed callback must not succeed.
    assert states.consume("abc123") is False
    assert states.consume("never-issued") is False


def _document(source_id="msg_001", body="first body", message_id="<abc@mail>") -> RawDocument:
    return RawDocument(
        source="gmail",
        source_id=source_id,
        subject="Q3 scope change",
        sender="Priya Nair <priya@acme.com>",
        sender_email="priya@acme.com",
        sent_at=datetime.now(timezone.utc),
        body_text=body,
        metadata={"message_id": message_id},
    )


def test_saving_the_same_message_twice_keeps_the_first_body(db_session):
    connections = SqlAlchemyConnectionRepository(db_session)
    documents = SqlAlchemyDocumentRepository(db_session)
    connection = connections.upsert(_connection())

    documents.save(connection.id, _document())
    # A message whose triage failed is re-fetched next run, so save must tolerate a
    # repeat rather than tripping the (connection_id, source_id) unique constraint.
    documents.save(connection.id, _document(body="second body"))

    rows = db_session.execute(
        select(orm.Document).where(orm.Document.connection_id == connection.id)
    ).scalars().all()

    assert len(rows) == 1
    assert rows[0].body_text == "first body"


def test_documents_are_scoped_per_connection(db_session):
    connections = SqlAlchemyConnectionRepository(db_session)
    documents = SqlAlchemyDocumentRepository(db_session)

    one = connections.upsert(_connection("one@example.com"))
    two = connections.upsert(_connection("two@example.com"))

    documents.save(one.id, _document())
    # The same provider id in another mailbox is a different message, not a duplicate.
    documents.save(two.id, _document())

    rows = db_session.execute(select(orm.Document)).scalars().all()
    assert {row.connection_id for row in rows} == {one.id, two.id}
    assert documents.exists(one.id, "msg_001") is True
    assert documents.exists(two.id, "msg_999") is False


def test_save_returns_the_row_id_and_keeps_the_message_id(db_session):
    connections = SqlAlchemyConnectionRepository(db_session)
    documents = SqlAlchemyDocumentRepository(db_session)
    connection = connections.upsert(_connection())

    first = documents.save(connection.id, _document())
    # A repeat save returns the same id rather than inserting again.
    second = documents.save(connection.id, _document(body="second body"))

    assert first == second
    row = db_session.get(orm.Document, first)
    assert row.message_id == "<abc@mail>"
    assert row.body_text == "first body"


def test_attachments_hang_off_a_document_and_survive_a_repeat(db_session):
    connections = SqlAlchemyConnectionRepository(db_session)
    documents = SqlAlchemyDocumentRepository(db_session)
    attachments = SqlAlchemyAttachmentRepository(db_session)

    connection = connections.upsert(_connection())
    document_id = documents.save(connection.id, _document())

    attachment = Attachment(
        filename="spec.pdf", mime_type="application/pdf", size_bytes=2048, remote_id="att_1"
    )
    attachments.save(document_id, attachment, location="/tmp/spec.pdf")
    attachments.save(document_id, attachment, location="/tmp/elsewhere.pdf")

    rows = db_session.execute(select(orm.Attachment)).scalars().all()
    assert len(rows) == 1
    assert rows[0].location == "/tmp/spec.pdf"
    assert attachments.exists(document_id, "att_1") is True
    assert attachments.exists(document_id, "att_2") is False


def test_an_oversized_attachment_is_recorded_with_no_location(db_session):
    connections = SqlAlchemyConnectionRepository(db_session)
    documents = SqlAlchemyDocumentRepository(db_session)
    attachments = SqlAlchemyAttachmentRepository(db_session)

    connection = connections.upsert(_connection())
    document_id = documents.save(connection.id, _document())

    attachments.save(
        document_id,
        Attachment(filename="huge.zip", mime_type="application/zip", size_bytes=5 * 10**8,
                   remote_id="att_big"),
        location=None,
    )

    row = db_session.execute(select(orm.Attachment)).scalar_one()
    assert row.location is None
    assert row.size_bytes == 5 * 10**8

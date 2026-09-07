"""The /sync endpoints.

Covers the layer between a use case and its HTTP response — the shape of the JSON, the
totals across mailboxes, and the 404 — which the use-case tests never reach. Both use
cases are replaced through FastAPI's dependency overrides, so these run with no
database, no Gmail and no network.
"""

import os
from datetime import datetime, timezone

import pytest

# Importing the app builds Settings, which insists on DATABASE_URL. These tests never
# reach a database, so an unreachable placeholder satisfies it and they keep running on
# a checkout with no .env — create_engine() only parses the URL, it does not connect.
# setdefault, so a real DATABASE_URL is left alone where one is configured.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://inert:inert@127.0.0.1:1/inert")

from fastapi.testclient import TestClient  # noqa: E402

from src.application.use_cases.sync_mailbox import MailboxSyncResult  # noqa: E402
from src.domain.entities import (  # noqa: E402
    Attachment,
    EmailConnection,
    OAuthTokens,
    RawDocument,
)
from src.domain.ports import ConnectionRepository  # noqa: E402
from src.interfaces.api import deps  # noqa: E402
from src.interfaces.api.main import app  # noqa: E402

SENT_AT = datetime(2026, 3, 4, 9, 30, tzinfo=timezone.utc)


def _connection(connection_id: int = 1, email: str = "a@example.com") -> EmailConnection:
    return EmailConnection(
        id=connection_id,
        provider="gmail",
        mailbox_email=email,
        tokens=OAuthTokens(
            access_token="t", refresh_token="r", expires_at=None, scope="readonly"
        ),
    )


def _document(source_id: str = "m1", attachments: list[Attachment] | None = None) -> RawDocument:
    return RawDocument(
        source="gmail",
        source_id=source_id,
        subject=f"subject {source_id}",
        sender="Priya Nair <priya@acme.com>",
        sender_email="priya@acme.com",
        sent_at=SENT_AT,
        body_text="body",
        attachments=attachments or [],
    )


def _result(email: str, documents: list[RawDocument], new_attachments: int = 0):
    return MailboxSyncResult(
        mailbox_email=email,
        new_messages=len(documents),
        new_attachments=new_attachments,
        documents=list(documents),
    )


class FakeConnections(ConnectionRepository):
    def __init__(self, connections: list[EmailConnection] | None = None):
        self._connections = {c.id: c for c in connections or []}

    def upsert(self, connection: EmailConnection) -> EmailConnection:
        self._connections[connection.id] = connection
        return connection

    def update_tokens(self, connection_id: int, tokens: OAuthTokens) -> None:
        self._connections[connection_id].tokens = tokens

    def get(self, connection_id: int) -> EmailConnection | None:
        return self._connections.get(connection_id)

    def list_all(self) -> list[EmailConnection]:
        return list(self._connections.values())


class FakeSyncAll:
    def __init__(self, results: list[MailboxSyncResult]):
        self._results = results

    def execute(self) -> list[MailboxSyncResult]:
        return self._results


class FakeSyncMailbox:
    """Echoes back the connection it was handed.

    Deliberately not a recorder: FastAPI gives the endpoint a deep copy of an overridden
    dependency, so anything the fake stores on itself is invisible here. Reporting the
    connection through the result makes the route's lookup observable in the response,
    which is the only channel that survives.
    """

    def __init__(self, documents: list[RawDocument], new_attachments: int = 0):
        self._documents = documents
        self._new_attachments = new_attachments

    def execute(self, connection: EmailConnection) -> MailboxSyncResult:
        return _result(connection.mailbox_email, self._documents, self._new_attachments)


@pytest.fixture
def client():
    # Not used as a context manager on purpose: that would run the app's lifespan, and
    # with it init_db() against a real database these tests have no need for.
    yield TestClient(app)
    app.dependency_overrides.clear()


def _override(**dependencies) -> None:
    for name, value in dependencies.items():
        app.dependency_overrides[getattr(deps, name)] = lambda value=value: value


def test_syncing_every_mailbox_reports_each_one_and_the_total(client):
    _override(
        get_sync_all=FakeSyncAll(
            [
                _result("a@example.com", [_document("m1"), _document("m2")]),
                _result("b@example.com", [_document("m3")]),
            ]
        )
    )

    body = client.post("/sync").json()

    assert [m["mailbox_email"] for m in body["mailboxes"]] == ["a@example.com", "b@example.com"]
    assert [m["new_messages"] for m in body["mailboxes"]] == [2, 1]
    assert body["total_new_messages"] == 3


def test_a_mailbox_with_nothing_new_is_still_reported(client):
    _override(get_sync_all=FakeSyncAll([_result("a@example.com", [])]))

    body = client.post("/sync").json()

    assert body["mailboxes"] == [
        {
            "mailbox_email": "a@example.com",
            "new_messages": 0,
            "new_attachments": 0,
            "messages": [],
        }
    ]
    assert body["total_new_messages"] == 0


def test_no_connected_mailboxes_is_an_empty_report_not_an_error(client):
    _override(get_sync_all=FakeSyncAll([]))

    response = client.post("/sync")

    assert response.status_code == 200
    assert response.json() == {"mailboxes": [], "total_new_messages": 0}


def test_each_message_reports_its_subject_sender_and_date(client):
    _override(get_sync_all=FakeSyncAll([_result("a@example.com", [_document("m1")])]))

    message = client.post("/sync").json()["mailboxes"][0]["messages"][0]

    assert message["subject"] == "subject m1"
    # The address alone, not the display name — the parsed field, not the raw header.
    assert message["from"] == "priya@acme.com"
    assert message["sent_at"].startswith("2026-03-04T09:30:00")


def test_a_message_reports_its_attachments_by_name(client):
    document = _document(
        "m1",
        [
            Attachment("spec.pdf", "application/pdf", 1024, "att_1"),
            Attachment("notes.txt", "text/plain", 12, "att_2"),
        ],
    )
    _override(get_sync_all=FakeSyncAll([_result("a@example.com", [document], new_attachments=2)]))

    mailbox = client.post("/sync").json()["mailboxes"][0]

    assert mailbox["new_attachments"] == 2
    assert mailbox["messages"][0]["attachments"] == ["spec.pdf", "notes.txt"]


def test_a_message_with_no_attachments_reports_an_empty_list(client):
    _override(get_sync_all=FakeSyncAll([_result("a@example.com", [_document("m1")])]))

    mailbox = client.post("/sync").json()["mailboxes"][0]

    assert mailbox["new_attachments"] == 0
    assert mailbox["messages"][0]["attachments"] == []


def test_syncing_one_mailbox_returns_only_that_mailbox(client):
    _override(
        get_connections=FakeConnections([_connection(1), _connection(2, "b@example.com")]),
        get_sync_mailbox=FakeSyncMailbox([_document("m3")]),
    )

    body = client.post("/sync/2").json()

    # b@example.com is connection 2's address, so the route resolved the id and handed
    # the whole entity to the use case rather than passing the id along unresolved.
    assert body["mailbox_email"] == "b@example.com"
    assert body["new_messages"] == 1
    # The single-mailbox response is the bare result, not the wrapper /sync returns.
    assert "mailboxes" not in body


def test_syncing_an_unknown_mailbox_is_a_404(client):
    _override(
        get_connections=FakeConnections([_connection(1)]),
        get_sync_mailbox=FakeSyncMailbox([_document("m1")]),
    )

    response = client.post("/sync/99")

    assert response.status_code == 404
    assert "99" in response.json()["detail"]

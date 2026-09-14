"""Claiming behaviour, against a real Postgres.

These cannot use the rolled-back `db_session` fixture: the point of the claim is what one
worker's *committed* transaction looks like to another, which a single connection inside
one transaction cannot show.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from src.domain.entities import EmailConnection, OAuthTokens, ScopeStatus
from src.infrastructure.persistence import orm
from src.infrastructure.persistence.repositories import (
    SqlAlchemyClassificationAuditRepository,
    SqlAlchemyConnectionRepository,
    SqlAlchemyScopeQueueRepository,
)


@pytest.fixture
def workers(engine):
    """Two sessions on independent connections, as two processes would be."""
    a, b = Session(bind=engine), Session(bind=engine)
    try:
        yield a, b
    finally:
        a.rollback()
        b.rollback()
        a.close()
        b.close()
        with Session(bind=engine) as cleanup:
            cleanup.execute(delete(orm.ClassificationAudit))
            cleanup.execute(delete(orm.Document))
            cleanup.execute(delete(orm.IngestionState))
            cleanup.execute(delete(orm.EmailConnection))
            cleanup.commit()


def seed(session: Session, count: int) -> int:
    connection = SqlAlchemyConnectionRepository(session).upsert(
        EmailConnection(
            provider="gmail",
            mailbox_email="queue@example.com",
            tokens=OAuthTokens(
                access_token="t", refresh_token=None, expires_at=None, scope="readonly"
            ),
        )
    )
    session.add_all(
        [
            orm.Document(
                connection_id=connection.id,
                source="gmail",
                source_id=f"msg-{i}",
                subject=f"Subject {i}",
                sender="Acme",
                sender_email="ap@acme.example",
                sent_at=datetime.now(timezone.utc),
                body_text="body",
            )
            for i in range(count)
        ]
    )
    session.commit()
    return connection.id


def test_claiming_marks_documents_in_progress_and_returns_their_content(workers):
    a, _ = workers
    seed(a, 2)

    claimed = SqlAlchemyScopeQueueRepository(a).claim_pending(10)

    assert {c.document.subject for c in claimed} == {"Subject 0", "Subject 1"}
    assert claimed[0].document.body_text == "body"

    statuses = a.execute(select(orm.Document.scope_status)).scalars().all()
    assert set(statuses) == {ScopeStatus.PROCESSING}


def test_a_second_worker_skips_rows_the_first_has_locked(workers):
    a, b = workers
    seed(a, 4)

    # Worker A holds a lock on two rows and has not committed — exactly the window in
    # which a second run fires. Without SKIP LOCKED this call would block on A rather
    # than return, and the test would hang instead of failing.
    a.execute(
        select(orm.Document.id)
        .where(orm.Document.scope_status == ScopeStatus.PENDING)
        .order_by(orm.Document.id)
        .limit(2)
        .with_for_update()
    ).scalars().all()

    claimed = SqlAlchemyScopeQueueRepository(b).claim_pending(10)

    # A set: UPDATE ... RETURNING makes no promise about the order rows come back in.
    assert {c.document.source_id for c in claimed} == {"msg-2", "msg-3"}


def test_two_sequential_claims_never_hand_out_the_same_document(workers):
    a, b = workers
    seed(a, 3)

    first = SqlAlchemyScopeQueueRepository(a).claim_pending(2)
    second = SqlAlchemyScopeQueueRepository(b).claim_pending(2)

    assert [c.id for c in first] + [c.id for c in second] != []
    assert set(c.id for c in first).isdisjoint(c.id for c in second)
    assert len(first) == 2 and len(second) == 1


def test_marking_a_document_moves_it_out_of_the_queue(workers):
    a, b = workers
    seed(a, 1)
    queue = SqlAlchemyScopeQueueRepository(a)

    (claimed,) = queue.claim_pending(10)
    queue.mark(claimed.id, ScopeStatus.COMPLETED)

    assert SqlAlchemyScopeQueueRepository(b).claim_pending(10) == []


def test_stale_in_progress_documents_are_reclaimed_but_fresh_ones_are_left_alone(workers):
    a, b = workers
    seed(a, 2)
    queue = SqlAlchemyScopeQueueRepository(a)

    claimed = queue.claim_pending(10)
    # Age one of them: its worker died twenty minutes ago.
    a.execute(
        update(orm.Document)
        .where(orm.Document.id == claimed[0].id)
        .values(scope_updated_at=datetime.now(timezone.utc) - timedelta(minutes=20))
    )
    a.commit()

    reclaimed = SqlAlchemyScopeQueueRepository(b).reclaim_stale(timedelta(minutes=15), 100)

    assert [r.id for r in reclaimed] == [claimed[0].id]


def test_reclaimed_documents_stay_in_progress_so_a_third_worker_cannot_take_them(workers):
    # If reclaiming released them to pending, another worker could claim one and write the
    # same attempt number the reclaimer is about to write.
    a, b = workers
    seed(a, 1)
    queue = SqlAlchemyScopeQueueRepository(a)

    (claimed,) = queue.claim_pending(10)
    a.execute(
        update(orm.Document)
        .where(orm.Document.id == claimed.id)
        .values(scope_updated_at=datetime.now(timezone.utc) - timedelta(minutes=20))
    )
    a.commit()

    assert len(SqlAlchemyScopeQueueRepository(b).reclaim_stale(timedelta(minutes=15), 100)) == 1
    assert SqlAlchemyScopeQueueRepository(a).claim_pending(10) == []


def test_attempt_numbers_are_counted_per_stage(workers):
    from src.domain.entities import AttemptStatus, ClassificationAttempt

    a, _ = workers
    connection_id = seed(a, 1)
    document_id = a.execute(select(orm.Document.id)).scalar_one()
    audit = SqlAlchemyClassificationAuditRepository(a)

    assert audit.last_attempt(document_id, "scope") == 0

    for stage, attempt in (("scope", 1), ("scope", 2), ("other", 1)):
        audit.append(
            ClassificationAttempt(
                document_id=document_id,
                connection_id=connection_id,
                stage=stage,
                attempt=attempt,
                status=AttemptStatus.FAILED,
                model_version="m",
                prompt_version="p",
                error="nope",
            )
        )

    # A later stage's rows must not inflate this one's count, which is the whole reason
    # the attempt number is stored rather than derived by counting rows.
    assert audit.last_attempt(document_id, "scope") == 2
    assert audit.last_attempt(document_id, "other") == 1

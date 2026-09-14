"""Use-case tests. Every port is faked, so these run with no database and no network."""

from datetime import datetime, timedelta, timezone

from src.application.use_cases.classify_documents import (
    STAGE,
    ClassifyPendingDocuments,
)
from src.domain.entities import (
    AttemptStatus,
    ClaimedDocument,
    Classification,
    ClassificationAttempt,
    ClassificationLabel,
    RawDocument,
    ScopeStatus,
)
from src.domain.errors import ClassificationFailed
from src.domain.ports import (
    ClassificationAuditRepository,
    DocumentClassifier,
    ScopeQueueRepository,
)


def make_document(document_id: int = 1, body: str = "The March invoice is short by $4,000.")\
        -> ClaimedDocument:
    return ClaimedDocument(
        id=document_id,
        connection_id=7,
        document=RawDocument(
            source="gmail",
            source_id=f"msg-{document_id}",
            subject="Invoice query",
            sender="Acme Ltd",
            sender_email="ap@acme.example",
            sent_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            body_text=body,
        ),
    )


class InMemoryQueue(ScopeQueueRepository):
    def __init__(self, pending: list[ClaimedDocument], stale: list[ClaimedDocument] | None = None):
        self._pending = pending
        self._stale = stale or []
        self.statuses: dict[int, str] = {}

    def claim_pending(self, limit: int) -> list[ClaimedDocument]:
        batch, self._pending = self._pending[:limit], self._pending[limit:]
        return batch

    def reclaim_stale(self, older_than: timedelta, limit: int) -> list[ClaimedDocument]:
        stale, self._stale = self._stale, []
        return stale

    def mark(self, document_id: int, status: str) -> None:
        self.statuses[document_id] = status


class InMemoryAudit(ClassificationAuditRepository):
    def __init__(self, existing: dict[int, int] | None = None):
        self.rows: list[ClassificationAttempt] = []
        self._existing = existing or {}

    def append(self, attempt: ClassificationAttempt) -> None:
        self.rows.append(attempt)

    def last_attempt(self, document_id: int, stage: str) -> int:
        recorded = [r.attempt for r in self.rows if r.document_id == document_id and r.stage == stage]
        return max(recorded + [self._existing.get(document_id, 0)])


class FakeClassifier(DocumentClassifier):
    """Answers with whatever the test queued, so both halves of the retry rule can be
    exercised without a model."""

    def __init__(self, answers: list[Classification | Exception]):
        self._answers = answers
        self.seen: list[RawDocument] = []

    @property
    def model_version(self) -> str:
        return "gemini-test-001"

    @property
    def prompt_version(self) -> str:
        return "scope-test"

    def classify(self, document: RawDocument) -> Classification:
        self.seen.append(document)
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def build(queue: InMemoryQueue, audit: InMemoryAudit, classifier: FakeClassifier):
    return ClassifyPendingDocuments(
        queue=queue,
        audit=audit,
        classifier=classifier,
        batch_size=100,
        stale_after=timedelta(minutes=15),
    )


def test_successful_classification_completes_the_document_and_records_the_answer():
    queue = InMemoryQueue(pending=[make_document()])
    audit = InMemoryAudit()
    classifier = FakeClassifier(
        [Classification(label=ClassificationLabel.RELEVANT, confidence=0.93, reason="Invoice short")]
    )

    result = build(queue, audit, classifier).execute()

    assert result.classified == 1
    assert queue.statuses == {1: ScopeStatus.COMPLETED}

    (row,) = audit.rows
    assert (row.attempt, row.status, row.stage) == (1, AttemptStatus.SUCCESS, STAGE)
    assert row.result.label == ClassificationLabel.RELEVANT
    # The circumstances travel with the answer, so a reclassification months later is
    # explainable rather than merely different.
    assert (row.model_version, row.prompt_version) == ("gemini-test-001", "scope-test")


def test_a_failed_attempt_returns_the_document_to_the_queue():
    queue = InMemoryQueue(pending=[make_document()])
    audit = InMemoryAudit()
    classifier = FakeClassifier([ClassificationFailed("timeout")])

    result = build(queue, audit, classifier).execute()

    assert (result.classified, result.failed, result.sent_to_review) == (0, 1, 0)
    assert queue.statuses == {1: ScopeStatus.PENDING}

    (row,) = audit.rows
    assert (row.status, row.attempt, row.error) == (AttemptStatus.FAILED, 1, "timeout")
    assert row.result is None


def test_the_third_failure_sends_the_document_to_review():
    # Two attempts already recorded, so this run's failure is the third and last.
    queue = InMemoryQueue(pending=[make_document()])
    audit = InMemoryAudit(existing={1: 2})
    classifier = FakeClassifier([ClassificationFailed("timeout again")])

    result = build(queue, audit, classifier).execute()

    assert result.sent_to_review == 1
    assert queue.statuses == {1: ScopeStatus.REVIEW}
    assert audit.rows[-1].attempt == 3


def test_a_document_left_in_progress_by_a_dead_worker_costs_an_attempt():
    # Without this the document sits in `processing` for ever, and a message that
    # reliably kills its worker would never exhaust its attempts.
    queue = InMemoryQueue(pending=[], stale=[make_document(document_id=5)])
    audit = InMemoryAudit()

    result = build(queue, audit, FakeClassifier([])).execute()

    assert (result.reclaimed, result.failed) == (1, 1)
    assert queue.statuses == {5: ScopeStatus.PENDING}
    assert audit.rows[0].status == AttemptStatus.FAILED


def test_one_failure_does_not_stop_the_rest_of_the_batch():
    queue = InMemoryQueue(pending=[make_document(1), make_document(2)])
    audit = InMemoryAudit()
    classifier = FakeClassifier(
        [
            ClassificationFailed("boom"),
            Classification(label=ClassificationLabel.JUNK, confidence=0.99, reason="Spam"),
        ]
    )

    result = build(queue, audit, classifier).execute()

    assert (result.failed, result.classified) == (1, 1)
    assert queue.statuses == {1: ScopeStatus.PENDING, 2: ScopeStatus.COMPLETED}

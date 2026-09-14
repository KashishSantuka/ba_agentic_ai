"""Deciding which stored documents are worth taking further.

Holds no clock of its own. Something outside — cron, the CLI, an HTTP call — decides when
a batch runs, which keeps the schedule an operational choice and lets a test run a batch
instantly.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from src.domain.entities import (
    AttemptStatus,
    ClaimedDocument,
    Classification,
    ClassificationAttempt,
    ScopeStatus,
)
from src.domain.errors import ClassificationFailed
from src.domain.ports import (
    ClassificationAuditRepository,
    DocumentClassifier,
    ScopeQueueRepository,
)

logger = logging.getLogger(__name__)

STAGE = "scope"
MAX_ATTEMPTS = 3


@dataclass
class ScopeClassificationResult:
    classified: int = 0
    failed: int = 0
    sent_to_review: int = 0
    reclaimed: int = 0


@dataclass
class ClassifyPendingDocuments:
    queue: ScopeQueueRepository
    audit: ClassificationAuditRepository
    classifier: DocumentClassifier
    batch_size: int
    stale_after: timedelta
    max_attempts: int = MAX_ATTEMPTS

    def execute(self) -> ScopeClassificationResult:
        result = ScopeClassificationResult()

        # Before anything new: documents a previous run was holding when it died. Left
        # alone they stay in progress forever, invisible to a query that only looks for
        # pending work. The lost attempt is recorded, so a document that reliably kills
        # its worker still runs out of attempts instead of retrying for ever.
        for claimed in self.queue.reclaim_stale(self.stale_after, self.batch_size):
            logger.warning("reclaiming document %d left in progress", claimed.id)
            self._record_failure(claimed, "worker lost before the attempt finished", result)
            result.reclaimed += 1

        for claimed in self.queue.claim_pending(self.batch_size):
            try:
                classification = self.classifier.classify(claimed.document)
            except ClassificationFailed as exc:
                logger.warning("classification failed for document %d: %s", claimed.id, exc)
                self._record_failure(claimed, str(exc), result)
                continue

            self._record_success(claimed, classification)
            result.classified += 1

        logger.info(
            "scope classification: %d classified, %d failed, %d to review, %d reclaimed",
            result.classified,
            result.failed,
            result.sent_to_review,
            result.reclaimed,
        )
        return result

    def _record_success(self, claimed: ClaimedDocument, classification: Classification) -> None:
        self._append(claimed, AttemptStatus.SUCCESS, result=classification)
        self.queue.mark(claimed.id, ScopeStatus.COMPLETED)

    def _record_failure(
        self, claimed: ClaimedDocument, error: str, result: ScopeClassificationResult
    ) -> None:
        attempt = self._append(claimed, AttemptStatus.FAILED, error=error)
        result.failed += 1

        if attempt >= self.max_attempts:
            # Out of attempts. Left pending it would be retried for ever at whatever the
            # schedule costs; review is where a person decides what to do with it.
            self.queue.mark(claimed.id, ScopeStatus.REVIEW)
            result.sent_to_review += 1
        else:
            self.queue.mark(claimed.id, ScopeStatus.PENDING)

    def _append(
        self,
        claimed: ClaimedDocument,
        status: str,
        result: Classification | None = None,
        error: str | None = None,
    ) -> int:
        """Write the attempt and return its number.

        Always before the document's status moves. A crash in between leaves the document
        in progress to be reclaimed later, which costs it an extra attempt; the reverse
        order would lose the record that the attempt happened at all, and a document
        whose attempts are never counted is retried without end.
        """
        attempt = self.audit.last_attempt(claimed.id, STAGE) + 1
        self.audit.append(
            ClassificationAttempt(
                document_id=claimed.id,
                connection_id=claimed.connection_id,
                stage=STAGE,
                attempt=attempt,
                status=status,
                model_version=self.classifier.model_version,
                prompt_version=self.classifier.prompt_version,
                result=result,
                error=error,
            )
        )
        return attempt

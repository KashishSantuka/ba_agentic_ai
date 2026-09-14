from typing import Annotated

from fastapi import APIRouter, Depends

from src.application.use_cases.classify_documents import ClassifyPendingDocuments
from src.interfaces.api.deps import get_classify_pending

router = APIRouter(prefix="/classify", tags=["classify"])


@router.post("")
def classify_pending(
    use_case: Annotated[ClassifyPendingDocuments, Depends(get_classify_pending)],
) -> dict:
    """Classify one batch of pending documents.

    One batch per call rather than a loop until empty, so the caller — cron — controls
    how much work happens and a run has a bounded duration.
    """
    result = use_case.execute()
    return {
        "classified": result.classified,
        "failed": result.failed,
        "sent_to_review": result.sent_to_review,
        "reclaimed": result.reclaimed,
    }

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from src.application.use_cases.sync_mailbox import (
    MailboxSyncResult,
    SyncAllMailboxes,
    SyncMailbox,
)
from src.interfaces.api.deps import ConnectionsDep, get_sync_all, get_sync_mailbox

router = APIRouter(prefix="/sync", tags=["sync"])


def _serialise(result: MailboxSyncResult) -> dict:
    return {
        "mailbox_email": result.mailbox_email,
        "new_messages": result.new_messages,
        "new_attachments": result.new_attachments,
        "messages": [
            {
                "subject": d.subject,
                "from": d.sender_email,
                "sent_at": d.sent_at,
                "attachments": [a.filename for a in d.attachments],
            }
            for d in result.documents
        ],
    }


@router.post("")
def sync_all(use_case: Annotated[SyncAllMailboxes, Depends(get_sync_all)]) -> dict:
    """Fetch new mail for every connected mailbox."""
    results = use_case.execute()
    return {
        "mailboxes": [_serialise(r) for r in results],
        "total_new_messages": sum(r.new_messages for r in results),
    }


@router.post("/{connection_id}")
def sync_one(
    connection_id: int,
    connections: ConnectionsDep,
    use_case: Annotated[SyncMailbox, Depends(get_sync_mailbox)],
) -> dict:
    connection = connections.get(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail=f"No connection with id {connection_id}")

    return _serialise(use_case.execute(connection))

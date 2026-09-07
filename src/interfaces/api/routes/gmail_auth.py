from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse

from src.application.use_cases.connect_mailbox import (
    CompleteMailboxConnection,
    StartMailboxConnection,
)
from src.domain.errors import InvalidOAuthState
from src.interfaces.api.deps import (
    ConnectionsDep,
    get_complete_connection,
    get_start_connection,
)

router = APIRouter(prefix="/auth/gmail", tags=["gmail-auth"])


@router.get("/connect")
def connect(
    use_case: Annotated[StartMailboxConnection, Depends(get_start_connection)],
    login_hint: str | None = Query(
        default=None,
        description="Pre-select this Google account on the consent screen, so the wrong "
        "mailbox cannot be connected by accident.",
    ),
) -> RedirectResponse:
    """Send the user to Google's consent screen."""
    return RedirectResponse(use_case.execute(login_hint=login_hint))


@router.get("/callback")
def callback(
    use_case: Annotated[CompleteMailboxConnection, Depends(get_complete_connection)],
    state: str | None = Query(default=None),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> dict:
    """Google redirects the user here once they approve or decline."""
    if error:
        raise HTTPException(status_code=400, detail=f"Google returned an error: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing 'code' or 'state' in callback")

    try:
        connection = use_case.execute(code=code, state=state)
    except InvalidOAuthState as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "connected",
        "connection_id": connection.id,
        "mailbox_email": connection.mailbox_email,
        "scope": connection.tokens.scope,
        "has_refresh_token": connection.tokens.refresh_token is not None,
    }


@router.get("/connections")
def list_connections(connections: ConnectionsDep) -> list[dict]:
    """Which mailboxes are connected. Tokens are deliberately never returned."""
    return [
        {
            "connection_id": c.id,
            "mailbox_email": c.mailbox_email,
            "provider": c.provider,
            "scope": c.tokens.scope,
            "connected_at": c.connected_at,
            "updated_at": c.updated_at,
            "expires_at": c.tokens.expires_at,
        }
        for c in connections.list_all()
    ]

"""FastAPI dependency wiring. The actual object graph is built in src.container."""

from typing import Annotated, Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from src import container
from src.application.use_cases.classify_documents import ClassifyPendingDocuments
from src.application.use_cases.connect_mailbox import (
    CompleteMailboxConnection,
    StartMailboxConnection,
)
from src.application.use_cases.sync_mailbox import SyncAllMailboxes, SyncMailbox
from src.domain.ports import ConnectionRepository
from src.infrastructure.persistence.session import SessionLocal


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_connections(session: SessionDep) -> ConnectionRepository:
    return container.build_connection_repository(session)


def get_start_connection(session: SessionDep) -> StartMailboxConnection:
    return container.build_start_connection(session)


def get_complete_connection(session: SessionDep) -> CompleteMailboxConnection:
    return container.build_complete_connection(session)


def get_sync_mailbox(session: SessionDep) -> SyncMailbox:
    return container.build_sync_mailbox(session)


def get_sync_all(session: SessionDep) -> SyncAllMailboxes:
    return container.build_sync_all(session)


def get_classify_pending(session: SessionDep) -> ClassifyPendingDocuments:
    return container.build_classify_pending(session)


ConnectionsDep = Annotated[ConnectionRepository, Depends(get_connections)]

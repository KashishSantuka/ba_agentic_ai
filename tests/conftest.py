import json
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.infrastructure.persistence.orm import Base

load_dotenv()

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "data" / "fixtures"


@pytest.fixture(scope="session")
def engine():
    """Tests run against a real Postgres database so they exercise the same engine as
    production; a SQLite stand-in would hide Postgres-specific behaviour."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")

    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(engine):
    """Each test runs in a transaction that is rolled back afterwards, so tests stay
    isolated and the test database is left clean."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())

"""Scope classification (AI #1) — schema change, and the code that applies it.

    uv run python migrations/001_scope_classification.py

Hand-written because the schema is otherwise created by Base.metadata.create_all, which
adds missing tables but never alters an existing one. A fresh database gets all of this
from the models; a database that already holds documents needs this file.

Safe to run twice: every statement is guarded, so re-applying is a no-op rather than an
error.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine

SQL = """
BEGIN;

-- Where a document has got to. Existing rows become pending, which is what we want:
-- everything already fetched is waiting to be classified.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS scope_status VARCHAR NOT NULL DEFAULT 'pending';

-- When scope_status last moved. Null until a document is first claimed; the sweeper only
-- looks at rows in `processing`, which always have it set.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS scope_updated_at TIMESTAMP WITH TIME ZONE;

-- Partial: only pending rows are ever searched for, so completed documents stay out of
-- the index instead of growing it for the life of the table.
CREATE INDEX IF NOT EXISTS ix_documents_pending
    ON documents (scope_status) WHERE scope_status = 'pending';

CREATE TABLE IF NOT EXISTS classification_audit (
    id SERIAL NOT NULL,
    document_id INTEGER NOT NULL,
    connection_id INTEGER NOT NULL,
    stage VARCHAR NOT NULL,
    attempt INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    classification VARCHAR,
    confidence FLOAT,
    reason TEXT,
    model_version VARCHAR NOT NULL,
    prompt_version VARCHAR NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    -- A retry that fires twice cannot write two rows both claiming the same attempt.
    UNIQUE (document_id, stage, attempt),
    FOREIGN KEY(document_id) REFERENCES documents (id) ON DELETE CASCADE,
    FOREIGN KEY(connection_id) REFERENCES email_connections (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_classification_audit_document_id
    ON classification_audit (document_id);
CREATE INDEX IF NOT EXISTS ix_classification_audit_connection_id
    ON classification_audit (connection_id);

COMMIT;
"""


if __name__ == "__main__":
    load_dotenv()

    # AUTOCOMMIT because the script above manages its own transaction with BEGIN/COMMIT.
    engine = create_engine(os.environ["DATABASE_URL"], isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.exec_driver_sql(SQL)

    print(f"applied to {engine.url.database}")

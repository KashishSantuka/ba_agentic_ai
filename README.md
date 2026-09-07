# ba_agentic_ai

Business-analyst agent. **Current stage:** connect Gmail mailboxes (read-only) through an
API and fetch new mail incrementally, remembering exactly where each mailbox left off.
Complaint detection with Claude is the next stage, built on top of this.

## Architecture

Domain-driven layout. Dependencies point inwards — the domain knows nothing about
Postgres, Google, or FastAPI.

```
src/
├── domain/                      # business rules. no framework, no I/O
│   ├── entities.py               # RawDocument, EmailConnection, SyncBookmark, OAuthTokens
│   ├── ports.py                  # interfaces the domain needs (repositories, providers)
│   └── errors.py
├── application/
│   └── use_cases/                # ConnectMailbox, SyncMailbox — orchestration only
├── infrastructure/               # the outside world
│   ├── config.py
│   ├── persistence/              # SQLAlchemy ORM + repository implementations
│   └── gmail/                    # OAuth provider, API client, parser, mailbox reader
├── interfaces/                   # entry points
│   ├── api/                      # FastAPI routes
│   └── cli.py
└── container.py                  # composition root — binds ports to implementations
```

Adding Outlook or IMAP later means writing new adapters under `infrastructure/`
implementing `OAuthProvider` and `MailboxReader`. Nothing in `domain/` or
`application/` changes.

## Setup

### 1. PostgreSQL

Requires a running PostgreSQL server and two databases:

```bash
createdb ba_agentic
createdb ba_agentic_test    # used only by the tests
```

### 2. Google Cloud

1. Create a project at [console.cloud.google.com](https://console.cloud.google.com)
2. Enable the **Gmail API**
3. **OAuth consent screen** → External, Testing mode → add each mailbox you'll connect
   as a **test user**
4. **Credentials** → Create OAuth client ID → **Web application**
5. Under **Authorized redirect URIs**, add exactly:
   ```
   http://localhost:8000/auth/gmail/callback
   ```
6. Copy the Client ID and Client Secret

### 3. Configure

```bash
uv sync
cp .env.example .env
```

Fill in `.env` — note that special characters in the Postgres password must be
URL-encoded (`@` becomes `%40`):

```
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:8000/auth/gmail/callback
DATABASE_URL=postgresql+psycopg://postgres:pass%40word@localhost:5433/ba_agentic
TEST_DATABASE_URL=postgresql+psycopg://postgres:pass%40word@localhost:5433/ba_agentic_test
```

## Running

```bash
uv run uvicorn src.interfaces.api.main:app --reload --port 8000
```

Interactive API docs: <http://localhost:8000/docs>

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness check |
| `GET /auth/gmail/connect?login_hint=you@gmail.com` | start the consent flow |
| `GET /auth/gmail/callback` | where Google returns — registered redirect URI |
| `GET /auth/gmail/connections` | connected mailboxes (never returns tokens) |
| `POST /sync` | fetch new mail for every mailbox |
| `POST /sync/{connection_id}` | fetch new mail for one mailbox |

To connect a mailbox, open in a browser:

```
http://localhost:8000/auth/gmail/connect?login_hint=you@gmail.com
```

`login_hint` pre-selects the account, so the wrong mailbox can't be connected by
accident — whichever account approves is the one that gets read.

## CLI

```bash
uv run python -m src.interfaces.cli init-db
uv run python -m src.interfaces.cli connections
uv run python -m src.interfaces.cli sync
```

Connecting a mailbox is API-only, since OAuth needs a browser redirect to a stable URL.

## Scheduled syncing

```
*/30 * * * * cd /path/to/ba_agentic_ai && uv run python -m src.interfaces.cli sync >> data/sync.log 2>&1
```

## Tests

```bash
uv run pytest
```

Domain and use-case tests run with in-memory fakes (no database, no network).
Repository tests run against `ba_agentic_test`.

## Notes

- Scope is `gmail.readonly` only — the app cannot send, delete, or modify mail.
- Tokens live in the `email_connections` table, one row per mailbox, with a nullable
  `user_id` ready for when signup/login is added.
- Refresh tokens are currently stored in plain text. Encrypt that column before real
  users exist.

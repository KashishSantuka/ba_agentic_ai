import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    database_url: str
    gmail_sync_lookback_days: int
    log_level: str

    attachment_dir: Path
    attachment_max_bytes: int


def load_settings() -> Settings:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and point it at your "
            "Postgres database, e.g. postgresql+psycopg://<user>@localhost:5433/ba_agentic"
        )

    return Settings(
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        google_redirect_uri=os.environ.get(
            "GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/gmail/callback"
        ),
        database_url=database_url,
        gmail_sync_lookback_days=int(os.environ.get("GMAIL_SYNC_LOOKBACK_DAYS", "30")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        attachment_dir=Path(os.environ.get("ATTACHMENT_DIR", "data/attachments")),
        # Gmail's own send limit is 25MB; anything at that size is recorded but not
        # written, so one enormous file cannot fill the disk unnoticed.
        attachment_max_bytes=int(os.environ.get("ATTACHMENT_MAX_BYTES", str(25 * 1024 * 1024))),
    )


settings = load_settings()

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.infrastructure.config import settings
from src.infrastructure.persistence.session import init_db
from src.interfaces.api.routes import classify, gmail_auth, sync

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="ba_agentic_ai",
    description="Business-analyst agent — mailbox connection and ingestion API.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(gmail_auth.router)
app.include_router(sync.router)
app.include_router(classify.router)


@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}

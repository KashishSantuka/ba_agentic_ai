from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.infrastructure.config import settings
from src.infrastructure.persistence.orm import Base

engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    Base.metadata.create_all(engine)

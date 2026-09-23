from collections.abc import Generator

from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.engine import build_engine

settings = get_settings()
settings.ensure_local_directories()

engine = build_engine(settings.active_database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session

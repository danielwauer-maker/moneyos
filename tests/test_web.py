from collections.abc import Generator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_db
from app.main import app


def test_all_main_pages_are_reachable() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine)

    def override_db() -> Generator[Session, None, None]:
        with testing_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            for path in [
                "/",
                "/transactions",
                "/envelopes",
                "/accounts",
                "/planning",
                "/projects",
                "/categories",
                "/review",
                "/transaction-review",
                "/import",
                "/diagnostics",
                "/settings",
                "/health",
            ]:
                response = client.get(path)
                assert response.status_code == 200, path
    finally:
        app.dependency_overrides.clear()

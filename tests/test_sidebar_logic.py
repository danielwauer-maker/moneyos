from collections.abc import Generator
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Account,
    BalanceConfirmation,
    Category,
    CategoryAssignmentDecision,
    EconomicEvent,
    EconomicTypeAssignmentDecision,
    EnvelopeAssignmentDecision,
    EventSourceLink,
    ProjectAssignmentDecision,
    ReviewItem,
    SourceTransaction,
)
from app.db.session import get_db
from app.main import app
from app.services.dashboard import build_dashboard


def _test_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _override(factory):
    def override_db() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    return override_db


def test_transaction_type_filters_are_functional() -> None:
    engine, factory = _test_session()
    with factory() as db:
        for event_type, description in (
            ("expense", "Nur Ausgabe"),
            ("income", "Nur Einnahme"),
            ("transfer", "Nur Transfer"),
            ("refund", "Nur Refund"),
        ):
            db.add(
                EconomicEvent(
                    event_type=event_type,
                    occurred_at=datetime(2026, 10, 1, 12),
                    description=description,
                    amount=Decimal("10.00"),
                    status="booked",
                )
            )
        db.commit()

    app.dependency_overrides[get_db] = _override(factory)
    try:
        with TestClient(app) as client:
            cases = {
                "expense": ("Nur Ausgabe", "Nur Einnahme"),
                "income": ("Nur Einnahme", "Nur Ausgabe"),
                "transfer": ("Nur Transfer", "Nur Refund"),
                "refund": ("Nur Refund", "Nur Transfer"),
            }
            for event_type, (included, excluded) in cases.items():
                response = client.get(f"/transactions?event_type={event_type}")
                assert response.status_code == 200
                assert included in response.text
                assert excluded not in response.text
            assert client.get("/transactions?event_type=unknown").status_code == 422
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_fully_reviewed_stale_review_item_is_not_actionable() -> None:
    engine, factory = _test_session()
    with factory() as db:
        source = SourceTransaction(
            source_system="synthetic",
            source_transaction_id="stale-review",
            booked_at=datetime(2026, 9, 21, 12),
            merchant_raw="Charanjit Singh Pelia",
            description_raw="Historisch bereits bestätigt",
            amount=Decimal("-48.50"),
            fingerprint="stale-review".ljust(64, "0"),
        )
        category = Category(name="Testkategorie")
        db.add_all([source, category])
        db.flush()
        event = EconomicEvent(
            event_type="expense",
            occurred_at=source.booked_at,
            description="Charanjit Singh Pelia",
            amount=Decimal("48.50"),
            category_id=category.id,
            status="booked",
        )
        db.add(event)
        db.flush()
        db.add(
            EventSourceLink(
                economic_event_id=event.id,
                source_transaction_id=source.id,
                link_type="canonical_source",
            )
        )
        key = f"source:{source.id}"
        now = datetime(2026, 10, 3, 12)
        db.add_all(
            [
                EconomicTypeAssignmentDecision(
                    candidate_key=key,
                    source_transaction_id=source.id,
                    economic_event_id=event.id,
                    decision="expense",
                    decided_at=now,
                ),
                CategoryAssignmentDecision(
                    candidate_key=key,
                    source_transaction_id=source.id,
                    economic_event_id=event.id,
                    category_id=category.id,
                    decided_at=now,
                ),
                EnvelopeAssignmentDecision(
                    candidate_key=key,
                    source_transaction_id=source.id,
                    economic_event_id=event.id,
                    decision="no_envelope",
                    decided_at=now,
                ),
                ProjectAssignmentDecision(
                    candidate_key=key,
                    source_transaction_id=source.id,
                    economic_event_id=event.id,
                    decision="no_project",
                    decided_at=now,
                ),
                ReviewItem(
                    source_transaction_id=source.id,
                    economic_event_id=event.id,
                    review_type="economic_type",
                    proposed_event_type="expense",
                    confidence=Decimal("0.55"),
                    explanation="Alter offener Review-Eintrag",
                    status="open",
                ),
            ]
        )
        db.commit()

    app.dependency_overrides[get_db] = _override(factory)
    try:
        with TestClient(app) as client:
            response = client.get("/review")
            assert response.status_code == 200
            assert "Charanjit Singh Pelia" not in response.text
            assert "Keine offenen Reviews." in response.text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_dashboard_uses_current_balances_once_and_refunds_in_cashflow() -> None:
    engine, factory = _test_session()
    with factory() as db:
        checking = Account(name="Giro", account_type="checking", is_active=True)
        card = Account(
            name="Kreditkarte",
            account_type="credit_card",
            is_active=True,
            is_liability=True,
        )
        db.add_all([checking, card])
        db.flush()
        db.add_all(
            [
                BalanceConfirmation(
                    account_id=checking.id,
                    confirmed_at=datetime(2026, 10, 3, 12),
                    balance=Decimal("1000.00"),
                    source_type="bank_statement",
                    confidence=Decimal("1"),
                    status="confirmed",
                ),
                BalanceConfirmation(
                    account_id=card.id,
                    confirmed_at=datetime(2026, 10, 3, 12),
                    balance=Decimal("-300.00"),
                    source_type="card_statement",
                    confidence=Decimal("1"),
                    status="confirmed",
                ),
                EconomicEvent(
                    event_type="expense",
                    occurred_at=datetime.now().replace(day=1, hour=12, minute=0, second=0, microsecond=0),
                    description="Monatsausgabe",
                    amount=Decimal("200.00"),
                    status="booked",
                ),
                EconomicEvent(
                    event_type="income",
                    occurred_at=datetime.now().replace(day=1, hour=13, minute=0, second=0, microsecond=0),
                    description="Monatseinnahme",
                    amount=Decimal("500.00"),
                    status="booked",
                ),
                EconomicEvent(
                    event_type="refund",
                    occurred_at=datetime.now().replace(day=1, hour=14, minute=0, second=0, microsecond=0),
                    description="Monatsrefund",
                    amount=Decimal("50.00"),
                    status="booked",
                ),
            ]
        )
        db.commit()
        data = build_dashboard(db)

        assert data["free_available"] == Decimal("1000.00")
        assert data["card_liabilities"] == Decimal("300.00")
        assert data["wealth"] == Decimal("700.00")
        assert data["income"] == Decimal("500.00")
        assert data["expenses"] == Decimal("200.00")
        assert data["refunds"] == Decimal("50.00")
        assert data["cashflow"] == Decimal("350.00")

    engine.dispose()


def test_sidebar_pages_are_reachable_and_placeholders_are_explicit() -> None:
    engine, factory = _test_session()
    app.dependency_overrides[get_db] = _override(factory)
    try:
        with TestClient(app) as client:
            paths = [
                "/",
                "/transactions",
                "/envelopes",
                "/accounts",
                "/planning",
                "/projects",
                "/categories",
                "/review",
                "/transaction-review",
                "/envelope-assignments",
                "/import",
                "/settings",
            ]
            for path in paths:
                response = client.get(path)
                assert response.status_code == 200, path

            dashboard = client.get("/")
            assert "6.200 €" not in dashboard.text
            assert "Noch nicht produktiv berechnet." in dashboard.text

            planning = client.get("/planning")
            settings = client.get("/settings")
            assert "Noch nicht produktiv implementiert" in planning.text
            assert "Noch nicht produktiv implementiert" in settings.text
    finally:
        app.dependency_overrides.clear()
        engine.dispose()

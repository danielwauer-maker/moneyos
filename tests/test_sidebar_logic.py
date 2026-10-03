from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Account,
    BalanceConfirmation,
    Category,
    EconomicEvent,
    Envelope,
    Project,
    EventSourceLink,
    ReviewItem,
    SourceTransaction,
)
from app.db.session import get_db
from app.main import app
from app.services.dashboard import build_dashboard
from app.services.reviews import actionable_review_count
from app.services.transaction_review import apply_transaction_decision


D = Decimal


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'sidebar.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _confirmed_account(
    db: Session,
    *,
    name: str,
    account_type: str,
    balance: str,
    liability: bool = False,
) -> Account:
    account = Account(
        name=name,
        account_type=account_type,
        is_liability=liability,
        is_active=True,
    )
    db.add(account)
    db.flush()
    db.add(
        BalanceConfirmation(
            account_id=account.id,
            confirmed_at=datetime(2026, 10, 3, 12),
            balance=D(balance),
            currency="EUR",
            source_type="bank_statement",
            confidence=D("1"),
            status="confirmed",
        )
    )
    return account


def test_dashboard_aggregates_accounts_and_treats_refunds_as_cashflow(db: Session) -> None:
    _confirmed_account(db, name="Giro A", account_type="checking", balance="1000")
    _confirmed_account(db, name="Giro B", account_type="checking", balance="500")
    _confirmed_account(db, name="PayPal", account_type="paypal", balance="100")
    _confirmed_account(
        db,
        name="Kreditkarte",
        account_type="credit_card",
        balance="-200",
        liability=True,
    )
    db.add_all(
        [
            EconomicEvent(
                event_type="income",
                occurred_at=datetime(2026, 10, 1, 8),
                description="Gehalt",
                amount=D("1000"),
                status="booked",
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 2, 8),
                description="Einkauf",
                amount=D("300"),
                status="booked",
            ),
            EconomicEvent(
                event_type="refund",
                occurred_at=datetime(2026, 10, 3, 8),
                description="Erstattung",
                amount=D("50"),
                status="booked",
            ),
        ]
    )
    db.commit()

    dashboard = build_dashboard(db, calculation_date=date(2026, 10, 3))

    assert dashboard["wealth"] == D("1400")
    assert dashboard["free_available"] == D("1400")
    assert dashboard["card_liabilities"] == D("200")
    assert dashboard["income"] == D("1000")
    assert dashboard["expenses"] == D("300")
    assert dashboard["refunds"] == D("50")
    assert dashboard["cashflow"] == D("750")


def test_completed_excel_style_decision_is_not_actionable_review(db: Session) -> None:
    source = SourceTransaction(
        source_system="synthetic",
        source_transaction_id="excel-complete",
        booked_at=datetime(2026, 9, 21, 12),
        merchant_raw="Charanjit Singh Pelia",
        description_raw="Synthetischer Alt-Review",
        amount=D("-48.50"),
        fingerprint="excel-complete".ljust(64, "0"),
    )
    category = Category(name="Gastronomie")
    db.add_all([source, category])
    db.flush()
    event = EconomicEvent(
        event_type="expense",
        occurred_at=source.booked_at,
        description="Charanjit Singh Pelia",
        amount=D("48.50"),
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
    db.add(
        ReviewItem(
            source_transaction_id=source.id,
            economic_event_id=event.id,
            review_type="economic_type",
            proposed_event_type="expense",
            confidence=D("0.55"),
            explanation="Alter Import-Prüffall",
            status="open",
        )
    )
    db.commit()

    apply_transaction_decision(
        db,
        candidate_keys=[f"source:{source.id}"],
        economic_type="expense",
        category_id=category.id,
        envelope_decision="no_envelope",
        project_decision="no_project",
    )
    db.commit()

    assert actionable_review_count(db) == 0


def test_transaction_page_type_and_account_filters_are_effective(db: Session) -> None:
    first = Account(name="Giro", account_type="checking", is_active=True)
    second = Account(name="PayPal", account_type="paypal", is_active=True)
    db.add_all([first, second])
    db.flush()
    db.add_all(
        [
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 1, 8),
                description="Nur Ausgabe",
                amount=D("25"),
                account_id=first.id,
                status="booked",
            ),
            EconomicEvent(
                event_type="income",
                occurred_at=datetime(2026, 10, 2, 8),
                description="Nur Einnahme",
                amount=D("50"),
                account_id=second.id,
                status="booked",
            ),
        ]
    )
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            expense_page = client.get("/transactions?type=expense")
            paypal_page = client.get(f"/transactions?account={second.id}")
    finally:
        app.dependency_overrides.clear()

    assert expense_page.status_code == 200
    assert "Nur Ausgabe" in expense_page.text
    assert "Nur Einnahme" not in expense_page.text
    assert paypal_page.status_code == 200
    assert "Nur Einnahme" in paypal_page.text
    assert "Nur Ausgabe" not in paypal_page.text


def test_transaction_page_hides_non_effective_event_statuses(db: Session) -> None:
    db.add_all(
        [
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 1, 8),
                description="Wirksame Ausgabe",
                amount=D("10"),
                status="booked",
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 2, 8),
                description="Nicht wirksamer Entwurf",
                amount=D("999"),
                status="draft",
            ),
        ]
    )
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get("/transactions")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Wirksame Ausgabe" in page.text
    assert "Nicht wirksamer Entwurf" not in page.text


def test_project_totals_use_only_effective_economic_events(db: Session) -> None:
    project = Project(name="Sidebar-Audit-Projekt", status="active")
    db.add(project)
    db.flush()
    db.add_all(
        [
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 1, 8),
                description="Gebuchte Projektausgabe",
                amount=D("40"),
                project_id=project.id,
                status="booked",
            ),
            EconomicEvent(
                event_type="refund",
                occurred_at=datetime(2026, 10, 2, 8),
                description="Gebuchter Projektrefund",
                amount=D("10"),
                project_id=project.id,
                status="confirmed",
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 3, 8),
                description="Nicht wirksamer Projektentwurf",
                amount=D("999"),
                project_id=project.id,
                status="draft",
            ),
        ]
    )
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get("/projects")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "3 Vorgänge" not in page.text
    assert "2 Vorgänge" in page.text
    assert "40,00" in page.text
    assert "10,00" in page.text
    assert "999,00" not in page.text


def test_category_page_labels_pending_counts_as_source_decisions(db: Session) -> None:
    parent = Category(name="Wohnen")
    db.add(parent)
    db.flush()
    db.add(Category(name="Haushalt", parent_id=parent.id))
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get("/categories")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Quellentscheidungen ohne Ereignis" in page.text
    assert "offene Review-Entscheidungen" not in page.text

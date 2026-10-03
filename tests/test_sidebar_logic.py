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
    EnvelopeSnapshot,
    EventSourceLink,
    Project,
    ReviewItem,
    SourceTransaction,
)
from app.db.session import get_db
from app.main import app
from app.services.categories import category_groups
from app.services.dashboard import build_dashboard
from app.services.envelope_targets import calculate_envelope_targets
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


def test_transaction_page_date_merchant_category_and_project_filters(db: Session) -> None:
    account = Account(name="Giro", account_type="checking", is_active=True)
    category = Category(name="Reise")
    project = Project(name="Frankreich 2026", status="active")
    db.add_all([account, category, project])
    db.flush()
    db.add_all(
        [
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 9, 28, 8),
                description="Boulangerie Le Lavandou",
                amount=D("18"),
                account_id=account.id,
                category_id=category.id,
                project_id=project.id,
                status="booked",
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 8, 1, 8),
                description="Anderer Händler",
                amount=D("22"),
                account_id=account.id,
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
            page = client.get(
                "/transactions",
                params={
                    "date_from": "2026-09-01",
                    "date_to": "2026-09-30",
                    "merchant": "Boulangerie",
                    "category": str(category.id),
                    "project": str(project.id),
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Boulangerie Le Lavandou" in page.text
    assert "Anderer Händler" not in page.text


def test_transfer_page_shows_source_to_target_and_filters_both_accounts(db: Session) -> None:
    source = Account(name="Giro Quelle", account_type="checking", is_active=True)
    target = Account(name="PayPal Ziel", account_type="paypal", is_active=True)
    db.add_all([source, target])
    db.flush()
    db.add(
        EconomicEvent(
            event_type="transfer",
            occurred_at=datetime(2026, 10, 1, 8),
            description="Interner Transfer",
            amount=D("75"),
            source_account_id=source.id,
            target_account_id=target.id,
            status="booked",
        )
    )
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get(f"/transactions?type=transfer&account={target.id}")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Giro Quelle" in page.text
    assert "PayPal Ziel" in page.text
    assert "→" in page.text


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


def test_envelope_targets_handles_open_review_rows(db: Session) -> None:
    envelope = Envelope(name="Haushalt", is_active=True, sort_order=1)
    db.add(envelope)
    db.flush()
    db.add(
        EnvelopeSnapshot(
            envelope_id=envelope.id,
            snapshot_date=date(2026, 9, 1),
            physical_balance=D("100"),
            source="confirmed_private_baseline",
            is_confirmed=True,
        )
    )
    source = SourceTransaction(
        source_system="synthetic",
        source_transaction_id="open-review-envelope-target",
        booked_at=datetime(2026, 9, 15, 12),
        merchant_raw="Test Merchant",
        description_raw="Offener Prüffall",
        amount=D("-20"),
        fingerprint="open-review-envelope-target".ljust(64, "0"),
    )
    db.add(source)
    db.flush()
    db.add(
        ReviewItem(
            source_transaction_id=source.id,
            review_type="economic_type",
            proposed_event_type="expense",
            proposed_envelope_id=envelope.id,
            confidence=D("0.5"),
            explanation="Regression für Envelope-Target-Reviewabfrage",
            status="open",
        )
    )
    db.commit()

    result = calculate_envelope_targets(
        db,
        calculation_date=date(2026, 10, 3),
        free_vault_cash=D("0"),
    )

    assert result.unresolved_count == 1
    assert result.rows[0].unresolved_count == 1
    assert result.rows[0].status == "partial"


def test_category_counts_exclude_non_effective_events(db: Session) -> None:
    category = Category(name="Wirksame Kategorie")
    db.add(category)
    db.flush()
    db.add_all(
        [
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 1, 8),
                description="Gebucht",
                amount=D("10"),
                category_id=category.id,
                status="booked",
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 2, 8),
                description="Bestätigt",
                amount=D("20"),
                category_id=category.id,
                status="confirmed",
            ),
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 3, 8),
                description="Entwurf",
                amount=D("999"),
                category_id=category.id,
                status="draft",
            ),
        ]
    )
    db.commit()

    groups = category_groups(db)

    assert groups[0].parent.event_count == 2


def test_transaction_page_rejects_invalid_date_filters(db: Session) -> None:
    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            invalid = client.get("/transactions?date_from=not-a-date")
            reversed_range = client.get("/transactions?date_from=2026-10-31&date_to=2026-10-01")
    finally:
        app.dependency_overrides.clear()

    assert invalid.status_code == 422
    assert reversed_range.status_code == 422


def test_dashboard_labels_all_liabilities_generically(db: Session) -> None:
    _confirmed_account(
        db,
        name="Sonstige Verbindlichkeit",
        account_type="loan",
        balance="-123",
        liability=True,
    )
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get("/")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Verbindlichkeiten" in page.text
    assert "Bestätigte Verbindlichkeitskonten" in page.text
    assert "Kartenverbindlichkeiten" not in page.text

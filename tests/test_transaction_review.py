from collections.abc import Generator
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    AssignmentRule,
    Category,
    EconomicEvent,
    Envelope,
    ProjectAssignmentDecision,
    ReviewItem,
    SourceTransaction,
)
from app.db.session import get_db
from app.main import app
from app.services.transaction_review import (
    apply_transaction_decision,
    build_transaction_review,
    create_project,
)


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _source(db: Session, key: str, day: int, *, event_type: str | None = None) -> SourceTransaction:
    source = SourceTransaction(
        source_system="synthetic",
        source_transaction_id=key,
        booked_at=datetime(2026, 7, day),
        merchant_raw="Synthetischer Händler",
        description_raw="Synthetische Buchung",
        amount=Decimal("-12.34"),
        fingerprint=key.ljust(64, "0"),
    )
    db.add(source)
    db.flush()
    if event_type:
        event = EconomicEvent(
            event_type=event_type,
            occurred_at=source.booked_at,
            description="Synthetischer Händler",
            amount=Decimal("12.34"),
            account_id=None,
        )
        db.add(event)
        db.flush()
        from app.db.models import EventSourceLink

        db.add(
            EventSourceLink(
                economic_event_id=event.id,
                source_transaction_id=source.id,
                link_type="canonical_source",
            )
        )
    return source


def test_chronological_order_and_project_assignment(db: Session) -> None:
    older = _source(db, "older", 2, event_type="expense")
    newer = _source(db, "newer", 8, event_type="expense")
    project = create_project(db, name="Dänemark Urlaub 2026")
    db.commit()

    rows, _ = build_transaction_review(db, sort="newest")
    assert [row.source.id for row in rows] == [newer.id, older.id]
    apply_transaction_decision(
        db,
        candidate_keys=[f"source:{older.id}"],
        project_decision="assigned",
        project_id=project.id,
    )
    db.commit()
    rows, _ = build_transaction_review(db, sort="oldest")
    assert rows[0].project.name == "Dänemark Urlaub 2026"


def test_no_project_is_resolved_and_dimensions_are_independent(db: Session) -> None:
    source = _source(db, "no-project", 3, event_type="expense")
    category = Category(name="Lebensmittel")
    envelope = Envelope(name="Urlaub", target_rule_type="monthly_contribution")
    db.add_all([category, envelope])
    db.commit()
    apply_transaction_decision(
        db,
        candidate_keys=[f"source:{source.id}"],
        category_id=category.id,
        envelope_decision="assigned",
        envelope_id=envelope.id,
        project_decision="no_project",
    )
    db.commit()
    row = build_transaction_review(db, sort="oldest")[0][0]
    assert row.category.name == "Lebensmittel"
    assert row.envelope.name == "Urlaub"
    assert row.project is None
    assert row.project_resolved
    assert row.fully_reviewed


def test_type_confirmation_creates_one_event_for_review_only(db: Session) -> None:
    source = _source(db, "review-only", 4)
    db.add(
        ReviewItem(
            source_transaction_id=source.id,
            review_type="economic_type",
            proposed_event_type=None,
            confidence=Decimal("0.5"),
            explanation="Synthetische Prüfung",
        )
    )
    db.commit()
    apply_transaction_decision(db, candidate_keys=[f"source:{source.id}"], economic_type="expense")
    db.commit()
    assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 1
    apply_transaction_decision(db, candidate_keys=[f"source:{source.id}"], economic_type="expense")
    db.commit()
    assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 1


def test_review_only_stores_project_and_category_before_event(db: Session) -> None:
    source = _source(db, "review-decisions", 5)
    category = Category(name="Reise")
    project = create_project(db, name="Synthetische Reise")
    db.add(category)
    db.commit()

    apply_transaction_decision(
        db,
        candidate_keys=[f"source:{source.id}"],
        category_id=category.id,
        project_decision="assigned",
        project_id=project.id,
    )
    db.commit()

    assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0
    decision = db.scalar(select(ProjectAssignmentDecision))
    assert decision is not None
    assert decision.project_id == project.id
    apply_transaction_decision(db, candidate_keys=[f"source:{source.id}"], economic_type="expense")
    db.commit()
    event = db.scalar(select(EconomicEvent))
    assert event is not None
    assert event.category_id == category.id
    assert event.project_id == project.id


def test_month_and_project_filtering(db: Session) -> None:
    july = _source(db, "july", 6, event_type="expense")
    august = SourceTransaction(
        source_system="synthetic",
        source_transaction_id="august",
        booked_at=datetime(2026, 8, 1),
        merchant_raw="Anderer Händler",
        description_raw="Synthetisch",
        amount=Decimal("-9.00"),
        fingerprint="august".ljust(64, "0"),
    )
    db.add(august)
    project = create_project(db, name="Filterprojekt")
    db.commit()
    apply_transaction_decision(
        db,
        candidate_keys=[f"source:{july.id}"],
        project_decision="assigned",
        project_id=project.id,
    )
    db.commit()

    month_rows, _ = build_transaction_review(db, month="2026-07")
    project_rows, _ = build_transaction_review(db, project_id=project.id)
    assert [row.source.id for row in month_rows] == [july.id]
    assert [row.source.id for row in project_rows] == [july.id]


def test_review_ui_shows_prominent_amount_and_inline_dimensions(db: Session) -> None:
    source = _source(db, "ui-review", 7)
    db.add(
        ReviewItem(
            source_transaction_id=source.id,
            review_type="economic_type",
            proposed_event_type="expense",
            confidence=Decimal("0.7"),
            explanation="Synthetische Prüfung",
        )
    )
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            inbox = client.get("/review")
            chronological = client.get("/transaction-review")
    finally:
        app.dependency_overrides.clear()

    assert inbox.status_code == 200
    assert 'class="review-amount amount red"' in inbox.text
    assert "12,34" in inbox.text
    assert chronological.status_code == 200
    for field in ("economic_type", "category_id", "envelope_id", "project_id"):
        assert f'name="{field}"' in chronological.text


def test_bulk_assignment_and_suggestion_rule(db: Session) -> None:
    first = _source(db, "bulk-first", 8, event_type="expense")
    second = _source(db, "bulk-second", 9, event_type="expense")
    project = create_project(db, name="Gemeinsames Projekt")
    db.commit()

    applied = apply_transaction_decision(
        db,
        candidate_keys=[f"source:{first.id}", f"source:{second.id}"],
        project_decision="assigned",
        project_id=project.id,
        create_rule=True,
    )
    db.commit()

    assert applied == 2
    assert all(row.project_id == project.id for row in db.scalars(select(EconomicEvent)))
    rule = db.scalar(select(AssignmentRule))
    assert rule is not None
    assert rule.rule_type == "transaction_review_suggestion"
    assert rule.action_json["project_id"] == project.id


def test_suggestion_rule_never_overwrites_manual_category(db: Session) -> None:
    source = _source(db, "protected", 10, event_type="expense")
    manual = Category(name="Manuell bestätigt")
    suggested = Category(name="Nur vorgeschlagen")
    db.add_all([manual, suggested])
    db.commit()
    apply_transaction_decision(db, candidate_keys=[f"source:{source.id}"], category_id=manual.id)
    db.add(
        AssignmentRule(
            rule_type="transaction_review_suggestion",
            priority=1,
            enabled=True,
            condition_json={"merchant_pattern": "synthetischer händler"},
            action_json={"category_id": suggested.id},
        )
    )
    db.commit()

    row = build_transaction_review(db)[0][0]
    assert row.category.id == manual.id
    assert row.proposed_category.id == suggested.id


def test_category_picker_order_is_shared_across_review_workflows(db: Session) -> None:
    zulu = Category(name="Zulu")
    alpha = Category(name="Alpha")
    db.add_all([zulu, alpha])
    db.flush()
    db.add_all(
        [
            Category(name="Zweite", parent_id=alpha.id),
            Category(name="Erste", parent_id=alpha.id),
            Category(name="Kind", parent_id=zulu.id),
        ]
    )
    source = _source(db, "shared-picker", 11)
    db.add(
        ReviewItem(
            source_transaction_id=source.id,
            review_type="economic_type",
            proposed_event_type="expense",
            confidence=Decimal("0.8"),
            explanation="Synthetische Prüfung",
        )
    )
    db.commit()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            pages = [
                client.get("/review"),
                client.get("/transaction-review"),
                client.get("/envelope-assignments"),
            ]
    finally:
        app.dependency_overrides.clear()

    for page in pages:
        assert page.status_code == 200
        text = page.text
        alpha_pos = text.index('<optgroup label="Alpha">')
        zulu_pos = text.index('<optgroup label="Zulu">')
        first_child_pos = text.index("↳ Erste", alpha_pos)
        second_child_pos = text.index("↳ Zweite", alpha_pos)
        assert alpha_pos < zulu_pos
        assert alpha_pos < first_child_pos < second_child_pos < zulu_pos
        assert "● Alpha (Hauptkategorie)" in text

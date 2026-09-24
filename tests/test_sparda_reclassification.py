from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Category,
    CategoryAssignmentDecision,
    EconomicEvent,
    Envelope,
    EventSourceLink,
    ReviewItem,
    SourceTransaction,
)
from app.services.categories import category_by_path
from app.services.envelope_assignments import build_assignment_workspace
from app.services.sparda_reclassification import (
    apply_sparda_reclassification,
    audit_sparda_transaction_details,
    plan_sparda_reclassification,
)

D = Decimal


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path}/reclassification.db")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _source(db: Session, key: str, detail: str, *, amount: str = "-10") -> SourceTransaction:
    source = SourceTransaction(
        source_system="sparda",
        source_transaction_id=f"sparda:{key}",
        booked_at=datetime(2026, 9, 1),
        merchant_raw="DZ BANK AG",
        description_raw=f"Kartenzahlung Debit MC | {detail}",
        amount=D(amount),
        fingerprint=key.ljust(64, "0"),
    )
    db.add(source)
    db.flush()
    return source


def _event(
    db: Session, source: SourceTransaction, category: Category | None = None
) -> EconomicEvent:
    event = EconomicEvent(
        event_type="expense",
        occurred_at=source.booked_at,
        description="DZ BANK AG",
        amount=abs(source.amount),
        category_id=category.id if category else None,
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
    db.flush()
    return event


def test_high_confidence_event_is_reclassified_without_source_mutation(db: Session) -> None:
    source = _source(db, "spar", "SPAR KARREB.KSM/Street/City/DK/2")
    event = _event(db, source)
    original_source = (source.merchant_raw, source.description_raw, source.amount)

    dry_run = plan_sparda_reclassification(db)
    result = apply_sparda_reclassification(db)

    assert dry_run.high_confidence_auto_confirm_candidates == 1
    assert result.auto_confirmed == 1
    assert event.category is not None
    assert event.category.name == "Supermarkt"
    assert event.category.parent.name == "Lebensmittel"
    assert (source.merchant_raw, source.description_raw, source.amount) == original_source


def test_manual_category_decision_is_never_overwritten(db: Session) -> None:
    manual_parent = Category(name="Manuell")
    db.add(manual_parent)
    db.flush()
    manual_category = Category(name="Bestätigt", parent_id=manual_parent.id)
    db.add(manual_category)
    db.flush()
    source = _source(db, "manual", "SPAR TEST/Street/City/DK/2")
    event = _event(db, source, manual_category)
    db.add(
        CategoryAssignmentDecision(
            candidate_key=f"source:{source.id}",
            source_transaction_id=source.id,
            economic_event_id=event.id,
            category_id=manual_category.id,
            decided_at=datetime(2026, 9, 2),
        )
    )
    db.flush()

    report = apply_sparda_reclassification(db)
    audit = audit_sparda_transaction_details(db)

    assert report.protected_manual_decisions == 1
    assert audit.protected_manual_decisions == 1
    assert audit.usable_secondary_detail == 1
    assert audit.canonical_merchant_differs == 1
    assert event.category_id == manual_category.id


def test_review_only_receives_proposal_without_economic_event(db: Session) -> None:
    source = _source(db, "bageri", "ENO BAGERI APS/Street/City/DK/2")
    review = ReviewItem(
        source_transaction_id=source.id,
        review_type="economic_type",
        proposed_event_type="expense",
        confidence=D("0.55"),
        explanation="Synthetisch unklar",
        status="open",
    )
    db.add(review)
    db.flush()

    report = apply_sparda_reclassification(db)

    assert report.suggested == 1
    assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0
    assert review.proposed_category_id == category_by_path(db, "Lebensmittel", "Bäckerei").id


def test_category_reclassification_leaves_envelope_independent(db: Session) -> None:
    envelope = Envelope(name="Synthetischer Reiseumschlag")
    db.add(envelope)
    db.flush()
    source = _source(db, "ferry", "SCANDLINES PR./Harbour/City/DE/0")
    event = _event(db, source)
    event.envelope_id = envelope.id

    apply_sparda_reclassification(db)

    assert event.category.name == "Fähre"
    assert event.envelope_id == envelope.id


def test_assignment_workspace_explains_generic_and_canonical_merchant(
    db: Session,
) -> None:
    source = _source(db, "workspace", "SPAR TEST/Street/City/DK/2")
    _event(db, source)
    apply_sparda_reclassification(db)

    row = build_assignment_workspace(db, state="all").candidates[0]

    assert row.raw_counterparty == "DZ BANK AG"
    assert row.canonical_merchant == "SPAR TEST"
    assert row.category.name == "Supermarkt"


def test_medium_confidence_creates_suggestion_not_assignment(db: Session) -> None:
    source = SourceTransaction(
        source_system="sparda",
        source_transaction_id="sparda:medium",
        booked_at=datetime(2026, 9, 1),
        merchant_raw="DZ BANK AG",
        description_raw="Lastschrift | Restaurant Beispiel",
        amount=D("-20"),
        fingerprint="medium".ljust(64, "0"),
    )
    db.add(source)
    db.flush()
    event = _event(db, source)

    report = apply_sparda_reclassification(db)

    assert report.medium_confidence_suggestions == 1
    assert event.category_id is None
    review = db.scalar(select(ReviewItem).where(ReviewItem.source_transaction_id == source.id))
    assert review is not None
    assert review.proposed_category.name == "Restaurant"

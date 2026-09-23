from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    EconomicEvent,
    Envelope,
    EnvelopeRulePeriod,
    EnvelopeSnapshot,
    EventSourceLink,
    ReviewItem,
    SourceTransaction,
)
from app.db.session import get_db
from app.main import app
from app.services.envelope_targets import calculate_envelope_targets

D = Decimal


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path}/targets.db")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _envelope(
    db: Session,
    name: str = "Test-Umschlag",
    *,
    baseline: str = "100",
    old: str = "10",
    new: str = "20",
    target: str | None = None,
) -> Envelope:
    envelope = Envelope(
        name=name,
        target_rule_type="target_balance" if target else "monthly_contribution",
        target_amount=D(target) if target else None,
    )
    db.add(envelope)
    db.flush()
    db.add(
        EnvelopeSnapshot(
            envelope_id=envelope.id,
            snapshot_date=date(2026, 4, 30),
            physical_balance=D(baseline),
            source="confirmed_private_baseline",
            is_confirmed=True,
        )
    )
    db.add_all(
        [
            EnvelopeRulePeriod(
                envelope_id=envelope.id,
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 5, 31),
                monthly_amount=None if target else D(old),
                rule_type="target_balance" if target else "monthly_contribution",
                target_amount=D(target) if target else None,
            ),
            EnvelopeRulePeriod(
                envelope_id=envelope.id,
                valid_from=date(2026, 6, 1),
                valid_to=None,
                monthly_amount=None if target else D(new),
                rule_type="target_balance" if target else "monthly_contribution",
                target_amount=D(target) if target else None,
            ),
        ]
    )
    db.flush()
    return envelope


def _event(
    db: Session,
    event_type: str,
    amount: str,
    *,
    envelope: Envelope | None = None,
    day: int = 10,
    occurred_at: datetime | None = None,
) -> EconomicEvent:
    event = EconomicEvent(
        event_type=event_type,
        occurred_at=occurred_at or datetime(2026, 5, day, 12),
        description="Synthetisches Ereignis",
        amount=D(amount),
        envelope_id=envelope.id if envelope else None,
        status="booked",
    )
    db.add(event)
    db.flush()
    return event


def _source(db: Session, key: str, day: int = 10) -> SourceTransaction:
    source = SourceTransaction(
        source_system="synthetic",
        source_transaction_id=key,
        booked_at=datetime(2026, 5, day, 12),
        description_raw="Synthetischer Vorgang",
        amount=D("25"),
        fingerprint=key.ljust(64, "0"),
    )
    db.add(source)
    db.flush()
    return source


def _view(db: Session, calculation_date: date = date(2026, 7, 15), free: str = "0"):
    db.flush()
    return calculate_envelope_targets(
        db, calculation_date=calculation_date, free_vault_cash=D(free)
    )


def test_may_uses_old_rule_and_june_onward_uses_new_rule(db: Session) -> None:
    _envelope(db)

    may = _view(db, date(2026, 5, 31)).rows[0]
    july = _view(db, date(2026, 7, 31)).rows[0]

    assert may.contributions == D("10")
    assert july.contributions == D("50")
    assert july.accounting_target == D("150")


def test_assigned_expense_reduces_target_but_not_physical_actual(db: Session) -> None:
    envelope = _envelope(db)
    _event(db, "expense", "23", envelope=envelope)

    row = _view(db, date(2026, 5, 31)).rows[0]

    assert row.accounting_target == D("87")
    assert row.actual == D("100")
    assert row.included_event_ids


def test_unassigned_expense_does_not_reduce_target_and_marks_partial(db: Session) -> None:
    _envelope(db)
    _event(db, "expense", "23")

    view = _view(db, date(2026, 5, 31))

    assert view.rows[0].accounting_target == D("110")
    assert view.rows[0].status == "partial"
    assert view.unresolved_count == 1


def test_review_only_transaction_does_not_affect_target(db: Session) -> None:
    _envelope(db)
    source = _source(db, "review")
    db.add(
        ReviewItem(
            source_transaction_id=source.id,
            review_type="classification",
            proposed_event_type="expense",
            confidence=D("0.4"),
            explanation="Synthetisch unklar",
        )
    )

    view = _view(db, date(2026, 5, 31))

    assert view.rows[0].expenses == ZERO
    assert view.rows[0].accounting_target == D("110")
    assert view.unresolved_count == 1


def test_partial_calculation_warning_is_visible(db: Session) -> None:
    _envelope(db)
    source = _source(db, "visible-review")
    db.add(
        ReviewItem(
            source_transaction_id=source.id,
            review_type="classification",
            proposed_event_type="expense",
            confidence=D("0.4"),
            explanation="Synthetisch unklar",
        )
    )
    db.flush()

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.get("/envelopes")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "Vorläufiger Abgleich" in response.text
    assert "1 Transaktionen" in response.text


def test_traceable_refund_restores_original_envelope(db: Session) -> None:
    envelope = _envelope(db)
    expense = _event(
        db,
        "expense",
        "40",
        envelope=envelope,
        occurred_at=datetime(2026, 4, 5, 12),
    )
    refund = _event(db, "refund", "15", day=12)
    original_source = _source(db, "original", day=5)
    db.add_all(
        [
            EventSourceLink(
                economic_event_id=expense.id,
                source_transaction_id=original_source.id,
                link_type="canonical_source",
            ),
            EventSourceLink(
                economic_event_id=refund.id,
                source_transaction_id=original_source.id,
                link_type="refund_origin",
            ),
        ]
    )

    row = _view(db, date(2026, 5, 31)).rows[0]

    assert row.expenses == ZERO
    assert row.refunds == D("15")
    assert row.accounting_target == D("125")


def test_dentist_target_stays_at_500_after_expense(db: Session) -> None:
    dentist = _envelope(db, "Zahnarztgeld", baseline="500", target="500")
    _event(db, "expense", "120", envelope=dentist)

    row = _view(db, date(2026, 9, 30)).rows[0]

    assert row.contributions == ZERO
    assert row.accounting_target == D("500")
    assert row.target_adjustment == D("120")


def test_five_euro_rounding_keeps_remainder_separate(db: Session) -> None:
    _envelope(db, baseline="112.99", old="0", new="0")

    row = _view(db, date(2026, 5, 31)).rows[0]

    assert row.physical_target == D("110")
    assert row.rounding_remainder == D("2.99")
    assert row.deficit == ZERO


def test_negative_accounting_target_becomes_zero_with_deficit(db: Session) -> None:
    envelope = _envelope(db, baseline="10", old="0", new="0")
    _event(db, "expense", "22.50", envelope=envelope)

    row = _view(db, date(2026, 5, 31)).rows[0]

    assert row.accounting_target == D("-12.50")
    assert row.physical_target == ZERO
    assert row.rounding_remainder == ZERO
    assert row.deficit == D("12.50")


def test_actual_target_delta_and_transfer_money(db: Session) -> None:
    _envelope(db, "Einzahlen", baseline="100", old="20", new="20")
    _envelope(db, "Entnehmen", baseline="100", old="0", new="0")
    withdraw = db.scalar(select(Envelope).where(Envelope.name == "Entnehmen"))
    assert withdraw is not None
    _event(db, "expense", "30", envelope=withdraw)

    view = _view(db, date(2026, 5, 31))

    assert [row.delta for row in view.rows] == [D("20"), D("-30")]
    assert view.reconciliation.deposit_total == D("20")
    assert view.reconciliation.withdraw_total == D("30")
    assert view.reconciliation.transfer_money == D("20")


def test_bank_withdrawal_is_remaining_need_when_vault_cash_is_zero(db: Session) -> None:
    _envelope(db, baseline="100", old="40", new="40")

    result = _view(db, date(2026, 5, 31), free="0").reconciliation

    assert result.remaining_need == D("40")
    assert result.free_vault_cash_used == ZERO
    assert result.bank_withdrawal_needed == D("40")
    assert result.free_vault_cash_after == ZERO


ZERO = D("0")

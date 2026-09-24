from __future__ import annotations

from collections.abc import Generator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Account,
    AssignmentRule,
    Category,
    CategoryAssignmentDecision,
    EconomicEvent,
    Envelope,
    EnvelopeAssignmentDecision,
    EnvelopeRulePeriod,
    EnvelopeSnapshot,
    EventSourceLink,
    ReviewItem,
    SourceTransaction,
    SourceTransactionAccount,
)
from app.db.session import get_db
from app.main import app
from app.services.envelope_assignments import (
    apply_assignment_decisions,
    build_assignment_workspace,
    create_subcategory,
)
from app.services.envelope_targets import calculate_envelope_targets

D = Decimal


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path}/assignment.db")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _account(db: Session) -> Account:
    account = db.scalar(select(Account).where(Account.name == "Synthetisches Giro"))
    if account is None:
        account = Account(name="Synthetisches Giro", account_type="checking")
        db.add(account)
        db.flush()
    return account


def _category(db: Session, name: str = "Synthetische Kategorie") -> Category:
    category = db.scalar(select(Category).where(Category.name == name))
    if category is None:
        category = Category(name=name)
        db.add(category)
        db.flush()
    return category


def _envelope(db: Session, name: str = "Test-Umschlag") -> Envelope:
    envelope = Envelope(name=name, target_rule_type="monthly_contribution")
    db.add(envelope)
    db.flush()
    db.add_all(
        [
            EnvelopeSnapshot(
                envelope_id=envelope.id,
                snapshot_date=date(2026, 4, 30),
                physical_balance=D("100"),
                source="confirmed_private_baseline",
                is_confirmed=True,
            ),
            EnvelopeRulePeriod(
                envelope_id=envelope.id,
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 5, 31),
                monthly_amount=D("0"),
                rule_type="monthly_contribution",
            ),
            EnvelopeRulePeriod(
                envelope_id=envelope.id,
                valid_from=date(2026, 6, 1),
                valid_to=None,
                monthly_amount=D("0"),
                rule_type="monthly_contribution",
            ),
        ]
    )
    db.flush()
    return envelope


def _transaction(
    db: Session,
    key: str,
    *,
    amount: str = "20",
    merchant: str = "Synthetischer Händler",
    event_type: str = "expense",
    category: Category | None = None,
) -> tuple[SourceTransaction, EconomicEvent]:
    account = _account(db)
    source = SourceTransaction(
        source_system="synthetic",
        source_transaction_id=key,
        booked_at=datetime(2026, 5, 10, 12),
        merchant_raw=merchant,
        description_raw="Synthetische Buchung",
        amount=-D(amount) if event_type == "expense" else D(amount),
        fingerprint=key.ljust(64, "0"),
    )
    db.add(source)
    db.flush()
    db.add(
        SourceTransactionAccount(
            source_transaction_id=source.id, account_id=account.id, role="source"
        )
    )
    event = EconomicEvent(
        event_type=event_type,
        occurred_at=source.booked_at,
        description=merchant,
        amount=D(amount),
        account_id=account.id,
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
    return source, event


def _key(source: SourceTransaction) -> str:
    return f"source:{source.id}"


def _review_only_transaction(
    db: Session, key: str, *, merchant: str = "Unklarer synthetischer Händler"
) -> SourceTransaction:
    account = _account(db)
    source = SourceTransaction(
        source_system="synthetic",
        source_transaction_id=key,
        booked_at=datetime(2026, 5, 11, 12),
        merchant_raw=merchant,
        description_raw="Synthetischer Prüffall",
        amount=-D("12.50"),
        fingerprint=key.ljust(64, "r"),
    )
    db.add(source)
    db.flush()
    db.add_all(
        [
            SourceTransactionAccount(
                source_transaction_id=source.id, account_id=account.id, role="source"
            ),
            ReviewItem(
                source_transaction_id=source.id,
                review_type="classification",
                proposed_event_type="expense",
                confidence=D("0.4"),
                explanation="Synthetischer Prüffall",
                status="open",
            ),
        ]
    )
    db.flush()
    return source


def _target(db: Session):
    db.flush()
    return calculate_envelope_targets(
        db,
        calculation_date=date(2026, 5, 31),
        free_vault_cash=D("0"),
    )


def test_assignment_changes_target_but_not_physical_actual(db: Session) -> None:
    envelope = _envelope(db)
    source, event = _transaction(db, "assigned")

    apply_assignment_decisions(
        db,
        keys=[_key(source)],
        decision="assigned",
        envelope_id=envelope.id,
    )
    assert event.envelope_id == envelope.id
    # A durable source-level decision also survives later event materialization/backfill.
    event.envelope_id = None
    row = _target(db).rows[0]

    assert row.accounting_target == D("80")
    assert row.actual == D("100")
    assert row.delta == D("-20")


def test_no_envelope_is_final_and_later_stays_unresolved(db: Session) -> None:
    _envelope(db)
    final_source, _ = _transaction(db, "final-none")
    later_source, _ = _transaction(db, "later")

    apply_assignment_decisions(
        db, keys=[_key(final_source)], decision="no_envelope", envelope_id=None
    )
    apply_assignment_decisions(db, keys=[_key(later_source)], decision="later", envelope_id=None)
    view = _target(db)
    workspace = build_assignment_workspace(db)

    assert view.unresolved_count == 1
    assert workspace.progress.no_envelope == 1
    assert workspace.progress.unresolved == 1


def test_bulk_assignment_updates_all_selected_events(db: Session) -> None:
    envelope = _envelope(db)
    first_source, first_event = _transaction(db, "bulk-one")
    second_source, second_event = _transaction(db, "bulk-two")

    count = apply_assignment_decisions(
        db,
        keys=[_key(first_source), _key(second_source)],
        decision="assigned",
        envelope_id=envelope.id,
    )

    assert count == 2
    assert first_event.envelope_id == envelope.id
    assert second_event.envelope_id == envelope.id
    assert _target(db).rows[0].expenses == D("40")


def test_reusable_rule_proposes_but_never_silently_assigns(db: Session) -> None:
    envelope = _envelope(db)
    first_source, _ = _transaction(db, "rule-one", merchant="Wiederholter Händler")
    second_source, _ = _transaction(db, "rule-two", merchant="Wiederholter Händler")

    apply_assignment_decisions(
        db,
        keys=[_key(first_source), _key(second_source)],
        decision="assigned",
        envelope_id=envelope.id,
        create_rule=True,
        rule_basis="merchant",
    )
    third_source, third_event = _transaction(db, "rule-three", merchant="Wiederholter Händler")
    workspace = build_assignment_workspace(db)
    third = next(row for row in workspace.candidates if row.key == _key(third_source))

    assert db.scalar(select(func.count()).select_from(AssignmentRule)) == 1
    assert third.proposed_envelope.id == envelope.id
    assert third_event.envelope_id is None


def test_category_rule_is_only_a_proposal(db: Session) -> None:
    envelope = _envelope(db)
    category = _category(db)
    first_source, _ = _transaction(db, "category-one", category=category)
    second_source, _ = _transaction(db, "category-two", category=category)
    apply_assignment_decisions(
        db,
        keys=[_key(first_source), _key(second_source)],
        decision="assigned",
        envelope_id=envelope.id,
        create_rule=True,
        rule_basis="category",
    )

    third_source, third_event = _transaction(db, "category-three", category=category)
    third = next(
        row for row in build_assignment_workspace(db).candidates if row.key == _key(third_source)
    )

    assert third.proposed_envelope.id == envelope.id
    assert third_event.envelope_id is None


def test_category_and_envelope_decisions_remain_independent(db: Session) -> None:
    envelope = _envelope(db)
    category = _category(db)
    source, event = _transaction(db, "independent")

    apply_assignment_decisions(
        db, keys=[_key(source)], category_id=category.id, apply_category=True
    )
    assert event.category_id == category.id
    assert event.envelope_id is None
    assert build_assignment_workspace(db).progress.envelopes_confirmed == 0

    apply_assignment_decisions(
        db, keys=[_key(source)], decision="assigned", envelope_id=envelope.id
    )
    assert event.category_id == category.id
    assert event.envelope_id == envelope.id
    progress = build_assignment_workspace(db).progress
    assert progress.categories_confirmed == 1
    assert progress.envelopes_confirmed == 1
    assert progress.fully_reviewed == 1


def test_bulk_category_and_combined_assignment(db: Session) -> None:
    envelope = _envelope(db)
    category = _category(db)
    first_source, first_event = _transaction(db, "category-bulk-one")
    second_source, second_event = _transaction(db, "category-bulk-two")

    apply_assignment_decisions(
        db,
        keys=[_key(first_source), _key(second_source)],
        category_id=category.id,
        apply_category=True,
    )
    assert first_event.category_id == second_event.category_id == category.id
    assert first_event.envelope_id is None and second_event.envelope_id is None

    apply_assignment_decisions(
        db,
        keys=[_key(first_source), _key(second_source)],
        decision="assigned",
        envelope_id=envelope.id,
        category_id=category.id,
        apply_category=True,
    )
    assert first_event.envelope_id == second_event.envelope_id == envelope.id
    assert db.scalar(select(func.count()).select_from(CategoryAssignmentDecision)) == 2


def test_review_only_category_decision_does_not_create_event(db: Session) -> None:
    category = _category(db)
    source = _review_only_transaction(db, "review-only-category")

    apply_assignment_decisions(
        db, keys=[_key(source)], category_id=category.id, apply_category=True
    )

    decision = db.scalar(
        select(CategoryAssignmentDecision).where(
            CategoryAssignmentDecision.source_transaction_id == source.id
        )
    )
    assert decision is not None and decision.category_id == category.id
    assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 0


def test_combined_rule_only_proposes_category_and_no_envelope(db: Session) -> None:
    _envelope(db)
    category = _category(db)
    first_source, _ = _transaction(db, "combined-rule-one", merchant="Regel Händler")
    second_source, _ = _transaction(db, "combined-rule-two", merchant="Regel Händler")
    apply_assignment_decisions(
        db,
        keys=[_key(first_source), _key(second_source)],
        decision="no_envelope",
        category_id=category.id,
        apply_category=True,
        create_rule=True,
        rule_basis="merchant",
    )

    third_source, third_event = _transaction(db, "combined-rule-three", merchant="Regel Händler")
    row = next(
        item for item in build_assignment_workspace(db).candidates if item.key == _key(third_source)
    )
    assert row.proposed_category.id == category.id
    assert row.proposed_envelope_decision == "no_envelope"
    assert third_event.category_id is None
    assert third_event.envelope_id is None


def test_duplicate_subcategory_is_rejected_case_insensitively(db: Session) -> None:
    parent = _category(db, "Lebensmittel")
    create_subcategory(db, parent_id=parent.id, name="Supermarkt")

    with pytest.raises(ValueError, match="existiert bereits"):
        create_subcategory(db, parent_id=parent.id, name="  supermarkt  ")


def test_category_update_does_not_change_financial_balances(db: Session) -> None:
    envelope = _envelope(db)
    category = _category(db)
    source, event = _transaction(db, "category-no-money")
    account = _account(db)
    account.balance = D("123.45")
    before_snapshot = db.scalar(
        select(EnvelopeSnapshot.physical_balance).where(EnvelopeSnapshot.envelope_id == envelope.id)
    )

    apply_assignment_decisions(
        db, keys=[_key(source)], category_id=category.id, apply_category=True
    )

    assert event.category_id == category.id
    assert account.balance == D("123.45")
    assert (
        db.scalar(
            select(EnvelopeSnapshot.physical_balance).where(
                EnvelopeSnapshot.envelope_id == envelope.id
            )
        )
        == before_snapshot
    )


def test_refund_recalculates_once_and_repeated_decision_is_idempotent(db: Session) -> None:
    envelope = _envelope(db)
    expense_source, _ = _transaction(db, "expense", amount="50")
    refund_source, _ = _transaction(db, "refund", amount="15", event_type="refund")

    apply_assignment_decisions(
        db,
        keys=[_key(expense_source), _key(refund_source)],
        decision="assigned",
        envelope_id=envelope.id,
    )
    apply_assignment_decisions(
        db,
        keys=[_key(refund_source)],
        decision="assigned",
        envelope_id=envelope.id,
    )
    row = _target(db).rows[0]

    assert row.expenses == D("50")
    assert row.refunds == D("15")
    assert row.accounting_target == D("65")
    assert db.scalar(select(func.count()).select_from(EnvelopeAssignmentDecision)) == 2


def test_dedicated_workspace_and_quick_action(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    envelope = _envelope(db)
    source, _ = _transaction(db, "web-action")

    def override_db() -> Generator[Session, None, None]:
        yield db

    class PrivateSettings:
        demo_mode = False

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr("app.web.routes.get_settings", lambda: PrivateSettings())
    try:
        with TestClient(app) as client:
            page = client.get("/envelope-assignments")
            saved = client.post(
                "/envelope-assignments/decide",
                data={
                    "candidate_keys": _key(source),
                    "decision": "assigned",
                    "envelope_id": str(envelope.id),
                    "return_to": "/envelope-assignments#workspace",
                },
                follow_redirects=False,
            )
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert "Umschlag-Zuordnung offen" in page.text
    assert "Synthetischer Händler" in page.text
    assert saved.status_code == 303
    assert saved.headers["location"].endswith("#workspace")

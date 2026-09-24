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
    Category,
    CategoryAssignmentDecision,
    EconomicEvent,
    Envelope,
    EnvelopeSnapshot,
    SourceTransaction,
)
from app.db.session import get_db
from app.main import app
from app.services.categories import (
    category_groups,
    category_selector_groups,
    create_category,
    hard_delete_category,
    rename_category,
    set_category_active,
)
from app.services.envelope_assignments import build_assignment_workspace

D = Decimal


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path}/categories.db")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_create_main_category_and_subcategory(db: Session) -> None:
    parent = create_category(db, name="Freizeit")
    child = create_category(db, name="Kultur", parent_id=parent.id)

    assert parent.parent_id is None
    assert child.parent_id == parent.id
    assert child.parent is parent


def test_duplicate_prevention_normalizes_case_and_whitespace(db: Session) -> None:
    create_category(db, name="  Freizeit   und Kultur ")

    with pytest.raises(ValueError, match="existiert bereits"):
        create_category(db, name="freizeit und kultur")


def test_same_display_name_is_allowed_only_at_different_hierarchy_levels(
    db: Session,
) -> None:
    parent = create_category(db, name="Kleidung")
    child = create_category(db, name="Kleidung", parent_id=parent.id)

    assert child.parent_id == parent.id
    with pytest.raises(ValueError, match="existiert bereits"):
        create_category(db, name=" kleidung ", parent_id=parent.id)


def test_hierarchy_is_limited_to_parent_and_child(db: Session) -> None:
    parent = create_category(db, name="Hauptkategorie")
    child = create_category(db, name="Unterkategorie", parent_id=parent.id)

    with pytest.raises(ValueError, match="keine weiteren Unterkategorien"):
        create_category(db, name="Dritte Ebene", parent_id=child.id)


def test_rename_preserves_event_and_decision_relationships(db: Session) -> None:
    category = create_category(db, name="Alter Name")
    event = EconomicEvent(
        event_type="expense",
        occurred_at=datetime(2026, 5, 1),
        description="Synthetisches Ereignis",
        amount=D("10"),
        category_id=category.id,
    )
    source = SourceTransaction(
        source_system="synthetic",
        source_transaction_id="rename-source",
        booked_at=datetime(2026, 5, 1),
        description_raw="Synthetischer Prüffall",
        amount=-D("10"),
        fingerprint="rename".ljust(64, "0"),
    )
    db.add_all([event, source])
    db.flush()
    decision = CategoryAssignmentDecision(
        candidate_key=f"source:{source.id}",
        source_transaction_id=source.id,
        category_id=category.id,
        decided_at=datetime(2026, 5, 2),
    )
    db.add(decision)
    db.flush()
    original_id = category.id

    renamed = rename_category(db, category_id=category.id, name="Neuer Name")
    db.flush()

    assert renamed.id == original_id
    assert event.category_id == original_id
    assert decision.category_id == original_id
    assert db.scalar(select(func.count()).select_from(Category)) == 1


def test_deactivate_reactivate_and_assignment_selector(db: Session) -> None:
    category = create_category(db, name="Temporär")
    set_category_active(db, category_id=category.id, active=False)

    assert category.is_active is False
    assert category.id not in {item.id for item in build_assignment_workspace(db).categories}

    set_category_active(db, category_id=category.id, active=True)
    assert category.is_active is True
    assert category.id in {item.id for item in build_assignment_workspace(db).categories}


def test_active_parent_requires_children_to_be_deactivated_first(db: Session) -> None:
    parent = create_category(db, name="Eltern")
    child = create_category(db, name="Kind", parent_id=parent.id)

    with pytest.raises(ValueError, match="Unterkategorien zuerst"):
        set_category_active(db, category_id=parent.id, active=False)
    set_category_active(db, category_id=child.id, active=False)
    set_category_active(db, category_id=parent.id, active=False)
    assert parent.is_active is False


def test_category_in_use_cannot_be_hard_deleted(db: Session) -> None:
    category = create_category(db, name="Historisch verwendet")
    db.add(
        EconomicEvent(
            event_type="expense",
            occurred_at=datetime(2026, 5, 1),
            description="Synthetisches Ereignis",
            amount=D("5"),
            category_id=category.id,
        )
    )
    db.flush()

    with pytest.raises(ValueError, match="nicht gelöscht"):
        hard_delete_category(db, category_id=category.id)
    assert db.get(Category, category.id) is category


def test_usage_information_counts_events_and_pending_decisions(db: Session) -> None:
    category = create_category(db, name="Verwendet")
    source = SourceTransaction(
        source_system="synthetic",
        source_transaction_id="usage-source",
        booked_at=datetime(2026, 5, 1),
        description_raw="Synthetischer Prüffall",
        amount=-D("3"),
        fingerprint="usage".ljust(64, "0"),
    )
    db.add_all(
        [
            source,
            EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 5, 1),
                description="Synthetisches Ereignis",
                amount=D("3"),
                category_id=category.id,
            ),
        ]
    )
    db.flush()
    db.add(
        CategoryAssignmentDecision(
            candidate_key=f"source:{source.id}",
            source_transaction_id=source.id,
            category_id=category.id,
            decided_at=datetime(2026, 5, 2),
        )
    )
    db.flush()

    row = category_groups(db)[0].parent
    assert row.event_count == 1
    assert row.pending_decision_count == 1


def test_quick_create_appears_immediately_in_assignment_workflow(db: Session) -> None:
    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/envelope-assignments/categories",
                data={
                    "name": "Schnell erstellt",
                    "return_to": "/envelope-assignments?state=all#workspace",
                },
                follow_redirects=False,
            )
            page = client.get(response.headers["location"])
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 303
    assert "state=all" in response.headers["location"]
    assert "new_category=" in response.headers["location"]
    assert response.headers["location"].endswith("#workspace")
    assert page.status_code == 200
    assert "Schnell erstellt" in page.text
    assert "selected>\nSchnell erstellt" not in page.text  # compact template has inline options
    assert "selected>● Schnell erstellt (Hauptkategorie)</option>" in page.text


def test_category_management_does_not_change_financial_balances(db: Session) -> None:
    account = Account(name="Synthetisches Konto", account_type="checking", balance=D("42.50"))
    envelope = Envelope(name="Synthetischer Umschlag")
    db.add_all([account, envelope])
    db.flush()
    snapshot = EnvelopeSnapshot(
        envelope_id=envelope.id,
        snapshot_date=date(2026, 4, 30),
        physical_balance=D("25"),
        source="synthetic",
        is_confirmed=True,
    )
    db.add(snapshot)
    db.flush()

    category = create_category(db, name="Ohne Saldenwirkung")
    rename_category(db, category_id=category.id, name="Weiterhin ohne Saldenwirkung")
    set_category_active(db, category_id=category.id, active=False)

    assert account.balance == D("42.50")
    assert snapshot.physical_balance == D("25")


def test_category_selector_groups_are_alphabetical(db: Session) -> None:
    zulu = create_category(db, name="Zulu")
    alpha = create_category(db, name="Alpha")
    create_category(db, name="Zweite", parent_id=alpha.id)
    create_category(db, name="Erste", parent_id=alpha.id)
    create_category(db, name="Kind", parent_id=zulu.id)

    groups = category_selector_groups(db)

    assert [group.parent.name for group in groups] == ["Alpha", "Zulu"]
    assert [child.name for child in groups[0].children] == ["Erste", "Zweite"]


def test_assignment_category_picker_is_hierarchical_and_searchable(db: Session) -> None:
    parent = create_category(db, name="Auto & Mobilität")
    child = create_category(db, name="Fähre", parent_id=parent.id)

    def override_db() -> Generator[Session, None, None]:
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get("/envelope-assignments?state=all")
            script = client.get("/static/app.js")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert '<optgroup label="Auto &amp; Mobilität">' in page.text
    assert f'value="{parent.id}"' in page.text
    assert f'value="{child.id}"' in page.text
    assert "data-category-search=" in page.text
    assert "data-category-text=" in page.text
    assert "toLocaleLowerCase" in script.text

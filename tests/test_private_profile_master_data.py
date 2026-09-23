from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.migrations import upgrade_database
from app.db.models import (
    Account,
    EconomicEvent,
    Envelope,
    EnvelopeRulePeriod,
    EnvelopeSnapshot,
    EventSourceLink,
    ReviewItem,
    SourceTransaction,
    SourceTransactionAccount,
)
from app.db.session import get_db
from app.main import app
from app.services.dashboard import build_dashboard
from app.services.private_profile import initialize_private_profile
from app.services.reviews import build_review_view

D = Decimal

ACCOUNT_SPECS = [
    {
        "key": "sparda",
        "name": "Sparda Girokonto",
        "account_type": "checking",
        "is_active": True,
        "is_liability": False,
    },
    {
        "key": "amex",
        "name": "American Express",
        "account_type": "credit_card",
        "is_active": True,
        "is_liability": True,
    },
    {
        "key": "paypal",
        "name": "PayPal",
        "account_type": "paypal",
        "is_active": True,
        "is_liability": False,
    },
    {
        "key": "wallet",
        "name": "Portemonnaie",
        "account_type": "cash_wallet",
        "is_active": True,
        "is_liability": False,
    },
    {
        "key": "vault",
        "name": "Tresor",
        "account_type": "cash_vault",
        "is_active": True,
        "is_liability": False,
    },
    {
        "key": "c24",
        "name": "C24",
        "account_type": "checking",
        "is_active": False,
        "is_liability": False,
    },
]
ENVELOPE_NAMES = [
    "Kleidung",
    "Vergnügen",
    "Haushalt",
    "Mietrücklage",
    "Urlaub",
    "Geschenke",
    "Dezembergeld",
    "Zahnarztgeld",
    "Unterstützung Brüder",
    "Sparen",
    "Noée",
    "Fix",
]


def _write_synthetic_master_data(settings: Settings) -> None:
    envelopes = []
    for index, name in enumerate(ENVELOPE_NAMES):
        if name == "Zahnarztgeld":
            envelopes.append(
                {
                    "name": name,
                    "baseline": "300",
                    "target_rule_type": "target_balance",
                    "target_amount": "500",
                    "rules": [
                        {
                            "valid_from": "2026-01-01",
                            "valid_to": None,
                            "monthly_amount": None,
                            "rule_type": "target_balance",
                            "target_amount": "500",
                        }
                    ],
                }
            )
        else:
            envelopes.append(
                {
                    "name": name,
                    "baseline": str((index + 1) * 10),
                    "target_rule_type": "monthly_contribution",
                    "target_amount": None,
                    "rules": [
                        {
                            "valid_from": "2026-01-01",
                            "valid_to": "2026-05-31",
                            "monthly_amount": "10",
                            "rule_type": "monthly_contribution",
                            "target_amount": None,
                        },
                        {
                            "valid_from": "2026-06-01",
                            "valid_to": None,
                            "monthly_amount": "15",
                            "rule_type": "monthly_contribution",
                            "target_amount": None,
                        },
                    ],
                }
            )
    path = settings.profile_private_data_dir / "master_data.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"accounts": ACCOUNT_SPECS, "envelopes": envelopes}), encoding="utf-8"
    )


@pytest.fixture
def private_store(tmp_path: Path) -> tuple[Settings, sessionmaker[Session]]:
    database_url = f"sqlite:///{(tmp_path / 'private.db').as_posix()}"
    settings = Settings(
        private_database_url=database_url,
        demo_mode=False,
        private_data_dir=tmp_path / "private",
        backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
    )
    _write_synthetic_master_data(settings)
    upgrade_database(database_url)
    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield settings, factory
    engine.dispose()


def _source(
    identity: str,
    *,
    booked_at: datetime,
    amount: str,
    balance: str,
    semantic: str,
    target_account_type: str | None = None,
) -> SourceTransaction:
    return SourceTransaction(
        source_system="sparda",
        source_transaction_id=identity,
        booked_at=booked_at,
        value_at=booked_at,
        merchant_raw="Synthetischer Empfänger",
        description_raw="Synthetischer Testumsatz",
        amount=D(amount),
        currency="EUR",
        source_account_hint="Testkonto",
        balance_after=D(balance),
        metadata_json={
            "sparda_semantic": semantic,
            "target_account_type": target_account_type,
        },
        fingerprint=(identity * 64)[:64],
    )


def test_private_master_data_is_idempotent_and_matches_confirmed_rules(
    private_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = private_store
    with factory.begin() as db:
        first = initialize_private_profile(db, settings)
    with factory.begin() as db:
        second = initialize_private_profile(db, settings)

    assert first.accounts_created == 6
    assert first.envelopes_created == 12
    assert first.snapshots_created == 12
    assert first.rules_created == 23
    assert second.accounts_created == 0
    assert second.envelopes_created == 0
    assert second.snapshots_created == 0
    assert second.rules_created == 0

    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Account)) == 6
        assert db.scalar(select(func.count()).select_from(Envelope)) == 12
        assert db.scalar(select(func.count()).select_from(EnvelopeSnapshot)) == 12
        assert db.scalar(select(func.count()).select_from(EnvelopeRulePeriod)) == 23
        baseline = db.scalar(select(func.sum(EnvelopeSnapshot.physical_balance)))
        assert baseline == D("1000.00")
        dentist = db.scalar(select(Envelope).where(Envelope.name == "Zahnarztgeld"))
        dentist_rule = db.scalar(
            select(EnvelopeRulePeriod).where(EnvelopeRulePeriod.envelope_id == dentist.id)
        )
        assert dentist.target_rule_type == "target_balance"
        assert dentist.target_amount == D("500.00")
        assert dentist_rule.monthly_amount is None
        assert dentist_rule.target_amount == D("500.00")
        unknown = list(db.scalars(select(Account).where(Account.name != "Sparda Girokonto")))
        assert all(not account.balance_confirmed for account in unknown)


def test_sparda_backfill_links_accounts_without_mutating_source_rows(
    private_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = private_store
    with factory.begin() as db:
        expense = _source(
            "a",
            booked_at=datetime(2026, 4, 30, 12),
            amount="-10",
            balance="1000",
            semantic="categorized_expense",
        )
        amex = _source(
            "b",
            booked_at=datetime(2026, 5, 1, 12),
            amount="-222.22",
            balance="950",
            semantic="american_express_settlement",
        )
        paypal = _source(
            "c",
            booked_at=datetime(2026, 5, 2, 12),
            amount="-25",
            balance="925",
            semantic="paypal_funding_leg",
        )
        review_only = _source(
            "d",
            booked_at=datetime(2026, 5, 3, 12),
            amount="-12",
            balance="913",
            semantic="ambiguous_debit",
        )
        db.add_all([expense, amex, paypal, review_only])
        db.flush()
        for source, event_type in ((expense, "expense"), (amex, "transfer"), (paypal, "transfer")):
            event = EconomicEvent(
                event_type=event_type,
                occurred_at=source.booked_at,
                description="Synthetisches Ereignis",
                amount=abs(source.amount),
                currency="EUR",
                confidence=D("1"),
            )
            db.add(event)
            db.flush()
            db.add(
                EventSourceLink(
                    economic_event_id=event.id,
                    source_transaction_id=source.id,
                    link_type="canonical_source" if event_type == "expense" else "settlement_leg",
                    confidence=D("1"),
                )
            )
        db.add(
            ReviewItem(
                source_transaction_id=review_only.id,
                review_type="economic_type",
                proposed_event_type="expense",
                confidence=D("0.55"),
                explanation="Synthetischer Prüfgrund",
            )
        )
        original = {
            source.id: (source.amount, source.balance_after, source.metadata_json.copy())
            for source in (expense, amex, paypal, review_only)
        }

    with factory.begin() as db:
        result = initialize_private_profile(db, settings)

    assert result.source_links_created == 4
    assert result.events_backfilled == 3
    assert result.transfer_targets_linked == 2
    assert result.sparda_balance_confirmed
    with factory() as db:
        sparda = db.scalar(select(Account).where(Account.name == "Sparda Girokonto"))
        assert sparda.balance == D("913.00")
        assert sparda.balance_confirmed
        assert db.scalar(select(func.count()).select_from(SourceTransactionAccount)) == 6
        events = list(db.scalars(select(EconomicEvent).order_by(EconomicEvent.id)))
        assert all(event.account_id == sparda.id for event in events)
        assert events[1].source_account.name == "Sparda Girokonto"
        assert events[1].target_account.name == "American Express"
        assert events[2].target_account.name == "PayPal"
        assert db.scalar(select(func.count()).select_from(EconomicEvent)) == 3
        review = db.scalar(select(ReviewItem))
        assert review.economic_event_id is None
        for source in db.scalars(select(SourceTransaction)):
            assert (source.amount, source.balance_after, source.metadata_json) == original[
                source.id
            ]

        dashboard = build_dashboard(db)
        assert dashboard["wealth"] == D("913.00")
        assert dashboard["reserved"] == D("1000.00")
        assert dashboard["wealth_has_unknown"]


def test_private_initializer_refuses_demo_profile(
    private_store: tuple[Settings, sessionmaker[Session]],
) -> None:
    settings, factory = private_store
    demo = settings.model_copy(update={"demo_mode": True})
    with factory.begin() as db, pytest.raises(ValueError, match="private profile"):
        initialize_private_profile(db, demo)


def test_review_and_transfer_views_are_explicit_and_redacted(
    private_store: tuple[Settings, sessionmaker[Session]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, factory = private_store
    with factory.begin() as db:
        transfer_source = _source(
            "t",
            booked_at=datetime(2026, 5, 4, 12),
            amount="-123.45",
            balance="2000",
            semantic="american_express_settlement",
        )
        review_source = _source(
            "r",
            booked_at=datetime(2026, 5, 5, 12),
            amount="-42",
            balance="1958",
            semantic="ambiguous_debit",
        )
        review_source.merchant_raw = "Synthetischer Empfänger"
        db.add_all([transfer_source, review_source])
        db.flush()
        transfer = EconomicEvent(
            event_type="transfer",
            occurred_at=transfer_source.booked_at,
            description="American-Express-Abrechnung",
            amount=D("123.45"),
            currency="EUR",
            confidence=D("1"),
        )
        db.add(transfer)
        db.flush()
        db.add(
            EventSourceLink(
                economic_event_id=transfer.id,
                source_transaction_id=transfer_source.id,
                link_type="settlement_leg",
                confidence=D("1"),
            )
        )
        review = ReviewItem(
            source_transaction_id=review_source.id,
            review_type="economic_type",
            proposed_event_type="expense",
            confidence=D("0.55"),
            explanation="Synthetischer Prüfgrund",
        )
        db.add(review)
        initialize_private_profile(db, settings)

    with factory() as db:
        persisted = db.scalar(select(ReviewItem))
        persisted.source_transaction = db.get(SourceTransaction, persisted.source_transaction_id)
        view = build_review_view(persisted)
        assert view.proposed_type == "Vorschlag: Ausgabe"
        assert view.payee == "Synthetischer Empfänger"

    def override_db():
        with factory() as db:
            yield db

    monkeypatch.setattr("app.web.routes.get_settings", lambda: settings)
    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            accounts_page = client.get("/accounts").text
            transactions_page = client.get("/transactions").text
            review_page = client.get("/review").text
    finally:
        app.dependency_overrides.clear()

    assert "Sparda Girokonto" in accounts_page
    assert "Nicht abgestimmt" in accounts_page
    assert "Sparda → American Express" in transactions_page
    assert "+123,45" not in transactions_page
    assert "Vorschlag: Ausgabe" in review_page
    assert "Synthetischer Prüfgrund" in review_page
    assert "Sparda Girokonto" in review_page

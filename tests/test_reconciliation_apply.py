from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.migrations import upgrade_database
from app.db.models import (
    AmazonEnrichmentRecord,
    AmazonPaymentMatch,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    RawImportRecord,
    SourceTransaction,
)
from app.services.reconciliation_apply import (
    apply_reconciliation_writes,
    plan_reconciliation_writes,
)

D = Decimal


def _factory(tmp_path: Path) -> tuple[sessionmaker[Session], object]:
    database = tmp_path / "moneyos-apply.sqlite"
    url = f"sqlite:///{database.as_posix()}"
    upgrade_database(url)
    engine = create_engine(url)
    return sessionmaker(bind=engine, expire_on_commit=False), engine


def _source(
    system: str,
    source_id: str,
    amount: str,
    booked_at: datetime,
    *,
    merchant: str = "",
    description: str = "",
    metadata: dict | None = None,
) -> SourceTransaction:
    fingerprint = (source_id.replace("-", "") + "0" * 64)[:64]
    return SourceTransaction(
        source_system=system,
        source_transaction_id=source_id,
        booked_at=booked_at,
        value_at=booked_at,
        merchant_raw=merchant or None,
        description_raw=description,
        amount=D(amount),
        currency="EUR",
        status="booked",
        metadata_json=metadata or {},
        fingerprint=fingerprint,
    )


def test_paypal_sparda_high_confidence_is_planned_and_idempotent(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            paypal_event = EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 9, 24),
                description="Merchant",
                amount=D("10.00"),
                currency="EUR",
                status="booked",
                confidence=D("0.95"),
            )
            sparda_event = EconomicEvent(
                event_type="transfer",
                occurred_at=datetime(2026, 9, 24),
                description="PayPal-Finanzierung",
                amount=D("10.00"),
                currency="EUR",
                status="booked",
                confidence=D("1.00"),
            )
            db.add_all([paypal_event, sparda_event])
            db.flush()

            paypal = _source(
                "paypal",
                "paypal-funding",
                "10.00",
                datetime(2026, 9, 24, 12, 0),
                description="Bankgutschrift auf PayPal-Konto",
                metadata={"paypal_semantic": "funding"},
            )
            sparda = _source(
                "sparda",
                "sparda-paypal",
                "-10.00",
                datetime(2026, 9, 24),
                merchant="PayPal Europe",
                metadata={"sparda_semantic": "paypal_funding_leg"},
            )
            db.add_all([paypal, sparda])
            db.flush()
            db.add_all(
                [
                    EventSourceLink(
                        economic_event_id=paypal_event.id,
                        source_transaction_id=paypal.id,
                        link_type="funding_leg",
                        confidence=D("0.95"),
                    ),
                    EventSourceLink(
                        economic_event_id=sparda_event.id,
                        source_transaction_id=sparda.id,
                        link_type="funding_leg",
                        confidence=D("1.00"),
                    ),
                ]
            )

        with factory() as db:
            plan = plan_reconciliation_writes(db)
            assert plan.action_count == 1
            assert plan.actions[0].kind == "paypal_sparda_funding"

        with factory.begin() as db:
            first = apply_reconciliation_writes(db)
        assert first.event_source_links_created == 1

        with factory.begin() as db:
            second = apply_reconciliation_writes(db)
        assert second.event_source_links_created == 0
    finally:
        engine.dispose()


def test_paypal_amex_high_confidence_is_blocked(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            paypal_event = EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 9, 25),
                description="Vinted",
                amount=D("7.90"),
                currency="EUR",
                status="booked",
                confidence=D("0.95"),
            )
            amex_event = EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 9, 25),
                description="PAYPAL *VINTED",
                amount=D("7.90"),
                currency="EUR",
                status="booked",
                confidence=D("0.95"),
            )
            db.add_all([paypal_event, amex_event])
            db.flush()

            paypal = _source(
                "paypal",
                "paypal-card-funding",
                "7.90",
                datetime(2026, 9, 25, 7, 29),
                description="Allgemeine Gutschrift auf Kreditkarte",
                metadata={"paypal_semantic": "funding"},
            )
            amex = _source(
                "amex",
                "amex-vinted",
                "7.90",
                datetime(2026, 9, 25),
                merchant="PAYPAL *VINTED",
                metadata={"amex_semantic": "merchant_purchase"},
            )
            db.add_all([paypal, amex])
            db.flush()
            db.add_all(
                [
                    EventSourceLink(
                        economic_event_id=paypal_event.id,
                        source_transaction_id=paypal.id,
                        link_type="funding_leg",
                        confidence=D("0.95"),
                    ),
                    EventSourceLink(
                        economic_event_id=amex_event.id,
                        source_transaction_id=amex.id,
                        link_type="canonical_source",
                        confidence=D("0.95"),
                    ),
                ]
            )

        with factory() as db:
            plan = plan_reconciliation_writes(db)
        assert plan.action_count == 0
        assert plan.blocked_paypal_amex == 1
    finally:
        engine.dispose()


def test_amazon_high_confidence_group_is_persisted_once(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            batch = ImportBatch(
                source_type="amazon",
                filename="amazon.zip",
                source_hash="a" * 64,
                status="imported",
                row_count=2,
                metadata_json={},
                validation_json={},
            )
            db.add(batch)
            db.flush()

            records = []
            for index, amount in enumerate(("7.55", "1.44"), start=1):
                raw = RawImportRecord(
                    import_batch_id=batch.id,
                    source_row_key=f"row-{index}",
                    raw_payload_json={},
                    raw_text="synthetic",
                    raw_hash=(str(index) * 64)[:64],
                )
                db.add(raw)
                db.flush()
                record = AmazonEnrichmentRecord(
                    import_batch_id=batch.id,
                    raw_record_id=raw.id,
                    record_type="digital_order_item",
                    natural_key=(str(index + 2) * 64)[:64],
                    content_hash=(str(index + 4) * 64)[:64],
                    order_key="f" * 64,
                    item_key=(str(index + 6) * 64)[:64],
                    occurred_at=datetime(2026, 10, 1, 15, 58),
                    amount=D(amount),
                    currency="EUR",
                    product_title="Prime Membership Fee",
                    payment_method="Payment Instrument Details Not Available",
                    metadata_json={},
                )
                db.add(record)
                records.append(record)

            event = EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 10, 1),
                description="AMZNPRIME",
                amount=D("8.99"),
                currency="EUR",
                status="booked",
                confidence=D("0.95"),
            )
            db.add(event)
            db.flush()
            amex = _source(
                "amex",
                "amex-prime",
                "8.99",
                datetime(2026, 10, 1),
                merchant="AMZNPRIME DE",
                metadata={"amex_semantic": "merchant_purchase"},
            )
            db.add(amex)
            db.flush()
            db.add(
                EventSourceLink(
                    economic_event_id=event.id,
                    source_transaction_id=amex.id,
                    link_type="canonical_source",
                    confidence=D("0.95"),
                )
            )

        with factory() as db:
            plan = plan_reconciliation_writes(db)
            amazon_actions = [action for action in plan.actions if action.kind == "amazon_payment"]
            assert len(amazon_actions) == 1
            assert len(amazon_actions[0].amazon_record_ids) == 2

        with factory.begin() as db:
            first = apply_reconciliation_writes(db)
        assert first.amazon_payment_matches_created == 2

        with factory.begin() as db:
            second = apply_reconciliation_writes(db)
        assert second.amazon_payment_matches_created == 0

        with factory() as db:
            count = db.scalar(select(func.count()).select_from(AmazonPaymentMatch))
        assert count == 2
    finally:
        engine.dispose()

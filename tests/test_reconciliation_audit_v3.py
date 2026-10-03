from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.migrations import upgrade_database
from app.db.models import (
    AmazonEnrichmentRecord,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    RawImportRecord,
    SourceTransaction,
)
from app.services.reconciliation_audit import audit_reconciliation

D = Decimal


def _factory(tmp_path: Path) -> tuple[sessionmaker[Session], object]:
    database = tmp_path / "moneyos-v3.sqlite"
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


def test_amex_settlement_diagnostics_accept_sparda_semantic(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            db.add(
                _source(
                    "amex",
                    "amex-statement-payment",
                    "-500.00",
                    datetime(2026, 9, 15),
                    merchant="PAYMENT RECEIVED",
                    metadata={"amex_semantic": "statement_payment"},
                )
            )
            db.add(
                _source(
                    "sparda",
                    "sparda-amex-settlement",
                    "-500.00",
                    datetime(2026, 9, 15),
                    merchant="American Express",
                    description="Kreditkartenabrechnung",
                    metadata={"sparda_semantic": "american_express_settlement"},
                )
            )

        with factory() as db:
            report = audit_reconciliation(db)

        matches = [
            candidate
            for candidate in report.candidates or []
            if candidate.kind == "amex_sparda_settlement"
        ]
        assert report.amex_statement_payment_rows == 1
        assert report.sparda_amex_settlement_rows == 1
        assert report.existing_amex_settlement_links == 0
        assert len(matches) == 1
        assert matches[0].confidence == "high"
    finally:
        engine.dispose()


def test_amazon_nm_order_matches_two_payment_events(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            batch = ImportBatch(
                source_type="amazon",
                filename="nm.zip",
                source_hash="8" * 64,
                status="imported",
                row_count=1,
                metadata_json={},
                validation_json={},
            )
            db.add(batch)
            db.flush()

            raw = RawImportRecord(
                import_batch_id=batch.id,
                source_row_key="nm-order",
                raw_payload_json={"Total Amount": "30.00"},
                raw_text="nm",
                raw_hash="7" * 64,
            )
            db.add(raw)
            db.flush()
            db.add(
                AmazonEnrichmentRecord(
                    import_batch_id=batch.id,
                    raw_record_id=raw.id,
                    record_type="order_item",
                    natural_key="6" * 64,
                    content_hash="5" * 64,
                    order_key="4" * 64,
                    item_key="3" * 64,
                    occurred_at=datetime(2026, 10, 1, 12, 0),
                    amount=D("30.00"),
                    currency="EUR",
                    product_title="Split order",
                    payment_method="American Express",
                    metadata_json={},
                )
            )

            for index, amount in enumerate(("10.00", "20.00"), start=1):
                event = EconomicEvent(
                    event_type="expense",
                    occurred_at=datetime(2026, 10, 1),
                    description=f"AMZN SPLIT {index}",
                    amount=D(amount),
                    currency="EUR",
                    status="booked",
                    confidence=D("0.95"),
                )
                db.add(event)
                db.flush()
                source = _source(
                    "amex",
                    f"amex-amazon-split-{index}",
                    amount,
                    datetime(2026, 10, 1),
                    merchant="AMZN MKTP DE",
                    metadata={"amex_semantic": "merchant_purchase"},
                )
                db.add(source)
                db.flush()
                db.add(
                    EventSourceLink(
                        economic_event_id=event.id,
                        source_transaction_id=source.id,
                        link_type="canonical_source",
                        confidence=D("0.95"),
                    )
                )

        with factory() as db:
            report = audit_reconciliation(db)

        matches = [
            candidate
            for candidate in report.candidates or []
            if candidate.kind == "amazon_payment_nm"
        ]
        assert report.amazon_nm_candidates == 1
        assert len(matches) == 1
        assert matches[0].confidence == "medium"
        assert matches[0].amount == "30.00"
    finally:
        engine.dispose()


def test_existing_amex_settlement_link_is_reported(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            event = EconomicEvent(
                event_type="transfer",
                occurred_at=datetime(2026, 9, 15),
                description="American-Express-Abrechnung",
                amount=D("500.00"),
                currency="EUR",
                status="booked",
                confidence=D("0.99"),
            )
            db.add(event)
            db.flush()
            amex = _source(
                "amex",
                "amex-existing-settlement",
                "-500.00",
                datetime(2026, 9, 15),
                merchant="PAYMENT RECEIVED",
                metadata={"amex_semantic": "statement_payment"},
            )
            db.add(amex)
            db.flush()
            db.add(
                EventSourceLink(
                    economic_event_id=event.id,
                    source_transaction_id=amex.id,
                    link_type="settlement_leg",
                    confidence=D("0.99"),
                )
            )

        with factory() as db:
            report = audit_reconciliation(db)

        assert report.amex_statement_payment_rows == 1
        assert report.existing_amex_settlement_links == 1
        assert report.amex_sparda_settlement == 0
    finally:
        engine.dispose()

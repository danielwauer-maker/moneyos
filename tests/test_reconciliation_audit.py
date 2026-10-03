from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func, select
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
    database = tmp_path / "moneyos.sqlite"
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


def test_paypal_credit_card_funding_matches_amex_without_mutation(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            db.add(
                _source(
                    "paypal",
                    "paypal-funding-1",
                    "7.90",
                    datetime(2026, 9, 25, 7, 29),
                    description="Allgemeine Gutschrift auf Kreditkarte",
                    metadata={"paypal_semantic": "funding"},
                )
            )
            db.add(
                _source(
                    "amex",
                    "amex-vinted-1",
                    "7.90",
                    datetime(2026, 9, 25),
                    merchant="PAYPAL *VINTED",
                    metadata={"amex_semantic": "merchant_purchase"},
                )
            )

        with factory() as db:
            before = db.scalar(select(func.count()).select_from(SourceTransaction))
            report = audit_reconciliation(db)
            after = db.scalar(select(func.count()).select_from(SourceTransaction))

        matches = [
            candidate for candidate in report.candidates or [] if candidate.kind == "paypal_amex"
        ]
        assert before == after == 2
        assert len(matches) == 1
        assert matches[0].confidence == "high"
        assert matches[0].amount == "7.90"
    finally:
        engine.dispose()


def test_amazon_split_group_matches_single_amex_event(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            batch = ImportBatch(
                source_type="amazon",
                filename="synthetic.zip",
                source_hash="a" * 64,
                status="imported",
                row_count=2,
                metadata_json={},
                validation_json={},
            )
            db.add(batch)
            db.flush()

            raws = []
            for index in range(2):
                raw = RawImportRecord(
                    import_batch_id=batch.id,
                    source_row_key=f"row-{index}",
                    raw_payload_json={},
                    raw_text="synthetic",
                    raw_hash=(str(index + 1) * 64)[:64],
                )
                db.add(raw)
                db.flush()
                raws.append(raw)

            db.add_all(
                [
                    AmazonEnrichmentRecord(
                        import_batch_id=batch.id,
                        raw_record_id=raws[0].id,
                        record_type="digital_order_item",
                        natural_key="b" * 64,
                        content_hash="c" * 64,
                        order_key="d" * 64,
                        item_key="e" * 64,
                        occurred_at=datetime(2026, 10, 1, 15, 58),
                        amount=D("7.55"),
                        currency="EUR",
                        product_title="Prime Membership Fee",
                        payment_method="Payment Instrument Details Not Available",
                        metadata_json={},
                    ),
                    AmazonEnrichmentRecord(
                        import_batch_id=batch.id,
                        raw_record_id=raws[1].id,
                        record_type="digital_order_item",
                        natural_key="f" * 64,
                        content_hash="1" * 64,
                        order_key="d" * 64,
                        item_key="2" * 64,
                        occurred_at=datetime(2026, 10, 1, 15, 58),
                        amount=D("1.44"),
                        currency="EUR",
                        product_title="Prime Membership Fee",
                        payment_method="Payment Instrument Details Not Available",
                        metadata_json={},
                    ),
                ]
            )
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
            source = _source(
                "amex",
                "amex-amazon-prime",
                "8.99",
                datetime(2026, 10, 1),
                merchant="AMZNPRIME DE",
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
            candidate for candidate in report.candidates or [] if candidate.kind == "amazon_payment"
        ]
        assert len(matches) == 1
        assert matches[0].confidence == "high"
        assert matches[0].amount == "8.99"
        assert "2 Evidenzzeile(n)" in matches[0].detail
    finally:
        engine.dispose()


def test_amazon_duplicate_refund_evidence_is_not_summed(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            batch = ImportBatch(
                source_type="amazon",
                filename="refund.zip",
                source_hash="9" * 64,
                status="imported",
                row_count=2,
                metadata_json={},
                validation_json={},
            )
            db.add(batch)
            db.flush()

            records = []
            for index in range(2):
                raw = RawImportRecord(
                    import_batch_id=batch.id,
                    source_row_key=f"refund-{index}",
                    raw_payload_json={"Refund Amount": "14.39"},
                    raw_text="refund",
                    raw_hash=(str(index + 3) * 64)[:64],
                )
                db.add(raw)
                db.flush()
                record = AmazonEnrichmentRecord(
                    import_batch_id=batch.id,
                    raw_record_id=raw.id,
                    record_type="refund",
                    natural_key=(str(index + 4) * 64)[:64],
                    content_hash=(str(index + 6) * 64)[:64],
                    order_key="a" * 64,
                    item_key=(str(index + 8) * 64)[:64],
                    occurred_at=datetime(2026, 9, 30, 12 + index, 30),
                    amount=D("14.39"),
                    currency="EUR",
                    product_title=None,
                    payment_method=None,
                    metadata_json={},
                )
                db.add(record)
                records.append(record)

            event = EconomicEvent(
                event_type="refund",
                occurred_at=datetime(2026, 9, 30),
                description="AMZN MKTP DE",
                amount=D("14.39"),
                currency="EUR",
                status="booked",
                confidence=D("0.95"),
            )
            db.add(event)
            db.flush()
            source = _source(
                "amex",
                "amex-amazon-refund",
                "-14.39",
                datetime(2026, 9, 30),
                merchant="AMZN MKTP DE",
                metadata={"amex_semantic": "refund"},
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

        refunds = [
            candidate for candidate in report.candidates or [] if candidate.kind == "amazon_refund"
        ]
        assert len(refunds) == 1
        assert refunds[0].amount == "14.39"
        assert refunds[0].confidence == "high"
        assert "2 gleichartige Refund-Evidenzen" in refunds[0].detail
    finally:
        engine.dispose()

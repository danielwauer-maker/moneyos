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
from app.services.reconciliation_plan_report import reconciliation_plan_report

D = Decimal


def _factory(tmp_path: Path) -> tuple[sessionmaker[Session], object]:
    database = tmp_path / "moneyos-plan-report.sqlite"
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


def test_report_explains_unsafe_paypal_sparda_candidate(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            paypal_event = EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 1, 5),
                description="Merchant",
                amount=D("10.00"),
                currency="EUR",
                status="booked",
                confidence=D("0.95"),
            )
            db.add(paypal_event)
            db.flush()
            paypal = _source(
                "paypal",
                "paypal-funding-unsafe",
                "10.00",
                datetime(2026, 1, 5, 12, 0),
                description="Bankgutschrift",
                metadata={"paypal_semantic": "funding"},
            )
            sparda = _source(
                "sparda",
                "sparda-funding-unsafe",
                "-10.00",
                datetime(2026, 1, 5),
                merchant="PayPal Europe",
                metadata={"sparda_semantic": "ambiguous_debit"},
            )
            db.add_all([paypal, sparda])
            db.flush()
            db.add(
                EventSourceLink(
                    economic_event_id=paypal_event.id,
                    source_transaction_id=paypal.id,
                    link_type="funding_leg",
                    confidence=D("0.95"),
                )
            )

        with factory() as db:
            report = reconciliation_plan_report(db)

        assert report["skipped_unsafe"] == 1
        details = report["skipped_unsafe_details"]
        assert len(details) == 1
        assert details[0]["kind"] == "paypal_sparda"
        assert "paypal_funding_leg" in details[0]["reason"]
        assert "ambiguous_debit" in details[0]["reason"]
    finally:
        engine.dispose()


def test_report_enriches_amazon_action_with_human_readable_context(tmp_path: Path) -> None:
    factory, engine = _factory(tmp_path)
    try:
        with factory.begin() as db:
            batch = ImportBatch(
                source_type="amazon",
                filename="amazon.zip",
                source_hash="b" * 64,
                status="imported",
                row_count=1,
                metadata_json={},
                validation_json={},
            )
            db.add(batch)
            db.flush()
            raw = RawImportRecord(
                import_batch_id=batch.id,
                source_row_key="row-1",
                raw_payload_json={},
                raw_text="synthetic",
                raw_hash="c" * 64,
            )
            db.add(raw)
            db.flush()
            amazon = AmazonEnrichmentRecord(
                import_batch_id=batch.id,
                raw_record_id=raw.id,
                record_type="order_item",
                natural_key="d" * 64,
                content_hash="e" * 64,
                order_key="f" * 64,
                item_key="1" * 64,
                occurred_at=datetime(2026, 1, 16, 15, 7),
                amount=D("16.98"),
                currency="EUR",
                product_title="Amazon Test Item",
                payment_method="American Express",
                metadata_json={},
            )
            db.add(amazon)

            event = EconomicEvent(
                event_type="expense",
                occurred_at=datetime(2026, 1, 16),
                description="AMZN MKTP DE",
                amount=D("16.98"),
                currency="EUR",
                status="booked",
                confidence=D("0.95"),
            )
            db.add(event)
            db.flush()
            amex = _source(
                "amex",
                "amex-amazon-16-98",
                "16.98",
                datetime(2026, 1, 16),
                merchant="AMZN MKTP DE",
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
            report = reconciliation_plan_report(db)

        assert report["action_count"] == 1
        action = report["actions"][0]
        assert action["amount"] == "16.98"
        assert action["payment_source"] == "amex"
        assert action["merchant"] == "AMZN MKTP DE"
        assert action["event_description"] == "AMZN MKTP DE"
        assert action["amazon_evidence"][0]["product_title"] == "Amazon Test Item"
        assert action["amazon_evidence"][0]["payment_method"] == "American Express"
    finally:
        engine.dispose()

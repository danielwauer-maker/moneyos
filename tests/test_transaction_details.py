from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    RawImportRecord,
    SourceTransaction,
)
from app.services.transaction_details import derive_transaction_detail, event_transaction_views


@pytest.fixture
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path}/transaction-details.db")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _source_with_raw(
    db: Session, *, counterparty: str, purpose: str
) -> tuple[SourceTransaction, RawImportRecord]:
    batch = ImportBatch(
        source_type="sparda",
        filename="synthetic.csv",
        source_hash="batch-detail".ljust(64, "0"),
        status="imported",
    )
    db.add(batch)
    db.flush()
    raw = RawImportRecord(
        import_batch_id=batch.id,
        source_row_key="synthetic-row",
        raw_payload_json={
            "Name Zahlungsbeteiligter": counterparty,
            "Buchungstext": "Kartenzahlung Debit MC",
            "Verwendungszweck": purpose,
            "Bemerkung": "",
        },
        raw_text="synthetic only",
        raw_hash="detail".ljust(64, "0"),
    )
    db.add(raw)
    db.flush()
    source = SourceTransaction(
        import_batch_id=batch.id,
        raw_record_id=raw.id,
        source_system="sparda",
        source_transaction_id="sparda:synthetic-detail",
        booked_at=datetime(2026, 9, 1),
        merchant_raw=counterparty,
        description_raw=f"Kartenzahlung Debit MC | {purpose}",
        amount=Decimal("-12.34"),
        fingerprint="source-detail".ljust(64, "0"),
    )
    db.add(source)
    db.flush()
    return source, raw


def test_derived_detail_does_not_modify_immutable_source(db: Session) -> None:
    source, raw = _source_with_raw(
        db,
        counterparty="DZ BANK AG",
        purpose="SYNTHETIC BAGERI/Example Road/Example/DK/2",
    )
    original = (source.merchant_raw, source.description_raw, raw.raw_payload_json.copy())

    detail = derive_transaction_detail(source, raw)

    assert detail.raw_counterparty == "DZ BANK AG"
    assert detail.secondary_detail == "SYNTHETIC BAGERI/Example Road/Example/DK/2"
    assert detail.canonical_merchant == "SYNTHETIC BAGERI"
    assert (source.merchant_raw, source.description_raw, raw.raw_payload_json) == original


def test_event_view_retains_raw_counterparty_and_secondary_detail(db: Session) -> None:
    source, _ = _source_with_raw(
        db,
        counterparty="toom BM Beispielstadt",
        purpose="TOOM BM SAGT DANKE 99999999",
    )
    event = EconomicEvent(
        event_type="expense",
        occurred_at=source.booked_at,
        description="toom BM Beispielstadt",
        amount=Decimal("12.34"),
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

    view = event_transaction_views(db, [event])[0]

    assert view.detail.raw_counterparty == "toom BM Beispielstadt"
    assert view.detail.secondary_detail == "TOOM BM SAGT DANKE 99999999"
    assert view.detail.canonical_merchant == "toom BM Beispielstadt"

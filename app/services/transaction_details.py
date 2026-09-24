from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EconomicEvent, EventSourceLink, RawImportRecord, SourceTransaction
from app.security.redaction import redact_text
from app.services.sparda_reclassification import source_classification


@dataclass(frozen=True)
class TransactionDetail:
    raw_counterparty: str
    processor: str | None
    secondary_detail: str
    canonical_merchant: str
    canonical_confidence: str
    canonical_reason: str


@dataclass(frozen=True)
class EventTransactionView:
    event: EconomicEvent
    detail: TransactionDetail


def derive_transaction_detail(
    source: SourceTransaction | None,
    raw: RawImportRecord | None = None,
    *,
    fallback: str = "Quelltransaktion",
) -> TransactionDetail:
    if source is None:
        safe = redact_text(fallback)[:160]
        return TransactionDetail(safe, None, "", safe, "unknown", "Keine Quelltransaktion")
    raw_counterparty = redact_text(source.merchant_raw or fallback)[:160]
    if source.source_system == "amex":
        canonical = (source.metadata_json or {}).get("canonical_merchant") or raw_counterparty
        return TransactionDetail(
            raw_counterparty=raw_counterparty,
            processor=None,
            secondary_detail=redact_text(source.description_raw or "")[:240],
            canonical_merchant=redact_text(canonical)[:160],
            canonical_confidence="high",
            canonical_reason="Direkter Amex-Händlertext",
        )
    if source.source_system != "sparda":
        return TransactionDetail(
            raw_counterparty=raw_counterparty,
            processor=None,
            secondary_detail=redact_text(source.description_raw or "")[:240],
            canonical_merchant=raw_counterparty,
            canonical_confidence="unknown",
            canonical_reason="Keine Sparda-Ableitung",
        )
    decision = source_classification(source, raw)
    merchant = decision.merchant
    if merchant is None:  # defensive; the Sparda classifier always supplies one
        return TransactionDetail(raw_counterparty, None, "", raw_counterparty, "unknown", "Offen")
    return TransactionDetail(
        raw_counterparty=redact_text(merchant.raw_counterparty or raw_counterparty)[:160],
        processor=redact_text(merchant.processor)[:160] if merchant.processor else None,
        secondary_detail=redact_text(merchant.secondary_detail)[:240],
        canonical_merchant=redact_text(merchant.canonical_merchant)[:160],
        canonical_confidence=str(merchant.confidence),
        canonical_reason=merchant.reason,
    )


def raw_records_by_id(db: Session, sources: list[SourceTransaction]) -> dict[int, RawImportRecord]:
    raw_ids = {source.raw_record_id for source in sources if source.raw_record_id is not None}
    if not raw_ids:
        return {}
    return {
        raw.id: raw
        for raw in db.scalars(select(RawImportRecord).where(RawImportRecord.id.in_(raw_ids)))
    }


def event_transaction_views(
    db: Session, events: list[EconomicEvent]
) -> tuple[EventTransactionView, ...]:
    event_ids = [event.id for event in events]
    if not event_ids:
        return ()
    links = list(
        db.scalars(
            select(EventSourceLink)
            .where(EventSourceLink.economic_event_id.in_(event_ids))
            .order_by(EventSourceLink.economic_event_id, EventSourceLink.id)
        )
    )
    source_ids = {link.source_transaction_id for link in links}
    sources = list(
        db.scalars(select(SourceTransaction).where(SourceTransaction.id.in_(source_ids)))
    )
    sources_by_id = {source.id: source for source in sources}
    raw_by_id = raw_records_by_id(db, sources)
    source_by_event: dict[int, SourceTransaction] = {}
    for link in links:
        source = sources_by_id.get(link.source_transaction_id)
        if source is not None:
            current = source_by_event.get(link.economic_event_id)
            if current is None or (
                current.source_system != "sparda" and source.source_system == "sparda"
            ):
                source_by_event[link.economic_event_id] = source
    return tuple(
        EventTransactionView(
            event=event,
            detail=derive_transaction_detail(
                source_by_event.get(event.id),
                raw_by_id.get(source_by_event[event.id].raw_record_id)
                if event.id in source_by_event
                else None,
                fallback=event.description,
            ),
        )
        for event in events
    )

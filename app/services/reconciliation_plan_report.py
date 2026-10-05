from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AmazonEnrichmentRecord, EconomicEvent, EventSourceLink, SourceTransaction
from app.services.reconciliation_apply import ReconciliationWriteAction, plan_reconciliation_writes
from app.services.reconciliation_audit import ReconciliationCandidate, audit_reconciliation


@dataclass(frozen=True)
class ReconciliationSkipDetail:
    kind: str
    source_id: int | None
    target_id: int | None
    amount: str
    occurred_at: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _canonical_source(db: Session, event_id: int) -> SourceTransaction | None:
    link = db.scalar(
        select(EventSourceLink).where(
            EventSourceLink.economic_event_id == event_id,
            EventSourceLink.link_type == "canonical_source",
        )
    )
    return db.get(SourceTransaction, link.source_transaction_id) if link is not None else None


def _event_for_source(db: Session, source_id: int) -> EconomicEvent | None:
    links = list(
        db.scalars(
            select(EventSourceLink).where(
                EventSourceLink.source_transaction_id == source_id,
                EventSourceLink.link_type.in_(
                    ("canonical_source", "funding_leg", "settlement_leg")
                ),
            )
        )
    )
    if not links:
        return None
    preferred = sorted(
        links,
        key=lambda link: (
            0 if link.link_type == "canonical_source" else 1,
            link.economic_event_id,
        ),
    )
    return db.get(EconomicEvent, preferred[0].economic_event_id)


def _candidate_for_action(
    candidates: list[ReconciliationCandidate], action: ReconciliationWriteAction
) -> ReconciliationCandidate | None:
    source_kind = {
        "paypal_sparda_funding": "paypal_sparda",
        "amazon_payment": "amazon_payment",
        "amazon_refund": "amazon_refund",
    }.get(action.kind)
    return next(
        (
            candidate
            for candidate in candidates
            if candidate.kind == source_kind
            and candidate.source_id == action.source_id
            and candidate.target_id == action.target_id
        ),
        None,
    )


def _action_detail(
    db: Session,
    action: ReconciliationWriteAction,
    candidate: ReconciliationCandidate | None,
) -> dict[str, object]:
    event = db.get(EconomicEvent, action.event_id)
    canonical = _canonical_source(db, action.event_id)
    detail = action.as_dict()
    detail.update(
        {
            "amount": candidate.amount if candidate is not None else None,
            "occurred_at": candidate.occurred_at if candidate is not None else None,
            "event_type": event.event_type if event is not None else None,
            "event_description": event.description if event is not None else None,
            "payment_source": canonical.source_system if canonical is not None else None,
            "merchant": (
                canonical.merchant_raw or canonical.description_raw
                if canonical is not None
                else None
            ),
        }
    )
    if action.amazon_record_ids:
        records = [
            db.get(AmazonEnrichmentRecord, record_id) for record_id in action.amazon_record_ids
        ]
        records = [record for record in records if record is not None]
        detail["amazon_evidence"] = [
            {
                "record_id": record.id,
                "record_type": record.record_type,
                "amount": format(abs(record.amount), ".2f") if record.amount is not None else None,
                "occurred_at": record.occurred_at.isoformat() if record.occurred_at else None,
                "product_title": record.product_title,
                "payment_method": record.payment_method,
            }
            for record in records
        ]
    return detail


def _unsafe_reason(db: Session, candidate: ReconciliationCandidate) -> str:
    if candidate.source_id is None or candidate.target_id is None:
        return "Kandidat besitzt keine vollstaendigen Source-/Target-IDs."

    if candidate.kind == "paypal_sparda":
        paypal_event = _event_for_source(db, candidate.source_id)
        sparda_source = db.get(SourceTransaction, candidate.target_id)
        sparda_event = _event_for_source(db, candidate.target_id)
        if paypal_event is None:
            return "PayPal-Funding-Zeile ist keinem bestehenden Economic Event zugeordnet."
        if sparda_source is None:
            return "Sparda-Zieltransaktion existiert nicht."
        if sparda_source.source_system != "sparda":
            return f"Zielquelle ist {sparda_source.source_system!r} statt 'sparda'."
        semantic = (sparda_source.metadata_json or {}).get("sparda_semantic")
        if semantic != "paypal_funding_leg":
            return (
                "Sparda-Zieltransaktion hat nicht das erwartete Semantic "
                f"'paypal_funding_leg' (gefunden: {semantic!r})."
            )
        if sparda_event is not None and sparda_event.event_type != "transfer":
            return (
                "Sparda-Zieltransaktion ist bereits mit einem Economic Event vom Typ "
                f"{sparda_event.event_type!r} statt 'transfer' verknuepft."
            )
        return "PayPal/Sparda-Kandidat wurde durch eine unbekannte Sicherheitsbedingung verworfen."

    if candidate.kind in {"amazon_payment", "amazon_refund"}:
        record = db.get(AmazonEnrichmentRecord, candidate.source_id)
        event = db.get(EconomicEvent, candidate.target_id)
        if record is None:
            return "Amazon-Evidenzdatensatz existiert nicht."
        if event is None:
            return "Ziel-Economic-Event existiert nicht."
        expected = "refund" if candidate.kind == "amazon_refund" else "expense"
        if event.event_type != expected:
            return f"Ziel-Economic-Event hat Typ {event.event_type!r} statt erwartet {expected!r}."
        return "Amazon-Kandidat wurde durch eine unbekannte Sicherheitsbedingung verworfen."

    return "High-Confidence-Kandidat ist fuer Apply V1 nicht freigegeben."


def reconciliation_plan_report(db: Session) -> dict[str, object]:
    plan = plan_reconciliation_writes(db)
    audit = audit_reconciliation(db)
    candidates = list(audit.candidates or [])

    planned_keys = {
        (
            {
                "paypal_sparda_funding": "paypal_sparda",
                "amazon_payment": "amazon_payment",
                "amazon_refund": "amazon_refund",
            }.get(action.kind),
            action.source_id,
            action.target_id,
        )
        for action in plan.actions
    }

    unsafe_details: list[ReconciliationSkipDetail] = []
    blocked_details: list[ReconciliationSkipDetail] = []
    for candidate in candidates:
        if candidate.confidence != "high":
            continue
        key = (candidate.kind, candidate.source_id, candidate.target_id)
        if key in planned_keys:
            continue
        if candidate.kind == "paypal_amex":
            blocked_details.append(
                ReconciliationSkipDetail(
                    kind=candidate.kind,
                    source_id=candidate.source_id,
                    target_id=candidate.target_id,
                    amount=candidate.amount,
                    occurred_at=candidate.occurred_at,
                    reason=(
                        "Automatisch blockiert: Amex besitzt bereits ein eigenes Expense-Event; "
                        "ein einfacher Funding-Link wuerde die Ausgabe nicht deduplizieren."
                    ),
                )
            )
            continue
        if candidate.kind in {"paypal_sparda", "amazon_payment", "amazon_refund"}:
            unsafe_details.append(
                ReconciliationSkipDetail(
                    kind=candidate.kind,
                    source_id=candidate.source_id,
                    target_id=candidate.target_id,
                    amount=candidate.amount,
                    occurred_at=candidate.occurred_at,
                    reason=_unsafe_reason(db, candidate),
                )
            )

    return {
        "action_count": plan.action_count,
        "blocked_paypal_amex": plan.blocked_paypal_amex,
        "skipped_existing": plan.skipped_existing,
        "skipped_unsafe": plan.skipped_unsafe,
        "actions": [
            _action_detail(db, action, _candidate_for_action(candidates, action))
            for action in plan.actions
        ],
        "skipped_unsafe_details": [item.as_dict() for item in unsafe_details],
        "blocked_paypal_amex_details": [item.as_dict() for item in blocked_details],
    }

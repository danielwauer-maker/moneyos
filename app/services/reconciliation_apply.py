from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AmazonEnrichmentRecord,
    AmazonPaymentMatch,
    EconomicEvent,
    EventSourceLink,
    SourceTransaction,
)
from app.services.reconciliation_audit import audit_reconciliation


@dataclass(frozen=True)
class ReconciliationWriteAction:
    kind: str
    source_id: int
    target_id: int
    event_id: int
    confidence: str
    detail: str
    amazon_record_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["amazon_record_ids"] = list(self.amazon_record_ids)
        return data


@dataclass
class ReconciliationWritePlan:
    actions: list[ReconciliationWriteAction]
    blocked_paypal_amex: int = 0
    skipped_existing: int = 0
    skipped_unsafe: int = 0

    @property
    def action_count(self) -> int:
        return len(self.actions)

    def as_dict(self) -> dict[str, object]:
        return {
            "action_count": self.action_count,
            "blocked_paypal_amex": self.blocked_paypal_amex,
            "skipped_existing": self.skipped_existing,
            "skipped_unsafe": self.skipped_unsafe,
            "actions": [action.as_dict() for action in self.actions],
        }


@dataclass
class ReconciliationApplyResult:
    event_source_links_created: int = 0
    amazon_payment_matches_created: int = 0
    blocked_paypal_amex: int = 0
    skipped_existing: int = 0
    skipped_unsafe: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


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


def _amazon_group_ids(
    db: Session,
    record: AmazonEnrichmentRecord,
    *,
    match_type: str,
) -> tuple[int, ...]:
    if match_type == "payment":
        if record.order_key is None:
            return (record.id,)
        rows = list(
            db.scalars(
                select(AmazonEnrichmentRecord).where(
                    AmazonEnrichmentRecord.order_key == record.order_key,
                    AmazonEnrichmentRecord.record_type.in_(
                        ("order_item", "digital_order_item")
                    ),
                )
            )
        )
        return tuple(sorted(row.id for row in rows))

    rows = list(
        db.scalars(
            select(AmazonEnrichmentRecord).where(
                AmazonEnrichmentRecord.record_type == "refund",
                AmazonEnrichmentRecord.order_key == record.order_key,
                AmazonEnrichmentRecord.amount == record.amount,
            )
        )
    )
    same_day = [
        row
        for row in rows
        if row.occurred_at is not None
        and record.occurred_at is not None
        and row.occurred_at.date() == record.occurred_at.date()
    ]
    return tuple(sorted(row.id for row in same_day or [record]))


def _existing_event_link(
    db: Session,
    *,
    event_id: int,
    source_id: int,
    link_type: str,
) -> bool:
    return (
        db.scalar(
            select(EventSourceLink.id).where(
                EventSourceLink.economic_event_id == event_id,
                EventSourceLink.source_transaction_id == source_id,
                EventSourceLink.link_type == link_type,
            )
        )
        is not None
    )


def plan_reconciliation_writes(db: Session) -> ReconciliationWritePlan:
    audit = audit_reconciliation(db)
    actions: list[ReconciliationWriteAction] = []
    blocked_paypal_amex = 0
    skipped_existing = 0
    skipped_unsafe = 0

    for candidate in audit.candidates or []:
        if candidate.confidence != "high":
            continue
        if candidate.source_id is None or candidate.target_id is None:
            skipped_unsafe += 1
            continue

        if candidate.kind == "paypal_amex":
            # Amex merchant charges already own an expense EconomicEvent.
            # Adding a second link without a canonical event merge would keep
            # double-counting the same household expense.
            blocked_paypal_amex += 1
            continue

        if candidate.kind == "paypal_sparda":
            paypal_event = _event_for_source(db, candidate.source_id)
            sparda_source = db.get(SourceTransaction, candidate.target_id)
            sparda_event = _event_for_source(db, candidate.target_id)
            if (
                paypal_event is None
                or sparda_source is None
                or sparda_source.source_system != "sparda"
                or (sparda_source.metadata_json or {}).get("sparda_semantic")
                != "paypal_funding_leg"
                or (sparda_event is not None and sparda_event.event_type != "transfer")
            ):
                skipped_unsafe += 1
                continue
            if _existing_event_link(
                db,
                event_id=paypal_event.id,
                source_id=sparda_source.id,
                link_type="funding_leg",
            ):
                skipped_existing += 1
                continue
            actions.append(
                ReconciliationWriteAction(
                    kind="paypal_sparda_funding",
                    source_id=candidate.source_id,
                    target_id=sparda_source.id,
                    event_id=paypal_event.id,
                    confidence=candidate.score,
                    detail="High-confidence PayPal funding matched to Sparda transfer.",
                )
            )
            continue

        if candidate.kind in {"amazon_payment", "amazon_refund"}:
            amazon_record = db.get(AmazonEnrichmentRecord, candidate.source_id)
            event = db.get(EconomicEvent, candidate.target_id)
            if amazon_record is None or event is None:
                skipped_unsafe += 1
                continue
            match_type = "refund" if candidate.kind == "amazon_refund" else "payment"
            expected_event_type = "refund" if match_type == "refund" else "expense"
            if event.event_type != expected_event_type:
                skipped_unsafe += 1
                continue
            record_ids = _amazon_group_ids(db, amazon_record, match_type=match_type)
            missing_ids = tuple(
                record_id
                for record_id in record_ids
                if db.scalar(
                    select(AmazonPaymentMatch.id).where(
                        AmazonPaymentMatch.amazon_record_id == record_id,
                        AmazonPaymentMatch.economic_event_id == event.id,
                        AmazonPaymentMatch.match_type == match_type,
                    )
                )
                is None
            )
            if not missing_ids:
                skipped_existing += 1
                continue
            actions.append(
                ReconciliationWriteAction(
                    kind=f"amazon_{match_type}",
                    source_id=amazon_record.id,
                    target_id=event.id,
                    event_id=event.id,
                    confidence=candidate.score,
                    detail="High-confidence Amazon evidence matched to existing event.",
                    amazon_record_ids=missing_ids,
                )
            )

    return ReconciliationWritePlan(
        actions=actions,
        blocked_paypal_amex=blocked_paypal_amex,
        skipped_existing=skipped_existing,
        skipped_unsafe=skipped_unsafe,
    )


def apply_reconciliation_writes(db: Session) -> ReconciliationApplyResult:
    plan = plan_reconciliation_writes(db)
    result = ReconciliationApplyResult(
        blocked_paypal_amex=plan.blocked_paypal_amex,
        skipped_existing=plan.skipped_existing,
        skipped_unsafe=plan.skipped_unsafe,
    )

    for action in plan.actions:
        confidence = Decimal(action.confidence)
        if action.kind == "paypal_sparda_funding":
            if not _existing_event_link(
                db,
                event_id=action.event_id,
                source_id=action.target_id,
                link_type="funding_leg",
            ):
                db.add(
                    EventSourceLink(
                        economic_event_id=action.event_id,
                        source_transaction_id=action.target_id,
                        link_type="funding_leg",
                        confidence=confidence,
                        notes="Reconciliation V1: high-confidence PayPal/Sparda funding match",
                    )
                )
                result.event_source_links_created += 1
            continue

        if action.kind in {"amazon_payment", "amazon_refund"}:
            match_type = "refund" if action.kind == "amazon_refund" else "payment"
            for record_id in action.amazon_record_ids:
                exists = db.scalar(
                    select(AmazonPaymentMatch.id).where(
                        AmazonPaymentMatch.amazon_record_id == record_id,
                        AmazonPaymentMatch.economic_event_id == action.event_id,
                        AmazonPaymentMatch.match_type == match_type,
                    )
                )
                if exists is not None:
                    continue
                db.add(
                    AmazonPaymentMatch(
                        amazon_record_id=record_id,
                        economic_event_id=action.event_id,
                        match_type=match_type,
                        status="linked",
                        confidence=confidence,
                        reason="Reconciliation V1: high-confidence deterministic match",
                    )
                )
                result.amazon_payment_matches_created += 1

    return result

from __future__ import annotations

from collections import defaultdict
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


@dataclass(frozen=True)
class ReconciliationCandidate:
    kind: str
    confidence: str
    score: str
    amount: str
    occurred_at: str
    source_id: int | None
    target_id: int | None
    detail: str

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


@dataclass
class ReconciliationAuditReport:
    source_transactions: int = 0
    economic_events: int = 0
    amazon_records: int = 0
    existing_cross_source_links: int = 0
    high_confidence_candidates: int = 0
    medium_confidence_candidates: int = 0
    unresolved_candidates: int = 0
    paypal_sparda: int = 0
    paypal_amex: int = 0
    amazon_payment: int = 0
    amazon_refund: int = 0
    amex_sparda_settlement: int = 0
    candidates: list[ReconciliationCandidate] | None = None

    def as_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["candidates"] = [candidate.as_dict() for candidate in self.candidates or []]
        return data


def _same_amount(left: Decimal, right: Decimal) -> bool:
    return abs(abs(left) - abs(right)) <= Decimal("0.01")


def _days(left, right) -> int:
    return abs((left.date() - right.date()).days)


def _existing_event_for_source(
    links_by_source: dict[int, list[EventSourceLink]],
    source_id: int,
) -> int | None:
    links = links_by_source.get(source_id, [])
    preferred = [
        link
        for link in links
        if link.link_type
        in {"canonical_source", "funding_leg", "third_party_payment", "settlement_leg"}
    ]
    return preferred[0].economic_event_id if preferred else None


def _candidate(
    *,
    kind: str,
    confidence: str,
    score: Decimal,
    amount: Decimal,
    occurred_at,
    source_id: int | None,
    target_id: int | None,
    detail: str,
) -> ReconciliationCandidate:
    return ReconciliationCandidate(
        kind=kind,
        confidence=confidence,
        score=format(score, "f"),
        amount=format(abs(amount), ".2f"),
        occurred_at=occurred_at.isoformat(),
        source_id=source_id,
        target_id=target_id,
        detail=detail,
    )


def audit_reconciliation(db: Session) -> ReconciliationAuditReport:
    sources = list(db.scalars(select(SourceTransaction)))
    events = list(db.scalars(select(EconomicEvent)))
    links = list(db.scalars(select(EventSourceLink)))
    amazon = list(db.scalars(select(AmazonEnrichmentRecord)))
    amazon_matches = list(db.scalars(select(AmazonPaymentMatch)))

    by_system: dict[str, list[SourceTransaction]] = defaultdict(list)
    for source in sources:
        by_system[source.source_system].append(source)

    links_by_source: dict[int, list[EventSourceLink]] = defaultdict(list)
    for link in links:
        links_by_source[link.source_transaction_id].append(link)

    candidates: list[ReconciliationCandidate] = []

    existing_cross_source_links = sum(
        1
        for link in links
        if link.link_type in {"funding_leg", "settlement_leg", "third_party_payment"}
    )

    # PayPal funding -> Sparda / Amex
    for source in by_system.get("paypal", []):
        meta = source.metadata_json or {}
        if meta.get("paypal_semantic") != "funding":
            continue

        text = f"{source.merchant_raw or ''} {source.description_raw}".casefold()
        if "kreditkarte" in text:
            target_system = "amex"
            kind = "paypal_amex"
            external = by_system.get("amex", [])
        elif "bankgutschrift" in text or "bank" in text:
            target_system = "sparda"
            kind = "paypal_sparda"
            external = by_system.get("sparda", [])
        else:
            continue

        event_id = _existing_event_for_source(links_by_source, source.id)
        already_linked = False
        if event_id is not None:
            already_linked = any(
                link.economic_event_id == event_id
                and link.source_transaction_id != source.id
                and next(
                    (
                        candidate.source_system
                        for candidate in sources
                        if candidate.id == link.source_transaction_id
                    ),
                    None,
                )
                == target_system
                for link in links
            )
        if already_linked:
            continue

        matches = [
            candidate
            for candidate in external
            if candidate.currency == source.currency
            and _same_amount(candidate.amount, source.amount)
            and _days(candidate.booked_at, source.booked_at)
            <= (3 if target_system == "amex" else 4)
        ]
        close = [
            candidate for candidate in matches if _days(candidate.booked_at, source.booked_at) <= 1
        ]

        if len(close) == 1:
            candidates.append(
                _candidate(
                    kind=kind,
                    confidence="high",
                    score=Decimal("0.95"),
                    amount=source.amount,
                    occurred_at=source.booked_at,
                    source_id=source.id,
                    target_id=close[0].id,
                    detail=(
                        f"Eindeutiger {target_system.upper()}-Treffer: identischer Betrag, "
                        "max. 1 Tag Abstand."
                    ),
                )
            )
        elif len(matches) == 1:
            candidates.append(
                _candidate(
                    kind=kind,
                    confidence="medium",
                    score=Decimal("0.75"),
                    amount=source.amount,
                    occurred_at=source.booked_at,
                    source_id=source.id,
                    target_id=matches[0].id,
                    detail=(
                        f"Ein {target_system.upper()}-Betragskandidat im erweiterten Datumsfenster."
                    ),
                )
            )
        else:
            candidates.append(
                _candidate(
                    kind=kind,
                    confidence="unresolved",
                    score=Decimal("0"),
                    amount=source.amount,
                    occurred_at=source.booked_at,
                    source_id=source.id,
                    target_id=None,
                    detail=f"{len(matches)} passende {target_system.upper()}-Kandidaten gefunden.",
                )
            )

    # Amex statement payment -> Sparda transfer
    for source in by_system.get("amex", []):
        if (source.metadata_json or {}).get("amex_semantic") != "statement_payment":
            continue
        event_id = _existing_event_for_source(links_by_source, source.id)
        if event_id is not None:
            continue

        matches = [
            candidate
            for candidate in by_system.get("sparda", [])
            if candidate.currency == source.currency
            and _same_amount(candidate.amount, source.amount)
            and _days(candidate.booked_at, source.booked_at) <= 7
            and (
                "american express"
                in f"{candidate.merchant_raw or ''} {candidate.description_raw}".casefold()
                or (candidate.metadata_json or {}).get("sparda_semantic")
                in {"amex_settlement", "card_settlement"}
            )
        ]
        close = [
            candidate for candidate in matches if _days(candidate.booked_at, source.booked_at) <= 2
        ]
        if len(close) == 1:
            confidence, score, target = "high", Decimal("0.95"), close[0]
        elif len(matches) == 1:
            confidence, score, target = "medium", Decimal("0.72"), matches[0]
        else:
            confidence, score, target = "unresolved", Decimal("0"), None
        candidates.append(
            _candidate(
                kind="amex_sparda_settlement",
                confidence=confidence,
                score=score,
                amount=source.amount,
                occurred_at=source.booked_at,
                source_id=source.id,
                target_id=target.id if target else None,
                detail=(
                    "Amex-Abrechnung passt zu Sparda-Transfer."
                    if target
                    else f"{len(matches)} Sparda-Settlement-Kandidaten gefunden."
                ),
            )
        )

    matched_amazon_ids = {match.amazon_record_id for match in amazon_matches}

    # Amazon direct and grouped split matches -> existing EconomicEvents
    relevant = [
        row
        for row in amazon
        if row.id not in matched_amazon_ids
        and row.occurred_at is not None
        and row.amount is not None
        and row.record_type in {"order_item", "digital_order_item", "refund"}
    ]

    groups: dict[tuple[str, str], list[AmazonEnrichmentRecord]] = defaultdict(list)
    for row in relevant:
        key = row.order_key or f"record:{row.id}"
        groups[(row.record_type, key)].append(row)

    for (record_type, _key), rows in groups.items():
        total = sum((abs(row.amount or Decimal("0")) for row in rows), Decimal("0"))
        if total == 0:
            continue
        occurred_at = min(row.occurred_at for row in rows if row.occurred_at is not None)
        expected_event_type = "refund" if record_type == "refund" else "expense"
        matches = [
            event
            for event in events
            if event.event_type == expected_event_type
            and event.currency == (rows[0].currency or event.currency)
            and _same_amount(event.amount, total)
            and _days(event.occurred_at, occurred_at) <= 3
        ]

        source_systems: set[str] = set()
        for event in matches:
            for link in links:
                if link.economic_event_id != event.id:
                    continue
                source = next(
                    (item for item in sources if item.id == link.source_transaction_id), None
                )
                if source:
                    source_systems.add(source.source_system)

        if len(matches) == 1:
            confidence = (
                "high"
                if len(rows) > 1 or source_systems & {"amex", "paypal", "sparda"}
                else "medium"
            )
            score = Decimal("0.94") if confidence == "high" else Decimal("0.75")
            target_id = matches[0].id
        else:
            confidence = "unresolved"
            score = Decimal("0")
            target_id = None

        candidates.append(
            _candidate(
                kind="amazon_refund" if record_type == "refund" else "amazon_payment",
                confidence=confidence,
                score=score,
                amount=total,
                occurred_at=occurred_at,
                source_id=rows[0].id,
                target_id=target_id,
                detail=(
                    f"Amazon-Gruppe mit {len(rows)} Position(en), Summe {total:.2f} EUR."
                    if matches
                    else (
                        f"Amazon-Gruppe mit {len(rows)} Position(en); "
                        f"{len(matches)} passende Events."
                    )
                ),
            )
        )

    report = ReconciliationAuditReport(
        source_transactions=len(sources),
        economic_events=len(events),
        amazon_records=len(amazon),
        existing_cross_source_links=existing_cross_source_links,
        candidates=candidates,
    )
    for candidate in candidates:
        if candidate.confidence == "high":
            report.high_confidence_candidates += 1
        elif candidate.confidence == "medium":
            report.medium_confidence_candidates += 1
        else:
            report.unresolved_candidates += 1

        if candidate.kind == "paypal_sparda":
            report.paypal_sparda += 1
        elif candidate.kind == "paypal_amex":
            report.paypal_amex += 1
        elif candidate.kind == "amazon_payment":
            report.amazon_payment += 1
        elif candidate.kind == "amazon_refund":
            report.amazon_refund += 1
        elif candidate.kind == "amex_sparda_settlement":
            report.amex_sparda_settlement += 1

    return report

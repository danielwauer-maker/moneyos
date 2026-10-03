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
    RawImportRecord,
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


def _amazon_source(source: SourceTransaction) -> bool:
    text = " ".join(
        (
            source.merchant_raw or "",
            source.description_raw or "",
            str((source.metadata_json or {}).get("canonical_merchant", "")),
        )
    ).casefold()
    return any(marker in text for marker in ("amazon", "amzn", "audible", "kindle"))


def _payment_sources(payment_method: str | None) -> set[str]:
    text = (payment_method or "").casefold()
    result: set[str] = set()
    if "american express" in text or "amex" in text:
        result.add("amex")
    if "paypal" in text:
        result.add("paypal")
    if any(marker in text for marker in ("bank", "lastschrift", "giro", "sepa")):
        result.add("sparda")
    return result


def _raw_money(value: object) -> Decimal | None:
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = "".join(character for character in text if character.isdigit() or character in ",.-")
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return abs(Decimal(cleaned))
    except Exception:
        return None


def _amazon_payment_amount(
    rows: list[AmazonEnrichmentRecord],
    raw_by_id: dict[int, RawImportRecord],
) -> Decimal:
    totals = {
        value
        for row in rows
        if (raw := raw_by_id.get(row.raw_record_id))
        and (value := _raw_money((raw.raw_payload_json or {}).get("Total Amount"))) is not None
        and value != 0
    }
    if len(totals) == 1:
        return next(iter(totals))
    return sum((abs(row.amount or Decimal("0")) for row in rows), Decimal("0"))


def audit_reconciliation(db: Session) -> ReconciliationAuditReport:
    sources = list(db.scalars(select(SourceTransaction)))
    events = list(db.scalars(select(EconomicEvent)))
    links = list(db.scalars(select(EventSourceLink)))
    amazon = list(db.scalars(select(AmazonEnrichmentRecord)))
    amazon_matches = list(db.scalars(select(AmazonPaymentMatch)))
    raw_records = list(db.scalars(select(RawImportRecord)))

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
    raw_by_id = {record.id: record for record in raw_records}
    source_by_id = {source.id: source for source in sources}

    canonical_sources_by_event: dict[int, list[SourceTransaction]] = defaultdict(list)
    for link in links:
        if link.link_type != "canonical_source":
            continue
        source = source_by_id.get(link.source_transaction_id)
        if source is not None:
            canonical_sources_by_event[link.economic_event_id].append(source)

    # Amazon payment evidence: orders are grouped by order; refund evidence is collapsed
    # by order/date/amount so duplicate export rows do not double the economic value.
    relevant = [
        row
        for row in amazon
        if row.id not in matched_amazon_ids
        and row.occurred_at is not None
        and row.amount is not None
        and row.record_type in {"order_item", "digital_order_item", "refund"}
    ]

    order_groups: dict[str, list[AmazonEnrichmentRecord]] = defaultdict(list)
    refund_groups: dict[tuple[str, str, str], list[AmazonEnrichmentRecord]] = defaultdict(list)
    for row in relevant:
        if row.record_type == "refund":
            refund_key = (
                row.order_key or f"record:{row.id}",
                format(abs(row.amount or Decimal("0")), ".2f"),
                row.occurred_at.date().isoformat(),
            )
            refund_groups[refund_key].append(row)
        else:
            order_groups[row.order_key or f"record:{row.id}"].append(row)

    amazon_groups: list[tuple[str, list[AmazonEnrichmentRecord], Decimal]] = []
    for rows in order_groups.values():
        amazon_groups.append(("payment", rows, _amazon_payment_amount(rows, raw_by_id)))
    for rows in refund_groups.values():
        amazon_groups.append(("refund", rows, abs(rows[0].amount or Decimal("0"))))

    for match_type, rows, amount in amazon_groups:
        if amount == 0:
            continue
        occurred_at = min(row.occurred_at for row in rows if row.occurred_at is not None)
        expected_event_type = "refund" if match_type == "refund" else "expense"
        hints = set().union(*(_payment_sources(row.payment_method) for row in rows))

        matches: list[tuple[EconomicEvent, SourceTransaction, int]] = []
        for event in events:
            if event.event_type != expected_event_type or not _same_amount(event.amount, amount):
                continue
            if event.currency != (rows[0].currency or event.currency):
                continue
            days = _days(event.occurred_at, occurred_at)
            if days > (21 if match_type == "refund" else 10):
                continue
            canonical_sources = canonical_sources_by_event.get(event.id, [])
            amazon_sources = [source for source in canonical_sources if _amazon_source(source)]
            if not amazon_sources:
                continue
            source = amazon_sources[0]
            if hints and source.source_system not in hints:
                continue
            matches.append((event, source, days))

        matches.sort(key=lambda item: item[2])
        if len(matches) == 1:
            event, _source, days = matches[0]
            if days <= 3:
                confidence, score = "high", Decimal("0.98")
            else:
                confidence, score = "medium", Decimal("0.75")
            target_id = event.id
        elif matches and len(matches) > 1 and matches[0][2] < matches[1][2]:
            event, _source, _days = matches[0]
            confidence, score, target_id = "medium", Decimal("0.65"), event.id
        else:
            confidence, score, target_id = "unresolved", Decimal("0"), None

        duplicate_evidence = len(rows) if match_type == "refund" else 1
        candidates.append(
            _candidate(
                kind="amazon_refund" if match_type == "refund" else "amazon_payment",
                confidence=confidence,
                score=score,
                amount=amount,
                occurred_at=occurred_at,
                source_id=rows[0].id,
                target_id=target_id,
                detail=(
                    f"Amazon-{match_type}: {len(rows)} Evidenzzeile(n), Betrag {amount:.2f} EUR"
                    + (
                        f"; {duplicate_evidence} gleichartige Refund-Evidenzen zusammengefasst."
                        if match_type == "refund" and duplicate_evidence > 1
                        else "."
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

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Account,
    AssignmentRule,
    Category,
    EconomicEvent,
    Envelope,
    EnvelopeAssignmentDecision,
    EventSourceLink,
    ReviewItem,
    SourceTransaction,
    SourceTransactionAccount,
)
from app.security.redaction import redact_text

BASELINE_DATE = date(2026, 4, 30)
CONFIRMED_EVENT_STATUSES = frozenset({"booked", "confirmed"})
DECISIONS = frozenset({"assigned", "no_envelope", "later"})


@dataclass(frozen=True)
class EnvelopeCandidate:
    key: str
    source: SourceTransaction | None
    event: EconomicEvent | None
    review: ReviewItem | None
    decision: EnvelopeAssignmentDecision | None
    booked_at: datetime
    payee: str
    merchant_key: str | None
    amount: Decimal
    account: Account | None
    category: Category | None
    economic_type: str
    current_envelope: Envelope | None
    proposed_envelope: Envelope | None
    proposal_reason: str
    confidence: Decimal | None
    repeat_count: int

    @property
    def state(self) -> str:
        if self.decision is not None:
            return self.decision.decision
        if self.current_envelope is not None:
            return "assigned"
        return "unresolved"


@dataclass(frozen=True)
class AssignmentProgress:
    total: int
    assigned: int
    no_envelope: int
    unresolved: int

    @property
    def reviewed(self) -> int:
        return self.assigned + self.no_envelope


@dataclass(frozen=True)
class GroupOpportunity:
    label: str
    count: int


@dataclass(frozen=True)
class AssignmentWorkspace:
    candidates: tuple[EnvelopeCandidate, ...]
    progress: AssignmentProgress
    envelopes: tuple[Envelope, ...]
    rules: tuple[AssignmentRule, ...]
    merchant_groups: tuple[GroupOpportunity, ...]
    category_groups: tuple[GroupOpportunity, ...]
    month_groups: tuple[GroupOpportunity, ...]
    amount_groups: tuple[GroupOpportunity, ...]
    accounts: tuple[Account, ...]
    categories: tuple[Category, ...]


def candidate_key(source_id: int | None, event_id: int | None) -> str:
    if source_id is not None:
        return f"source:{source_id}"
    if event_id is not None:
        return f"event:{event_id}"
    raise ValueError("Envelope candidate requires a source transaction or economic event")


def _source_account(source: SourceTransaction | None) -> Account | None:
    if source is None:
        return None
    link = next((item for item in source.account_links if item.role == "source"), None)
    return link.account if link else None


def _category_label(category: Category | None) -> str:
    if category is None:
        return "Ohne Kategorie"
    return f"{category.parent.name} / {category.name}" if category.parent else category.name


def _rule_matches(
    rule: AssignmentRule,
    *,
    merchant_key: str | None,
    category_id: int | None,
    account_id: int | None,
) -> bool:
    conditions = rule.condition_json or {}
    if "merchant_pattern" in conditions:
        pattern = str(conditions["merchant_pattern"])
        if merchant_key is None or pattern not in merchant_key:
            return False
    if conditions.get("category_id") != category_id and "category_id" in conditions:
        return False
    if conditions.get("account_id") != account_id and "account_id" in conditions:
        return False
    return bool(conditions)


def _rule_proposal(
    rules: list[AssignmentRule],
    envelopes: dict[int, Envelope],
    *,
    merchant_key: str | None,
    category_id: int | None,
    account_id: int | None,
) -> tuple[Envelope | None, str, Decimal | None]:
    for rule in rules:
        if not _rule_matches(
            rule,
            merchant_key=merchant_key,
            category_id=category_id,
            account_id=account_id,
        ):
            continue
        envelope_id = (rule.action_json or {}).get("envelope_id")
        envelope = envelopes.get(envelope_id)
        if envelope is not None:
            return (
                envelope,
                f"Bestätigte Vorschlagsregel · Priorität {rule.priority}",
                Decimal("0.8"),
            )
    return None, "Kein Vorschlag", None


def _all_candidates(
    db: Session,
) -> tuple[list[EnvelopeCandidate], list[Envelope], list[AssignmentRule]]:
    period_start = datetime.combine(BASELINE_DATE, time.max)
    events = list(
        db.scalars(
            select(EconomicEvent)
            .where(
                EconomicEvent.occurred_at > period_start,
                EconomicEvent.event_type.in_(("expense", "refund")),
                EconomicEvent.status.in_(CONFIRMED_EVENT_STATUSES),
            )
            .options(
                selectinload(EconomicEvent.account),
                selectinload(EconomicEvent.category).selectinload(Category.parent),
                selectinload(EconomicEvent.envelope),
            )
        )
    )
    event_ids = [event.id for event in events]
    links = (
        list(
            db.scalars(
                select(EventSourceLink).where(
                    EventSourceLink.economic_event_id.in_(event_ids),
                    EventSourceLink.link_type == "canonical_source",
                )
            )
        )
        if event_ids
        else []
    )
    source_id_by_event = {link.economic_event_id: link.source_transaction_id for link in links}

    reviews = list(
        db.scalars(
            select(ReviewItem)
            .join(SourceTransaction, SourceTransaction.id == ReviewItem.source_transaction_id)
            .where(
                ReviewItem.status == "open",
                ReviewItem.proposed_event_type.in_(("expense", "refund"))
                | ReviewItem.proposed_event_type.is_(None),
                SourceTransaction.booked_at > period_start,
            )
            .options(
                selectinload(ReviewItem.proposed_category).selectinload(Category.parent),
                selectinload(ReviewItem.proposed_envelope),
            )
        )
    )
    source_ids = {
        *source_id_by_event.values(),
        *(review.source_transaction_id for review in reviews if review.source_transaction_id),
    }
    sources = (
        list(
            db.scalars(
                select(SourceTransaction)
                .where(SourceTransaction.id.in_(source_ids))
                .options(
                    selectinload(SourceTransaction.account_links).selectinload(
                        SourceTransactionAccount.account
                    )
                )
            )
        )
        if source_ids
        else []
    )
    sources_by_id = {source.id: source for source in sources}
    events_by_key = {
        candidate_key(source_id_by_event.get(event.id), event.id): event for event in events
    }
    reviews_by_key = {
        candidate_key(review.source_transaction_id, review.economic_event_id): review
        for review in reviews
        if review.source_transaction_id is not None or review.economic_event_id is not None
    }
    keys = set(events_by_key) | set(reviews_by_key)
    decisions = (
        list(
            db.scalars(
                select(EnvelopeAssignmentDecision)
                .where(EnvelopeAssignmentDecision.candidate_key.in_(keys))
                .options(selectinload(EnvelopeAssignmentDecision.envelope))
            )
        )
        if keys
        else []
    )
    decisions_by_key = {decision.candidate_key: decision for decision in decisions}
    envelopes = list(
        db.scalars(select(Envelope).where(Envelope.is_active).order_by(Envelope.sort_order))
    )
    envelopes_by_id = {envelope.id: envelope for envelope in envelopes}
    rules = list(
        db.scalars(
            select(AssignmentRule)
            .where(AssignmentRule.rule_type == "envelope_suggestion")
            .order_by(AssignmentRule.priority, AssignmentRule.id)
        )
    )
    enabled_rules = [rule for rule in rules if rule.enabled]

    draft: list[dict[str, object]] = []
    for key in keys:
        event = events_by_key.get(key)
        review = reviews_by_key.get(key)
        source_id = source_id_by_event.get(event.id) if event else None
        if source_id is None and review is not None:
            source_id = review.source_transaction_id
        source = sources_by_id.get(source_id) if source_id is not None else None
        account = _source_account(source) or (event.account if event else None)
        category = (event.category if event else None) or (
            review.proposed_category if review else None
        )
        raw_payee = source.merchant_raw if source and source.merchant_raw else None
        if not raw_payee and event:
            raw_payee = event.description
        payee = redact_text(raw_payee or "Unbekannter Zahlungspartner")[:160]
        merchant_key = payee.casefold() if raw_payee else None
        proposal = review.proposed_envelope if review else None
        reason = review.explanation if review and proposal else ""
        confidence = review.confidence if review and proposal else None
        if proposal is None:
            proposal, reason, confidence = _rule_proposal(
                enabled_rules,
                envelopes_by_id,
                merchant_key=merchant_key,
                category_id=category.id if category else None,
                account_id=account.id if account else None,
            )
        draft.append(
            {
                "key": key,
                "source": source,
                "event": event,
                "review": review,
                "decision": decisions_by_key.get(key),
                "booked_at": source.booked_at if source else event.occurred_at,
                "payee": payee,
                "merchant_key": merchant_key,
                "amount": event.amount if event else abs(source.amount),
                "account": account,
                "category": category,
                "economic_type": event.event_type
                if event
                else (review.proposed_event_type or "unknown"),
                "current_envelope": event.envelope if event else None,
                "proposed_envelope": proposal,
                "proposal_reason": redact_text(reason or "Kein Vorschlag")[:240],
                "confidence": confidence,
            }
        )
    merchant_counts = Counter(
        item["merchant_key"] for item in draft if item["merchant_key"] is not None
    )
    candidates = [
        EnvelopeCandidate(
            **item,
            repeat_count=merchant_counts.get(item["merchant_key"], 0),
        )
        for item in draft
    ]
    candidates.sort(
        key=lambda row: (
            row.repeat_count <= 1,
            row.category is None,
            -row.amount,
            row.economic_type == "unknown",
            row.booked_at,
        )
    )
    return candidates, envelopes, rules


def _opportunities(values: list[str], *, minimum: int = 2) -> tuple[GroupOpportunity, ...]:
    counts = Counter(values)
    return tuple(
        GroupOpportunity(label, count)
        for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if count >= minimum
    )


def build_assignment_workspace(
    db: Session,
    *,
    month: str | None = None,
    merchant: str | None = None,
    category_id: int | None = None,
    account_id: int | None = None,
    state: str = "unresolved",
    group_by: str = "priority",
) -> AssignmentWorkspace:
    all_rows, envelopes, rules = _all_candidates(db)
    assigned = sum(row.state == "assigned" for row in all_rows)
    no_envelope = sum(row.state == "no_envelope" for row in all_rows)
    progress = AssignmentProgress(
        total=len(all_rows),
        assigned=assigned,
        no_envelope=no_envelope,
        unresolved=len(all_rows) - assigned - no_envelope,
    )
    rows = all_rows
    if month:
        rows = [row for row in rows if row.booked_at.strftime("%Y-%m") == month]
    if merchant:
        needle = merchant.casefold()
        rows = [row for row in rows if needle in row.payee.casefold()]
    if category_id is not None:
        rows = [row for row in rows if row.category and row.category.id == category_id]
    if account_id is not None:
        rows = [row for row in rows if row.account and row.account.id == account_id]
    if state != "all":
        rows = [
            row
            for row in rows
            if row.state == state or (state == "unresolved" and row.state == "later")
        ]
    if group_by == "merchant":
        rows.sort(key=lambda row: (row.payee.casefold(), row.booked_at, -row.amount))
    elif group_by == "category":
        rows.sort(key=lambda row: (_category_label(row.category), row.booked_at, -row.amount))
    elif group_by == "month":
        rows.sort(key=lambda row: (row.booked_at.strftime("%Y-%m"), row.payee.casefold()))
    elif group_by == "amount":
        rows.sort(key=lambda row: (-row.amount, row.booked_at))

    return AssignmentWorkspace(
        candidates=tuple(rows),
        progress=progress,
        envelopes=tuple(envelopes),
        rules=tuple(rules),
        merchant_groups=_opportunities([row.payee for row in all_rows]),
        category_groups=_opportunities([_category_label(row.category) for row in all_rows]),
        month_groups=_opportunities(
            [row.booked_at.strftime("%Y-%m") for row in all_rows], minimum=1
        ),
        amount_groups=_opportunities([str(row.amount) for row in all_rows]),
        accounts=tuple(
            sorted(
                {row.account.id: row.account for row in all_rows if row.account}.values(),
                key=lambda account: account.name,
            )
        ),
        categories=tuple(
            sorted(
                {row.category.id: row.category for row in all_rows if row.category}.values(),
                key=_category_label,
            )
        ),
    )


def _consistent_condition(rows: list[EnvelopeCandidate], basis: str) -> dict[str, object]:
    if len(rows) < 2:
        raise ValueError("Eine Regel benötigt mindestens zwei ausgewählte Transaktionen")
    merchants = {row.merchant_key for row in rows}
    categories = {row.category.id if row.category else None for row in rows}
    accounts = {row.account.id if row.account else None for row in rows}
    condition: dict[str, object] = {}
    if (
        basis in {"merchant", "combination", "auto"}
        and len(merchants) == 1
        and None not in merchants
    ):
        condition["merchant_pattern"] = next(iter(merchants))
    if basis in {"category", "combination"} and len(categories) == 1 and None not in categories:
        condition["category_id"] = next(iter(categories))
    if basis in {"account", "combination"} and len(accounts) == 1 and None not in accounts:
        condition["account_id"] = next(iter(accounts))
    if basis == "auto" and not condition and len(categories) == 1 and None not in categories:
        condition["category_id"] = next(iter(categories))
    if not condition:
        raise ValueError("Die Auswahl besitzt keine ausreichend konsistenten Regelmerkmale")
    return condition


def apply_assignment_decisions(
    db: Session,
    *,
    keys: list[str],
    decision: str,
    envelope_id: int | None,
    create_rule: bool = False,
    rule_basis: str = "auto",
) -> int:
    if not keys:
        raise ValueError("Mindestens eine Transaktion auswählen")
    if decision not in DECISIONS:
        raise ValueError("Ungültige Umschlagentscheidung")
    envelope = db.get(Envelope, envelope_id) if envelope_id is not None else None
    if decision == "assigned" and (envelope is None or not envelope.is_active):
        raise ValueError("Für die Zuordnung ist ein aktiver Umschlag erforderlich")
    if decision != "assigned":
        envelope = None

    all_rows, _, _ = _all_candidates(db)
    by_key = {row.key: row for row in all_rows}
    selected = [by_key[key] for key in dict.fromkeys(keys) if key in by_key]
    if len(selected) != len(set(keys)):
        raise ValueError("Mindestens eine Auswahl ist kein aktueller Kandidat")

    rule: AssignmentRule | None = None
    if create_rule:
        if decision != "assigned" or envelope is None:
            raise ValueError("Regeln können nur aus bestätigten Umschlagzuordnungen entstehen")
        condition = _consistent_condition(selected, rule_basis)
        highest = db.scalar(
            select(AssignmentRule.priority)
            .where(AssignmentRule.rule_type == "envelope_suggestion")
            .order_by(AssignmentRule.priority.desc())
            .limit(1)
        )
        rule = AssignmentRule(
            rule_type="envelope_suggestion",
            priority=(highest or 0) + 10,
            enabled=True,
            condition_json=condition,
            action_json={"envelope_id": envelope.id},
        )
        db.add(rule)
        db.flush()

    now = datetime.now(UTC).replace(tzinfo=None)
    for row in selected:
        record = db.scalar(
            select(EnvelopeAssignmentDecision).where(
                EnvelopeAssignmentDecision.candidate_key == row.key
            )
        )
        if record is None:
            record = EnvelopeAssignmentDecision(
                candidate_key=row.key,
                source_transaction_id=row.source.id if row.source else None,
                economic_event_id=row.event.id if row.event else None,
                decision=decision,
                envelope_id=envelope.id if envelope else None,
                assignment_rule_id=rule.id if rule else None,
                decided_at=now if decision != "later" else None,
            )
            db.add(record)
        else:
            record.decision = decision
            record.envelope_id = envelope.id if envelope else None
            record.assignment_rule_id = rule.id if rule else record.assignment_rule_id
            record.decided_at = now if decision != "later" else None
            record.updated_at = now

        if row.event is not None:
            row.event.envelope_id = envelope.id if envelope else None
        if row.review is not None:
            row.review.proposed_envelope_id = envelope.id if envelope else None
    return len(selected)


def update_envelope_rule(
    db: Session,
    *,
    rule_id: int,
    priority: int,
    enabled: bool,
    envelope_id: int,
) -> AssignmentRule:
    rule = db.get(AssignmentRule, rule_id)
    envelope = db.get(Envelope, envelope_id)
    if rule is None or rule.rule_type != "envelope_suggestion":
        raise ValueError("Umschlagregel nicht gefunden")
    if envelope is None or not envelope.is_active:
        raise ValueError("Aktiver Zielumschlag erforderlich")
    rule.priority = priority
    rule.enabled = enabled
    rule.action_json = {"envelope_id": envelope.id}
    rule.updated_at = datetime.now(UTC).replace(tzinfo=None)
    return rule


def rule_condition_label(rule: AssignmentRule, db: Session) -> str:
    parts: list[str] = []
    conditions = rule.condition_json or {}
    if "merchant_pattern" in conditions:
        parts.append(f"Zahlungspartner: {redact_text(conditions['merchant_pattern'])}")
    if "category_id" in conditions:
        category = db.get(Category, conditions["category_id"])
        parts.append(f"Kategorie: {_category_label(category)}")
    if "account_id" in conditions:
        account = db.get(Account, conditions["account_id"])
        parts.append(f"Quelle: {account.name if account else 'Unbekannt'}")
    return " + ".join(parts) or "Keine Bedingung"

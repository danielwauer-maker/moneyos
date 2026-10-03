from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    EconomicEvent,
    Envelope,
    EnvelopeAssignmentDecision,
    EnvelopeMovement,
    EnvelopeSnapshot,
    EventSourceLink,
    ReviewItem,
    SourceTransaction,
)
from app.domain.envelopes import physical_balance
from app.domain.reconciliation import ReconciliationResult, reconcile_envelopes
from app.services.reviews import actionable_open_reviews

ZERO = Decimal("0")
CONFIRMED_EVENT_STATUSES = frozenset({"booked", "confirmed"})
BASELINE_SOURCES = frozenset({"confirmed_private_baseline", "confirmed_baseline"})


@dataclass(frozen=True)
class EnvelopeTargetRow:
    envelope: Envelope
    baseline_snapshot_id: int
    actual_snapshot_id: int
    baseline: Decimal
    contributions: Decimal
    expenses: Decimal
    refunds: Decimal
    target_adjustment: Decimal
    accounting_target: Decimal
    physical_target: Decimal
    rounding_remainder: Decimal
    deficit: Decimal
    actual: Decimal
    delta: Decimal
    action: str
    status: str
    unresolved_count: int
    included_event_ids: tuple[int, ...]


@dataclass(frozen=True)
class EnvelopeTargetView:
    calculation_date: date
    rows: tuple[EnvelopeTargetRow, ...]
    reconciliation: ReconciliationResult
    unresolved_count: int
    included_event_ids: tuple[int, ...]
    partial_envelope_names: tuple[str, ...]


def _month_starts_after(baseline: date, calculation_date: date) -> list[date]:
    current = date(baseline.year + (baseline.month == 12), baseline.month % 12 + 1, 1)
    months: list[date] = []
    while current <= calculation_date:
        months.append(current)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return months


def _confirmed_snapshots(envelope: Envelope, calculation_date: date) -> list[EnvelopeSnapshot]:
    return sorted(
        (
            snapshot
            for snapshot in envelope.snapshots
            if snapshot.is_confirmed and snapshot.snapshot_date <= calculation_date
        ),
        key=lambda snapshot: (snapshot.snapshot_date, snapshot.id),
    )


def _baseline_snapshot(snapshots: list[EnvelopeSnapshot]) -> EnvelopeSnapshot:
    designated = [snapshot for snapshot in snapshots if snapshot.source in BASELINE_SOURCES]
    return designated[0] if designated else snapshots[0]


def _monthly_contributions(
    envelope: Envelope, baseline: date, calculation_date: date
) -> tuple[Decimal, bool]:
    total = ZERO
    complete = True
    for month_start in _month_starts_after(baseline, calculation_date):
        rules = [
            rule
            for rule in envelope.rule_periods
            if rule.valid_from <= month_start
            and (rule.valid_to is None or month_start <= rule.valid_to)
        ]
        if len(rules) != 1:
            complete = False
            continue
        rule = rules[0]
        if rule.rule_type == "target_balance":
            continue
        if rule.monthly_amount is None:
            complete = False
            continue
        total += rule.monthly_amount
    return total, complete


def _physical_actual(
    db: Session,
    envelope_id: int,
    snapshot: EnvelopeSnapshot,
    calculation_date: date,
) -> Decimal:
    actual = snapshot.physical_balance
    end = datetime.combine(calculation_date + timedelta(days=1), time.min)
    movements = db.scalars(
        select(EnvelopeMovement).where(
            EnvelopeMovement.envelope_id == envelope_id,
            EnvelopeMovement.occurred_at
            >= datetime.combine(snapshot.snapshot_date + timedelta(days=1), time.min),
            EnvelopeMovement.occurred_at < end,
        )
    )
    for movement in movements:
        direction = -1 if movement.movement_type in {"expense", "transfer_out"} else 1
        actual += direction * movement.amount
    return actual


def _source_token(event_id: int, links: list[EventSourceLink]) -> str:
    canonical = next((link for link in links if link.link_type == "canonical_source"), None)
    return f"source:{canonical.source_transaction_id}" if canonical else f"event:{event_id}"


def calculate_envelope_targets(
    db: Session,
    *,
    calculation_date: date,
    free_vault_cash: Decimal,
) -> EnvelopeTargetView:
    envelopes = list(
        db.scalars(
            select(Envelope)
            .where(Envelope.is_active)
            .options(
                selectinload(Envelope.snapshots),
                selectinload(Envelope.rule_periods),
            )
            .order_by(Envelope.sort_order, Envelope.id)
        )
    )
    if not envelopes:
        reconciliation = reconcile_envelopes([], free_vault_cash)
        return EnvelopeTargetView(calculation_date, (), reconciliation, 0, (), ())

    snapshot_sets = {
        envelope.id: _confirmed_snapshots(envelope, calculation_date) for envelope in envelopes
    }
    if any(not snapshots for snapshots in snapshot_sets.values()):
        raise ValueError("All active envelopes require a confirmed physical snapshot")
    baselines = {
        envelope.id: _baseline_snapshot(snapshot_sets[envelope.id]) for envelope in envelopes
    }
    earliest_baseline = min(snapshot.snapshot_date for snapshot in baselines.values())
    period_start = datetime.combine(earliest_baseline + timedelta(days=1), time.min)
    period_end = datetime.combine(calculation_date + timedelta(days=1), time.min)

    events = list(
        db.scalars(
            select(EconomicEvent).where(
                EconomicEvent.occurred_at >= period_start,
                EconomicEvent.occurred_at < period_end,
                EconomicEvent.event_type.in_(("expense", "refund")),
                EconomicEvent.status.in_(CONFIRMED_EVENT_STATUSES),
            )
        )
    )
    event_ids = [event.id for event in events]
    links = (
        list(
            db.scalars(
                select(EventSourceLink).where(EventSourceLink.economic_event_id.in_(event_ids))
            )
        )
        if event_ids
        else []
    )
    links_by_event: dict[int, list[EventSourceLink]] = {}
    for link in links:
        links_by_event.setdefault(link.economic_event_id, []).append(link)

    canonical_event_by_source = {
        link.source_transaction_id: link.economic_event_id
        for link in links
        if link.link_type == "canonical_source"
    }
    event_by_id = {event.id: event for event in events}
    origin_source_ids = {
        link.source_transaction_id for link in links if link.link_type == "refund_origin"
    }
    if origin_source_ids:
        origin_links = list(
            db.scalars(
                select(EventSourceLink).where(
                    EventSourceLink.source_transaction_id.in_(origin_source_ids),
                    EventSourceLink.link_type == "canonical_source",
                )
            )
        )
        canonical_event_by_source.update(
            {link.source_transaction_id: link.economic_event_id for link in origin_links}
        )
        missing_event_ids = {
            link.economic_event_id
            for link in origin_links
            if link.economic_event_id not in event_by_id
        }
        if missing_event_ids:
            event_by_id.update(
                {
                    event.id: event
                    for event in db.scalars(
                        select(EconomicEvent).where(EconomicEvent.id.in_(missing_event_ids))
                    )
                }
            )
    actionable_reviews = actionable_open_reviews(db)
    open_review_event_ids = {
        review.economic_event_id
        for review in actionable_reviews
        if review.economic_event_id is not None
    }
    event_keys = {
        event.id: _source_token(event.id, links_by_event.get(event.id, [])) for event in events
    }
    assignment_decisions = {
        decision.candidate_key: decision
        for decision in db.scalars(
            select(EnvelopeAssignmentDecision).where(
                EnvelopeAssignmentDecision.candidate_key.in_(event_keys.values())
            )
        )
    }

    resolved_envelope_by_event: dict[int, int] = {}
    for event in events:
        if event.id in open_review_event_ids:
            continue
        decision = assignment_decisions.get(event_keys[event.id])
        if decision and decision.decision == "assigned" and decision.envelope_id is not None:
            resolved_envelope_by_event[event.id] = decision.envelope_id
            continue
        if decision and decision.decision == "no_envelope":
            continue
        if event.envelope_id is not None:
            resolved_envelope_by_event[event.id] = event.envelope_id
            continue
        if event.event_type != "refund":
            continue
        for link in links_by_event.get(event.id, []):
            if link.link_type != "refund_origin":
                continue
            original_id = canonical_event_by_source.get(link.source_transaction_id)
            original = event_by_id.get(original_id) if original_id is not None else None
            if original and original.event_type == "expense" and original.envelope_id is not None:
                resolved_envelope_by_event[event.id] = original.envelope_id
                break

    unresolved: dict[str, set[int] | None] = {}
    for event in events:
        if event.id in resolved_envelope_by_event:
            continue
        token = _source_token(event.id, links_by_event.get(event.id, []))
        unresolved[token] = None

    review_rows = [
        (review, review.source_transaction)
        for review in actionable_reviews
        if review.source_transaction is not None
        and review.source_transaction.booked_at >= period_start
        and review.source_transaction.booked_at < period_end
        and review.proposed_event_type in {"expense", "refund", None}
    ]
    for review, source in review_rows:
        token = f"source:{source.id}"
        proposed = (
            {review.proposed_envelope_id} if review.proposed_envelope_id is not None else None
        )
        existing = unresolved.get(token)
        if token not in unresolved or existing is None or proposed is None:
            unresolved[token] = proposed
        else:
            unresolved[token] = existing | proposed

    if unresolved:
        final_keys = set(
            db.scalars(
                select(EnvelopeAssignmentDecision.candidate_key).where(
                    EnvelopeAssignmentDecision.candidate_key.in_(unresolved),
                    EnvelopeAssignmentDecision.decision == "no_envelope",
                )
            )
        )
        for key in final_keys:
            unresolved.pop(key, None)

    rows: list[EnvelopeTargetRow] = []
    all_included_ids: set[int] = set()
    for envelope in envelopes:
        baseline_snapshot = baselines[envelope.id]
        actual_snapshot = snapshot_sets[envelope.id][-1]
        contributions, rules_complete = _monthly_contributions(
            envelope, baseline_snapshot.snapshot_date, calculation_date
        )
        assigned = [
            event
            for event in events
            if resolved_envelope_by_event.get(event.id) == envelope.id
            and event.occurred_at.date() > baseline_snapshot.snapshot_date
        ]
        expenses = sum(
            (event.amount for event in assigned if event.event_type == "expense"), start=ZERO
        )
        refunds = sum(
            (event.amount for event in assigned if event.event_type == "refund"), start=ZERO
        )
        included_ids = tuple(sorted(event.id for event in assigned))
        all_included_ids.update(included_ids)
        raw_target = baseline_snapshot.physical_balance + contributions - expenses + refunds
        target_adjustment = ZERO
        if envelope.target_rule_type == "target_balance" and envelope.target_amount is not None:
            target_adjustment = envelope.target_amount - raw_target
            accounting_target = envelope.target_amount
        else:
            accounting_target = raw_target
        rounded = physical_balance(accounting_target)
        rounding_remainder = max(accounting_target, ZERO) - rounded.physical
        actual = _physical_actual(db, envelope.id, actual_snapshot, calculation_date)
        delta = rounded.physical - actual
        action = "Einzahlen" if delta > ZERO else "Entnehmen" if delta < ZERO else "Keine Aktion"
        envelope_unresolved = sum(
            1
            for proposed_ids in unresolved.values()
            if proposed_ids is None or envelope.id in proposed_ids
        )
        status = "complete" if rules_complete and envelope_unresolved == 0 else "partial"
        rows.append(
            EnvelopeTargetRow(
                envelope=envelope,
                baseline_snapshot_id=baseline_snapshot.id,
                actual_snapshot_id=actual_snapshot.id,
                baseline=baseline_snapshot.physical_balance,
                contributions=contributions,
                expenses=expenses,
                refunds=refunds,
                target_adjustment=target_adjustment,
                accounting_target=accounting_target,
                physical_target=rounded.physical,
                rounding_remainder=rounding_remainder,
                deficit=rounded.deficit,
                actual=actual,
                delta=delta,
                action=action,
                status=status,
                unresolved_count=envelope_unresolved,
                included_event_ids=included_ids,
            )
        )

    reconciliation = reconcile_envelopes([row.delta for row in rows], free_vault_cash)
    return EnvelopeTargetView(
        calculation_date=calculation_date,
        rows=tuple(rows),
        reconciliation=reconciliation,
        unresolved_count=len(unresolved),
        included_event_ids=tuple(sorted(all_included_ids)),
        partial_envelope_names=tuple(row.envelope.name for row in rows if row.status == "partial"),
    )

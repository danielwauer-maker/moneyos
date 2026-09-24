"""Shared chronological review and assignment service.

This service is deliberately independent from the immutable import graph. Decisions
are append/update records and existing EconomicEvents are changed only by explicit
user action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Account,
    AssignmentRule,
    Category,
    CategoryAssignmentDecision,
    EconomicEvent,
    EconomicTypeAssignmentDecision,
    Envelope,
    EnvelopeAssignmentDecision,
    EventSourceLink,
    Project,
    ProjectAssignmentDecision,
    ReviewItem,
    SourceTransaction,
    SourceTransactionAccount,
)
from app.security.redaction import redact_text
from app.services.transaction_details import derive_transaction_detail

TYPE_VALUES = frozenset({"expense", "income", "transfer", "refund"})
PROJECT_DECISIONS = frozenset({"assigned", "no_project", "later"})


@dataclass(frozen=True)
class TransactionReviewRow:
    key: str
    source: SourceTransaction
    event: EconomicEvent | None
    review: ReviewItem | None
    booked_at: datetime
    raw_counterparty: str
    secondary_detail: str
    canonical_merchant: str
    amount: Decimal
    account: Account | None
    economic_type: str
    proposed_type: str | None
    category: Category | None
    proposed_category: Category | None
    envelope: Envelope | None
    proposed_envelope: Envelope | None
    project: Project | None
    proposed_project: Project | None
    project_decision: ProjectAssignmentDecision | None
    type_decision: EconomicTypeAssignmentDecision | None
    category_decision: CategoryAssignmentDecision | None
    envelope_decision: EnvelopeAssignmentDecision | None

    @property
    def type_resolved(self) -> bool:
        return self.economic_type in TYPE_VALUES

    @property
    def category_resolved(self) -> bool:
        return self.category is not None

    @property
    def envelope_resolved(self) -> bool:
        return self.envelope is not None or (
            self.envelope_decision is not None and self.envelope_decision.decision == "no_envelope"
        )

    @property
    def project_resolved(self) -> bool:
        return self.project is not None or (
            self.project_decision is not None and self.project_decision.decision == "no_project"
        )

    @property
    def fully_reviewed(self) -> bool:
        return (
            self.type_resolved
            and self.category_resolved
            and self.envelope_resolved
            and self.project_resolved
        )

    @property
    def status(self) -> str:
        return "Vollständig" if self.fully_reviewed else "Offen"


@dataclass(frozen=True)
class ReviewProgress:
    total: int
    fully_reviewed: int
    type_unresolved: int
    category_unresolved: int
    envelope_unresolved: int
    project_unresolved: int


def _key(source_id: int) -> str:
    return f"source:{source_id}"


def _source_account(source: SourceTransaction) -> Account | None:
    link = next((link for link in source.account_links if link.role == "source"), None)
    return link.account if link else None


def _load_rows(db: Session) -> list[TransactionReviewRow]:
    sources = list(
        db.scalars(
            select(SourceTransaction)
            .options(
                selectinload(SourceTransaction.account_links).selectinload(
                    SourceTransactionAccount.account
                ),
            )
            .order_by(SourceTransaction.booked_at.desc(), SourceTransaction.id.desc())
        )
    )
    source_ids = [source.id for source in sources]
    links = (
        list(
            db.scalars(
                select(EventSourceLink).where(
                    EventSourceLink.source_transaction_id.in_(source_ids),
                    EventSourceLink.link_type == "canonical_source",
                )
            )
        )
        if source_ids
        else []
    )
    event_by_source = {
        link.source_transaction_id: db.get(EconomicEvent, link.economic_event_id) for link in links
    }
    reviews = (
        list(
            db.scalars(
                select(ReviewItem)
                .where(
                    ReviewItem.source_transaction_id.in_(source_ids), ReviewItem.status == "open"
                )
                .options(
                    selectinload(ReviewItem.proposed_category),
                    selectinload(ReviewItem.proposed_envelope),
                    selectinload(ReviewItem.proposed_project),
                )
            )
        )
        if source_ids
        else []
    )
    review_by_source = {item.source_transaction_id: item for item in reviews}
    keys = [_key(source.id) for source in sources]
    categories = (
        {
            item.candidate_key: item
            for item in db.scalars(
                select(CategoryAssignmentDecision).where(
                    CategoryAssignmentDecision.candidate_key.in_(keys)
                )
            )
        }
        if keys
        else {}
    )
    envelopes = (
        {
            item.candidate_key: item
            for item in db.scalars(
                select(EnvelopeAssignmentDecision)
                .where(EnvelopeAssignmentDecision.candidate_key.in_(keys))
                .options(selectinload(EnvelopeAssignmentDecision.envelope))
            )
        }
        if keys
        else {}
    )
    projects = (
        {
            item.candidate_key: item
            for item in db.scalars(
                select(ProjectAssignmentDecision)
                .where(ProjectAssignmentDecision.candidate_key.in_(keys))
                .options(selectinload(ProjectAssignmentDecision.project))
            )
        }
        if keys
        else {}
    )
    types = (
        {
            item.candidate_key: item
            for item in db.scalars(
                select(EconomicTypeAssignmentDecision).where(
                    EconomicTypeAssignmentDecision.candidate_key.in_(keys)
                )
            )
        }
        if keys
        else {}
    )
    suggestion_rules = list(
        db.scalars(
            select(AssignmentRule)
            .where(
                AssignmentRule.rule_type == "transaction_review_suggestion",
                AssignmentRule.enabled.is_(True),
            )
            .order_by(AssignmentRule.priority, AssignmentRule.id)
        )
    )
    rows: list[TransactionReviewRow] = []
    for source in sources:
        key = _key(source.id)
        event = event_by_source.get(source.id)
        review = review_by_source.get(source.id)
        if (
            source.source_system == "paypal"
            and (source.metadata_json or {}).get("technical") is True
            and review is None
        ):
            continue
        detail = derive_transaction_detail(
            source, None, fallback=event.description if event else source.description_raw
        )
        cat_decision = categories.get(key)
        env_decision = envelopes.get(key)
        project_decision = projects.get(key)
        type_decision = types.get(key)
        category = (event.category if event else None) or (
            cat_decision.category if cat_decision else None
        )
        envelope = (event.envelope if event else None) or (
            env_decision.envelope if env_decision and env_decision.decision == "assigned" else None
        )
        project = (
            event.project
            if event
            else (
                project_decision.project
                if project_decision and project_decision.decision == "assigned"
                else None
            )
        )
        economic_type = (
            event.event_type
            if event
            else (
                type_decision.decision
                if type_decision and type_decision.decision != "later"
                else "review"
            )
        )
        proposed_type = review.proposed_event_type if review else None
        proposed_category = review.proposed_category if review else None
        proposed_envelope = review.proposed_envelope if review else None
        proposed_project = review.proposed_project if review else None
        for rule in suggestion_rules:
            conditions = rule.condition_json or {}
            if rule.valid_from and source.booked_at.date() < rule.valid_from:
                continue
            if rule.valid_to and source.booked_at.date() > rule.valid_to:
                continue
            merchant_pattern = conditions.get("merchant_pattern")
            if (
                merchant_pattern
                and str(merchant_pattern) not in detail.canonical_merchant.casefold()
            ):
                continue
            account_id = conditions.get("account_id")
            account = _source_account(source)
            if account_id and (account is None or account.id != account_id):
                continue
            actions = rule.action_json or {}
            proposed_type = proposed_type or actions.get("economic_type")
            if proposed_category is None and actions.get("category_id") is not None:
                proposed_category = db.get(Category, actions["category_id"])
            if proposed_envelope is None and actions.get("envelope_id") is not None:
                proposed_envelope = db.get(Envelope, actions["envelope_id"])
            if proposed_project is None and actions.get("project_id") is not None:
                proposed_project = db.get(Project, actions["project_id"])
            break
        rows.append(
            TransactionReviewRow(
                key=key,
                source=source,
                event=event,
                review=review,
                booked_at=source.booked_at,
                raw_counterparty=redact_text(detail.raw_counterparty),
                secondary_detail=redact_text(detail.secondary_detail),
                canonical_merchant=redact_text(detail.canonical_merchant),
                amount=abs(source.amount),
                account=_source_account(source),
                economic_type=economic_type,
                proposed_type=proposed_type,
                category=category,
                proposed_category=proposed_category,
                envelope=envelope,
                proposed_envelope=proposed_envelope,
                project=project,
                proposed_project=proposed_project,
                project_decision=project_decision,
                type_decision=type_decision,
                category_decision=cat_decision,
                envelope_decision=env_decision,
            )
        )
    return rows


def review_progress(rows: list[TransactionReviewRow]) -> ReviewProgress:
    return ReviewProgress(
        total=len(rows),
        fully_reviewed=sum(row.fully_reviewed for row in rows),
        type_unresolved=sum(not row.type_resolved for row in rows),
        category_unresolved=sum(not row.category_resolved for row in rows),
        envelope_unresolved=sum(not row.envelope_resolved for row in rows),
        project_unresolved=sum(not row.project_resolved for row in rows),
    )


def build_transaction_review(
    db: Session,
    *,
    sort: str = "newest",
    month: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    account_id: int | None = None,
    merchant: str | None = None,
    economic_type: str | None = None,
    category_id: int | None = None,
    envelope_id: int | None = None,
    project_id: int | None = None,
    unresolved_only: bool = False,
) -> tuple[list[TransactionReviewRow], ReviewProgress]:
    all_rows = _load_rows(db)
    progress = review_progress(all_rows)
    rows = all_rows
    if month:
        rows = [row for row in rows if row.booked_at.strftime("%Y-%m") == month]
    if date_from:
        rows = [row for row in rows if row.booked_at.date().isoformat() >= date_from]
    if date_to:
        rows = [row for row in rows if row.booked_at.date().isoformat() <= date_to]
    if account_id is not None:
        rows = [row for row in rows if row.account and row.account.id == account_id]
    if merchant:
        needle = merchant.casefold()
        rows = [
            row
            for row in rows
            if needle in row.canonical_merchant.casefold()
            or needle in row.raw_counterparty.casefold()
        ]
    if economic_type:
        rows = [row for row in rows if row.economic_type == economic_type]
    if category_id is not None:
        rows = [row for row in rows if row.category and row.category.id == category_id]
    if envelope_id is not None:
        rows = [row for row in rows if row.envelope and row.envelope.id == envelope_id]
    if project_id is not None:
        rows = [row for row in rows if row.project and row.project.id == project_id]
    if unresolved_only:
        rows = [row for row in rows if not row.fully_reviewed]
    rows.sort(key=lambda row: (row.booked_at, row.source.id), reverse=sort != "oldest")
    return rows, progress


def _decision_timestamp() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _get_or_create_event(db: Session, row: TransactionReviewRow, event_type: str) -> EconomicEvent:
    if row.event is not None:
        row.event.event_type = event_type
        return row.event
    existing_link = db.scalar(
        select(EventSourceLink).where(
            EventSourceLink.source_transaction_id == row.source.id,
            EventSourceLink.link_type == "canonical_source",
        )
    )
    if existing_link:
        event = db.get(EconomicEvent, existing_link.economic_event_id)
        if event:
            event.event_type = event_type
            return event
    account = row.account
    event = EconomicEvent(
        event_type=event_type,
        occurred_at=row.source.booked_at,
        description=row.canonical_merchant or row.source.description_raw[:255],
        amount=abs(row.source.amount),
        currency=row.source.currency,
        account_id=account.id if account else None,
        category_id=row.category.id if row.category else None,
        envelope_id=row.envelope.id if row.envelope else None,
        project_id=row.project.id if row.project else None,
        status="booked",
        confidence=Decimal("1.0"),
    )
    db.add(event)
    db.flush()
    db.add(
        EventSourceLink(
            economic_event_id=event.id,
            source_transaction_id=row.source.id,
            link_type="canonical_source",
            confidence=Decimal("1.0"),
        )
    )
    if row.review:
        row.review.economic_event_id = event.id
    return event


def apply_transaction_decision(
    db: Session,
    *,
    candidate_keys: list[str],
    economic_type: str | None = None,
    category_id: int | None = None,
    envelope_decision: str | None = None,
    envelope_id: int | None = None,
    project_decision: str | None = None,
    project_id: int | None = None,
    create_rule: bool = False,
) -> int:
    if not candidate_keys:
        raise ValueError("Mindestens eine Transaktion auswählen")
    if economic_type is not None and economic_type not in TYPE_VALUES:
        raise ValueError("Ungültiger wirtschaftlicher Typ")
    if envelope_decision not in {None, "assigned", "no_envelope", "later"}:
        raise ValueError("Ungültige Umschlagentscheidung")
    if project_decision not in {None, *PROJECT_DECISIONS}:
        raise ValueError("Ungültige Projektentscheidung")
    if envelope_decision == "assigned" and (
        db.get(Envelope, envelope_id) is None or not db.get(Envelope, envelope_id).is_active
    ):
        raise ValueError("Aktiver Umschlag erforderlich")
    if project_decision == "assigned" and db.get(Project, project_id) is None:
        raise ValueError("Projekt erforderlich")
    rows, _ = build_transaction_review(db, sort="oldest")
    by_key = {row.key: row for row in rows}
    selected = [by_key[key] for key in dict.fromkeys(candidate_keys) if key in by_key]
    if len(selected) != len(set(candidate_keys)):
        raise ValueError("Mindestens eine Auswahl ist keine Transaktion")
    if create_rule:
        merchants = {row.canonical_merchant.casefold() for row in selected}
        accounts = {row.account.id if row.account else None for row in selected}
        condition: dict[str, object] = {}
        if len(merchants) == 1:
            condition["merchant_pattern"] = next(iter(merchants))
        elif len(accounts) == 1 and None not in accounts:
            condition["account_id"] = next(iter(accounts))
        if not condition:
            raise ValueError("Auswahl besitzt keine konsistente Regelbasis")
        action: dict[str, object] = {}
        if economic_type is not None:
            action["economic_type"] = economic_type
        if category_id is not None:
            action["category_id"] = category_id
        if envelope_decision is not None:
            action["envelope_decision"] = envelope_decision
            if envelope_id is not None:
                action["envelope_id"] = envelope_id
        if project_decision is not None:
            action["project_decision"] = project_decision
            if project_id is not None:
                action["project_id"] = project_id
        if not action:
            raise ValueError("Regel benötigt mindestens einen Vorschlag")
        highest = db.scalar(
            select(AssignmentRule.priority).order_by(AssignmentRule.priority.desc()).limit(1)
        )
        db.add(
            AssignmentRule(
                rule_type="transaction_review_suggestion",
                priority=(highest or 0) + 10,
                enabled=True,
                condition_json=condition,
                action_json=action,
            )
        )
    now = _decision_timestamp()
    for row in selected:
        event = row.event
        if economic_type is not None:
            record = db.scalar(
                select(EconomicTypeAssignmentDecision).where(
                    EconomicTypeAssignmentDecision.candidate_key == row.key
                )
            )
            if record is None:
                record = EconomicTypeAssignmentDecision(
                    candidate_key=row.key,
                    source_transaction_id=row.source.id,
                    economic_event_id=row.event.id if row.event else None,
                    decision=economic_type,
                    decided_at=now,
                )
                db.add(record)
            else:
                record.decision = economic_type
                record.decided_at = now
                record.updated_at = now
            event = _get_or_create_event(db, row, economic_type)
            record.economic_event_id = event.id
        if category_id is not None:
            category = db.get(Category, category_id)
            if category is None or not category.is_active:
                raise ValueError("Aktive Kategorie erforderlich")
            record = db.scalar(
                select(CategoryAssignmentDecision).where(
                    CategoryAssignmentDecision.candidate_key == row.key
                )
            )
            if record is None:
                db.add(
                    CategoryAssignmentDecision(
                        candidate_key=row.key,
                        source_transaction_id=row.source.id,
                        economic_event_id=event.id if event else None,
                        category_id=category_id,
                        decided_at=now,
                    )
                )
            else:
                record.category_id = category_id
                record.decided_at = now
                record.updated_at = now
            if event:
                event.category_id = category_id
        if envelope_decision is not None:
            record = db.scalar(
                select(EnvelopeAssignmentDecision).where(
                    EnvelopeAssignmentDecision.candidate_key == row.key
                )
            )
            if record is None:
                db.add(
                    EnvelopeAssignmentDecision(
                        candidate_key=row.key,
                        source_transaction_id=row.source.id,
                        economic_event_id=event.id if event else None,
                        decision=envelope_decision,
                        envelope_id=envelope_id if envelope_decision == "assigned" else None,
                        decided_at=now if envelope_decision != "later" else None,
                    )
                )
            else:
                record.decision = envelope_decision
                record.envelope_id = envelope_id if envelope_decision == "assigned" else None
                record.updated_at = now
                record.decided_at = now if envelope_decision != "later" else None
            if event:
                event.envelope_id = envelope_id if envelope_decision == "assigned" else None
        if project_decision is not None:
            record = db.scalar(
                select(ProjectAssignmentDecision).where(
                    ProjectAssignmentDecision.candidate_key == row.key
                )
            )
            if record is None:
                db.add(
                    ProjectAssignmentDecision(
                        candidate_key=row.key,
                        source_transaction_id=row.source.id,
                        economic_event_id=event.id if event else None,
                        decision=project_decision,
                        project_id=project_id if project_decision == "assigned" else None,
                        decided_at=now if project_decision != "later" else None,
                    )
                )
            else:
                record.decision = project_decision
                record.project_id = project_id if project_decision == "assigned" else None
                record.updated_at = now
                record.decided_at = now if project_decision != "later" else None
            if event:
                event.project_id = project_id if project_decision == "assigned" else None
    return len(selected)


def create_project(
    db: Session, *, name: str, starts_at=None, ends_at=None, notes: str | None = None
) -> Project:
    clean = " ".join(name.split())
    if not clean:
        raise ValueError("Projektname darf nicht leer sein")
    if starts_at and ends_at and ends_at < starts_at:
        raise ValueError("Das Enddatum darf nicht vor dem Startdatum liegen")
    if db.scalar(select(Project).where(Project.name.ilike(clean))):
        raise ValueError("Projekt existiert bereits")
    project = Project(name=clean, starts_at=starts_at, ends_at=ends_at, notes=notes)
    db.add(project)
    db.flush()
    return project

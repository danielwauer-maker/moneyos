from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Category,
    CategoryAssignmentDecision,
    EconomicEvent,
    EventSourceLink,
    RawImportRecord,
    ReviewItem,
    SourceTransaction,
)
from app.domain.sparda import (
    CategorySuggestion,
    MerchantExtraction,
    SpardaDecision,
    classify_sparda_transaction,
)
from app.security.redaction import redact_text
from app.services.categories import (
    category_by_path,
    ensure_optimized_category_hierarchy,
)

HIGH_CONFIDENCE = Decimal("0.95")
MEDIUM_CONFIDENCE = Decimal("0.70")


@dataclass(frozen=True)
class ReclassificationCandidate:
    source: SourceTransaction
    event: EconomicEvent | None
    review: ReviewItem | None
    merchant: MerchantExtraction
    suggestion: CategorySuggestion | None
    action: str
    old_category: str
    new_category: str | None
    protected: bool


@dataclass(frozen=True)
class ReclassificationReport:
    total_evaluated: int
    high_confidence_auto_confirm_candidates: int
    medium_confidence_suggestions: int
    review_only_high_confidence_suggestions: int
    unresolved: int
    protected_manual_decisions: int
    already_correct: int
    auto_confirmed: int
    suggested: int
    taxonomy_changes: int
    category_changes: dict[str, int]
    corrected_generic_examples: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "total_evaluated": self.total_evaluated,
            "high_confidence_auto_confirm_candidates": (
                self.high_confidence_auto_confirm_candidates
            ),
            "medium_confidence_suggestions": self.medium_confidence_suggestions,
            "review_only_high_confidence_suggestions": (
                self.review_only_high_confidence_suggestions
            ),
            "unresolved": self.unresolved,
            "protected_manual_decisions": self.protected_manual_decisions,
            "already_correct": self.already_correct,
            "auto_confirmed": self.auto_confirmed,
            "suggested": self.suggested,
            "taxonomy_changes": self.taxonomy_changes,
            "category_changes": self.category_changes,
            "corrected_generic_examples": list(self.corrected_generic_examples),
        }


def _category_path(category: Category | None) -> str:
    if category is None:
        return "Offen"
    return f"{category.parent.name} / {category.name}" if category.parent else category.name


def source_classification(
    source: SourceTransaction,
    raw: RawImportRecord | None,
) -> SpardaDecision:
    payload = raw.raw_payload_json if raw is not None else {}
    description_parts = source.description_raw.split(" | ", 1)
    booking_text = payload.get("Buchungstext", "") or description_parts[0]
    purpose = payload.get("Verwendungszweck", "")
    if not purpose and len(description_parts) > 1:
        purpose = description_parts[1]
    return classify_sparda_transaction(
        amount=source.amount,
        counterparty=(payload.get("Name Zahlungsbeteiligter", "") or source.merchant_raw or ""),
        booking_text=booking_text,
        purpose=purpose,
        note=payload.get("Bemerkung", ""),
    )


def _load_candidates(db: Session) -> list[ReclassificationCandidate]:
    sources = list(
        db.scalars(
            select(SourceTransaction)
            .where(SourceTransaction.source_system == "sparda")
            .order_by(SourceTransaction.id)
        )
    )
    raw_ids = [source.raw_record_id for source in sources if source.raw_record_id is not None]
    raw_by_id = {
        raw.id: raw
        for raw in db.scalars(select(RawImportRecord).where(RawImportRecord.id.in_(raw_ids)))
    }
    source_ids = [source.id for source in sources]
    links = list(
        db.scalars(
            select(EventSourceLink).where(EventSourceLink.source_transaction_id.in_(source_ids))
        )
    )
    event_ids = [link.economic_event_id for link in links]
    events = list(
        db.scalars(
            select(EconomicEvent)
            .where(EconomicEvent.id.in_(event_ids))
            .options(selectinload(EconomicEvent.category).selectinload(Category.parent))
        )
    )
    events_by_id = {event.id: event for event in events}
    event_by_source = {
        link.source_transaction_id: events_by_id.get(link.economic_event_id) for link in links
    }
    reviews = list(
        db.scalars(
            select(ReviewItem)
            .where(ReviewItem.source_transaction_id.in_(source_ids), ReviewItem.status == "open")
            .order_by(ReviewItem.id)
        )
    )
    review_by_source: dict[int, ReviewItem] = {}
    for review in reviews:
        if review.source_transaction_id is not None:
            review_by_source.setdefault(review.source_transaction_id, review)
    protected_source_ids = set(
        db.scalars(
            select(CategoryAssignmentDecision.source_transaction_id).where(
                CategoryAssignmentDecision.source_transaction_id.in_(source_ids)
            )
        )
    )
    protected_event_ids = set(
        db.scalars(
            select(CategoryAssignmentDecision.economic_event_id).where(
                CategoryAssignmentDecision.economic_event_id.in_(event_ids)
            )
        )
    )

    candidates: list[ReclassificationCandidate] = []
    for source in sources:
        event = event_by_source.get(source.id)
        review = review_by_source.get(source.id)
        decision = source_classification(source, raw_by_id.get(source.raw_record_id))
        merchant = decision.merchant
        if merchant is None:  # defensive; current classifier always supplies one
            raise ValueError("Sparda merchant extraction missing")
        suggestion = decision.category
        protected = source.id in protected_source_ids or (
            event is not None and event.id in protected_event_ids
        )
        old_category = _category_path(event.category if event else None)
        new_category = suggestion.path if suggestion else None
        if protected:
            action = "protected"
        elif suggestion is None or decision.event_type == "transfer":
            action = "unresolved"
        elif event is None:
            action = (
                "suggest_high_review"
                if suggestion.confidence >= HIGH_CONFIDENCE
                else (
                    "suggest_medium" if suggestion.confidence >= MEDIUM_CONFIDENCE else "unresolved"
                )
            )
        elif suggestion.confidence >= HIGH_CONFIDENCE:
            action = "already_correct" if old_category == suggestion.path else "auto_confirm"
        elif suggestion.confidence >= MEDIUM_CONFIDENCE:
            action = "suggest_medium"
        else:
            action = "unresolved"
        candidates.append(
            ReclassificationCandidate(
                source=source,
                event=event,
                review=review,
                merchant=merchant,
                suggestion=suggestion,
                action=action,
                old_category=old_category,
                new_category=new_category,
                protected=protected,
            )
        )
    return candidates


def _report(
    candidates: list[ReclassificationCandidate],
    *,
    auto_confirmed: int = 0,
    suggested: int = 0,
    taxonomy_changes: int = 0,
) -> ReclassificationReport:
    category_changes = Counter(
        f"{candidate.old_category} → {candidate.new_category}"
        for candidate in candidates
        if candidate.action == "auto_confirm" and candidate.new_category is not None
    )
    examples: list[str] = []
    for candidate in candidates:
        if (
            candidate.merchant.raw_counterparty.casefold().startswith("dz bank")
            and candidate.merchant.canonical_merchant.casefold()
            != candidate.merchant.raw_counterparty.casefold()
            and candidate.new_category
        ):
            label = (
                f"DZ BANK AG → {redact_text(candidate.merchant.canonical_merchant)[:80]} "
                f"→ {candidate.new_category}"
            )
            if label not in examples:
                examples.append(label)
            if len(examples) == 8:
                break
    return ReclassificationReport(
        total_evaluated=len(candidates),
        high_confidence_auto_confirm_candidates=sum(
            candidate.action == "auto_confirm" for candidate in candidates
        ),
        medium_confidence_suggestions=sum(
            candidate.action == "suggest_medium" for candidate in candidates
        ),
        review_only_high_confidence_suggestions=sum(
            candidate.action == "suggest_high_review" for candidate in candidates
        ),
        unresolved=sum(candidate.action == "unresolved" for candidate in candidates),
        protected_manual_decisions=sum(candidate.protected for candidate in candidates),
        already_correct=sum(candidate.action == "already_correct" for candidate in candidates),
        auto_confirmed=auto_confirmed,
        suggested=suggested,
        taxonomy_changes=taxonomy_changes,
        category_changes=dict(sorted(category_changes.items())),
        corrected_generic_examples=tuple(examples),
    )


def plan_sparda_reclassification(db: Session) -> ReclassificationReport:
    return _report(_load_candidates(db))


def apply_sparda_reclassification(db: Session) -> ReclassificationReport:
    taxonomy_changes = ensure_optimized_category_hierarchy(db)
    candidates = _load_candidates(db)
    auto_confirmed = 0
    suggested = 0
    for candidate in candidates:
        suggestion = candidate.suggestion
        if suggestion is None or candidate.protected:
            continue
        category = category_by_path(db, suggestion.parent, suggestion.child)
        if candidate.action == "auto_confirm" and candidate.event is not None:
            candidate.event.category = category
            candidate.event.confidence = suggestion.confidence
            auto_confirmed += 1
            continue
        if candidate.action not in {"suggest_medium", "suggest_high_review"}:
            continue
        review = candidate.review
        if review is None:
            review = ReviewItem(
                economic_event_id=candidate.event.id if candidate.event else None,
                source_transaction_id=candidate.source.id,
                review_type="category_assignment",
                proposed_event_type=(
                    candidate.event.event_type
                    if candidate.event
                    else ("expense" if candidate.source.amount < 0 else None)
                ),
                confidence=suggestion.confidence,
                explanation="Kategorie-Vorschlag aus sicherer Händlerextraktion",
                status="open",
            )
            db.add(review)
        review.proposed_category = category
        review.confidence = suggestion.confidence
        review.explanation = (
            f"{candidate.merchant.reason}; {suggestion.reason}. "
            "Nur Vorschlag – keine automatische Zuordnung."
        )
        suggested += 1
    db.flush()
    return _report(
        candidates,
        auto_confirmed=auto_confirmed,
        suggested=suggested,
        taxonomy_changes=taxonomy_changes,
    )

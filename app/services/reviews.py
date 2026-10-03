from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import RawImportRecord, ReviewItem, SourceTransaction, SourceTransactionAccount
from app.services.transaction_details import derive_transaction_detail
from app.services.transaction_review import build_transaction_review

TYPE_LABELS = {
    "expense": "Ausgabe",
    "income": "Einnahme",
    "transfer": "Transfer",
    "refund": "Refund",
}


@dataclass(frozen=True)
class ReviewView:
    review: ReviewItem
    booked_at: datetime | None
    payee: str
    secondary_detail: str
    canonical_merchant: str
    amount: Decimal | None
    source_account: str
    proposed_type: str
    proposed_category: str
    proposed_envelope: str
    proposed_project: str


def build_review_view(review: ReviewItem, raw: RawImportRecord | None = None) -> ReviewView:
    source = review.source_transaction
    event = review.economic_event
    display = derive_transaction_detail(
        source,
        raw,
        fallback=event.description if event else "Quelltransaktion",
    )
    source_account = "Nicht zugeordnet"
    if source:
        account_link = next((link for link in source.account_links if link.role == "source"), None)
        if account_link:
            source_account = account_link.account.name
    if source_account == "Nicht zugeordnet" and event and event.account:
        source_account = event.account.name

    category = review.proposed_category
    if category and category.parent:
        category_label = f"{category.parent.name} / {category.name}"
    elif category:
        category_label = category.name
    else:
        category_label = "Offen"
    proposed = review.proposed_event_type
    proposed_label = f"Vorschlag: {TYPE_LABELS.get(proposed, proposed)}" if proposed else "Offen"
    return ReviewView(
        review=review,
        booked_at=source.booked_at if source else (event.occurred_at if event else None),
        payee=display.raw_counterparty,
        secondary_detail=display.secondary_detail,
        canonical_merchant=display.canonical_merchant,
        amount=source.amount if source else (event.amount if event else None),
        source_account=source_account,
        proposed_type=proposed_label,
        proposed_category=category_label,
        proposed_envelope=(review.proposed_envelope.name if review.proposed_envelope else "Keiner"),
        proposed_project=(review.proposed_project.name if review.proposed_project else "Keines"),
    )


def actionable_review_items(db: Session) -> list[ReviewItem]:
    """Return only review items that still require a user decision.

    Importers deliberately keep their immutable review evidence. A later explicit
    transaction decision can make a source transaction complete while its original
    ReviewItem still has status open. The inbox follows the current decision state
    rather than stale importer workflow state.
    """
    rows, _ = build_transaction_review(db)
    by_source = {row.source.id: row for row in rows}
    reviews = list(
        db.scalars(
            select(ReviewItem)
            .where(ReviewItem.status == "open")
            .options(
                selectinload(ReviewItem.economic_event),
                selectinload(ReviewItem.proposed_category),
                selectinload(ReviewItem.proposed_envelope),
                selectinload(ReviewItem.proposed_project),
                selectinload(ReviewItem.source_transaction)
                .selectinload(SourceTransaction.account_links)
                .selectinload(SourceTransactionAccount.account),
            )
            .order_by(ReviewItem.id)
        )
    )
    return [
        review
        for review in reviews
        if review.source_transaction_id is None
        or review.source_transaction_id not in by_source
        or not by_source[review.source_transaction_id].fully_reviewed
    ]


def actionable_review_count(db: Session) -> int:
    return len(actionable_review_items(db))

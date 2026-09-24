from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.db.models import RawImportRecord, ReviewItem
from app.services.transaction_details import derive_transaction_detail

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
    )

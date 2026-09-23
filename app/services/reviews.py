from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.db.models import ReviewItem
from app.security.redaction import redact_text

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
    amount: Decimal | None
    source_account: str
    proposed_type: str
    proposed_category: str
    proposed_envelope: str


def build_review_view(review: ReviewItem) -> ReviewView:
    source = review.source_transaction
    event = review.economic_event
    candidate = source.merchant_raw if source and source.merchant_raw else None
    if not candidate and event:
        candidate = event.description
    payee = redact_text(candidate or "Quelltransaktion")[:160]
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
        payee=payee,
        amount=source.amount if source else (event.amount if event else None),
        source_account=source_account,
        proposed_type=proposed_label,
        proposed_category=category_label,
        proposed_envelope=(review.proposed_envelope.name if review.proposed_envelope else "Keiner"),
    )

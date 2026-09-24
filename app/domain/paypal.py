from decimal import Decimal

TECHNICAL_TYPES = frozenset(
    {
        "allgemeine gutschrift auf kreditkarte",
        "allgemeine abbuchung von kreditkarte",
        "bankgutschrift auf paypal-konto",
        "allgemeine autorisierung",
        "einbehaltung für offene autorisierung",
        "rückbuchung allgemeiner einbehaltung",
    }
)

COMPLETED_PURCHASE_TYPES = {
    "zahlung",
    "allgemeine zahlung",
    "express-kaufabwicklung",
}


def classify_paypal_type(transaction_type: str) -> str | None:
    key = transaction_type.casefold().strip()
    if key in TECHNICAL_TYPES or any(
        marker in key
        for marker in ("autorisierung", "einbehaltung", "authorization", "temporary hold")
    ):
        return None
    if "rückzahlung" in key or "refund" in key:
        return "refund"
    if key in COMPLETED_PURCHASE_TYPES:
        return "expense"
    return "review"


def classify_paypal_semantic(
    transaction_type: str,
    status: str,
    gross: Decimal,
    *,
    has_name: bool,
) -> str:
    """Return a conservative, mutually exclusive PayPal row semantic."""
    key = transaction_type.casefold().strip()
    if classify_paypal_type(transaction_type) is None:
        return (
            "funding"
            if key in TECHNICAL_TYPES
            and any(marker in key for marker in ("kreditkarte", "bankgutschrift"))
            else "technical"
        )
    if classify_paypal_type(transaction_type) == "refund":
        return "refund"
    if status.casefold().strip() in {"abgeschlossen", "completed"} and gross < 0 and has_name:
        return "merchant_payment"
    return "unresolved"

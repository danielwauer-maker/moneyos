TECHNICAL_TYPES = {
    "Allgemeine Gutschrift auf Kreditkarte",
    "Allgemeine Abbuchung von Kreditkarte",
    "Bankgutschrift auf PayPal-Konto",
    "Allgemeine Autorisierung",
    "Einbehaltung für offene Autorisierung",
    "Rückbuchung allgemeiner Einbehaltung",
}

COMPLETED_PURCHASE_TYPES = {
    "Zahlung",
    "Allgemeine Zahlung",
    "Express-Kaufabwicklung",
}


def classify_paypal_type(transaction_type: str) -> str | None:
    if transaction_type in TECHNICAL_TYPES:
        return None
    if transaction_type == "Rückzahlung":
        return "refund"
    if transaction_type in COMPLETED_PURCHASE_TYPES:
        return "expense"
    return "review"

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from app.domain.accounting import AccountMovementKind, classify_account_movement


@dataclass(frozen=True)
class CategorySuggestion:
    parent: str
    child: str | None
    confidence: Decimal

    @property
    def path(self) -> str:
        return f"{self.parent} / {self.child}" if self.child else self.parent


@dataclass(frozen=True)
class SpardaDecision:
    event_type: str | None
    confidence: Decimal
    semantic: str
    link_type: str | None = None
    review_type: str | None = None
    explanation: str | None = None
    category: CategorySuggestion | None = None
    target_account_type: str | None = None


def _searchable(*values: str) -> str:
    combined = " ".join(value for value in values if value)
    normalized = unicodedata.normalize("NFKD", combined).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", normalized.upper()).strip()


CATEGORY_RULES: tuple[tuple[tuple[str, ...], str, str | None, str], ...] = (
    (("LIDL", "REWE", "EDEKA", "KAUFLAND"), "Lebensmittel", "Supermarkt", "0.98"),
    (("BACKEREI",), "Lebensmittel", "Bäckerei", "0.97"),
    (("METZGEREI",), "Lebensmittel", "Metzgerei", "0.97"),
    (
        ("ARAL", "SHELL", "ESSO", "JET TANK", "TOTALENERGIES", "TANKSTELLE"),
        "Auto & Mobilität",
        "Tanken",
        "0.96",
    ),
    (("DM DROGERIE", "ROSSMANN"), "Drogerie & Pflege", None, "0.97"),
    (("APOTHEKE",), "Gesundheit", "Apotheke", "0.97"),
    (("PARKSTER",), "Auto & Mobilität", "Parken", "0.98"),
    (("BAUMARKT", "BAUHAUS", "HORNBACH", "TOOM"), "Wohnen & Haushalt", "Baumarkt", "0.97"),
    (("DEUTSCHE GLASFASER",), "Kommunikation", "Internet", "0.99"),
    (("KLARMOBIL",), "Kommunikation", "Mobilfunk", "0.99"),
    (("RUNDFUNKBEITRAG", "ARD ZDF DEUTSCHLANDRADIO"), "Wohnen & Haushalt", "Rundfunk", "0.99"),
    (
        ("STROM", "ENERGIEVERSORG", "VATTENFALL", "LICHTBLICK", "ENBW", "E.ON"),
        "Wohnen & Haushalt",
        "Strom",
        "0.94",
    ),
    (("KFZ-STEUER", "KRAFTFAHRZEUGSTEUER"), "Auto & Mobilität", "KFZ-Steuer", "0.99"),
    (("DONATE.JW.ORG",), "Spenden & Unterstützung", None, "0.99"),
)


def suggest_sparda_category(
    counterparty: str, booking_text: str, purpose: str
) -> CategorySuggestion | None:
    text = _searchable(counterparty, booking_text, purpose)
    for patterns, parent, child, confidence in CATEGORY_RULES:
        if any(pattern in text for pattern in patterns):
            return CategorySuggestion(parent, child, Decimal(confidence))
    return None


def classify_sparda_transaction(
    *,
    amount: Decimal,
    counterparty: str,
    booking_text: str,
    purpose: str,
    note: str = "",
) -> SpardaDecision:
    text = _searchable(counterparty, booking_text, purpose, note)
    category = suggest_sparda_category(counterparty, booking_text, purpose)

    if "AMERICAN EXPRESS" in text:
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.CARD_SETTLEMENT),
            Decimal("1"),
            "american_express_settlement",
            link_type="settlement_leg",
        )
    if "PAYPAL EUROPE" in text:
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.OWN_ACCOUNT_TRANSFER),
            Decimal("1"),
            "paypal_funding_leg",
            link_type="funding_leg",
        )
    if "ABRECHNUNG AMAZON VISA" in text or ("AMAZON VISA" in text and "ABRECHNUNG" in text):
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.CARD_SETTLEMENT),
            Decimal("1"),
            "amazon_visa_settlement",
            link_type="settlement_leg",
            review_type="missing_card_source",
            explanation=(
                "Amazon-Visa-Abrechnung erkannt; die zugrunde liegenden Kartentransaktionen "
                "fehlen noch und müssen später abgeglichen werden."
            ),
        )
    if any(pattern in text for pattern in ("AUSZAHLUNG GIROCARD", "GELDAUTOMAT", "BARAUSZAHLUNG")):
        target = None
        if "PORTEMONNAIE" in text:
            target = "cash_wallet"
        elif "TRESOR" in text:
            target = "cash_vault"
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.CASH_WITHDRAWAL),
            Decimal("1" if target else "0.80"),
            "cash_withdrawal",
            link_type="canonical_source",
            review_type=None if target else "transfer_target",
            explanation=(
                None
                if target
                else "Bargeldabhebung erkannt; Ziel Portemonnaie oder Tresor ist nicht eindeutig."
            ),
            target_account_type=target,
        )

    if amount > 0:
        if "FINANZAMT" in text:
            return SpardaDecision(
                classify_account_movement(AccountMovementKind.INCOME),
                Decimal("0.99"),
                "tax_refund_income",
                link_type="canonical_source",
            )
        if any(
            pattern in text
            for pattern in (
                "GEHALT",
                "LOHN",
                "BUNDESAGENTUR FUR ARBEIT",
                "ARBEITSLOSENGELD",
                "FAMILIENKASSE",
                "KINDERGELD",
            )
        ):
            return SpardaDecision(
                classify_account_movement(AccountMovementKind.INCOME),
                Decimal("0.99"),
                "high_confidence_income",
                link_type="canonical_source",
            )
        if any(
            pattern in text
            for pattern in (
                "ERSTATTUNG",
                "RUCKERSTATTUNG",
                "RUCKZAHLUNG",
                "RETOURE",
                "REFUND",
                "STORNO",
            )
        ):
            return SpardaDecision(
                classify_account_movement(AccountMovementKind.REFUND),
                Decimal("0.95"),
                "merchant_refund",
                link_type="canonical_source",
                category=category,
            )
        return SpardaDecision(
            None,
            Decimal("0.50"),
            "ambiguous_credit",
            review_type="economic_type",
            explanation=(
                "Positive Buchung ist nicht eindeutig Einkommen, Erstattung oder Privattransfer."
            ),
        )

    if amount < 0 and category is not None:
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.ORDINARY_EXPENSE),
            category.confidence,
            "categorized_expense",
            link_type="canonical_source",
            category=category,
        )
    return SpardaDecision(
        None,
        Decimal("0.55"),
        "ambiguous_debit" if amount < 0 else "zero_amount",
        review_type="economic_type",
        explanation=(
            "Belastung ist nicht sicher von Transfer oder Ausgabe unterscheidbar."
            if amount < 0
            else "Nullbetrag kann nicht automatisch wirtschaftlich eingeordnet werden."
        ),
        category=category,
    )

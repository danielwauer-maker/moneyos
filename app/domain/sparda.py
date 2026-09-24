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
    reason: str = "Deterministische Kategorie-Regel"

    @property
    def path(self) -> str:
        return f"{self.parent} / {self.child}" if self.child else self.parent


@dataclass(frozen=True)
class MerchantExtraction:
    raw_counterparty: str
    canonical_merchant: str
    detail: str
    source: str
    confidence: Decimal
    reason: str
    unambiguous: bool


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
    merchant: MerchantExtraction | None = None


@dataclass(frozen=True)
class _CategoryRule:
    patterns: tuple[str, ...]
    parent: str
    child: str | None
    confidence: Decimal
    reason: str


def _searchable(*values: str) -> str:
    combined = " ".join(value for value in values if value)
    normalized = unicodedata.normalize("NFKD", combined).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", normalized.upper()).strip()


def _display_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip(" |")


GENERIC_COUNTERPARTIES = (
    "DZ BANK AG",
    "DZ BANK",
)


def _is_generic_counterparty(counterparty: str) -> bool:
    normalized = _searchable(counterparty)
    return any(
        normalized == item or normalized.startswith(f"{item} ") for item in GENERIC_COUNTERPARTIES
    )


def _structured_card_merchant(purpose: str) -> str | None:
    if not purpose.strip():
        return None
    if "/" not in purpose and "\n" not in purpose and "\r" not in purpose:
        return None
    first_segment = _display_text(re.split(r"[/\r\n]", purpose, maxsplit=1)[0])
    if not first_segment or len(first_segment) < 2:
        return None
    noise = _searchable(first_segment)
    if noise in {"ZAHLUNG", "KARTENZAHLUNG", "LASTSCHRIFT", "EC-KARTENZAHLUNG"}:
        return None
    return first_segment[:160]


def extract_sparda_merchant(
    *, counterparty: str, booking_text: str, purpose: str, note: str = ""
) -> MerchantExtraction:
    raw_counterparty = _display_text(counterparty)
    detail = _display_text(purpose or note)
    booking = _searchable(booking_text)
    structured_detail = (
        _structured_card_merchant(purpose)
        if any(token in booking for token in ("KARTENZAHLUNG", "DEBIT MC", "MAESTRO"))
        else None
    )
    if structured_detail:
        return MerchantExtraction(
            raw_counterparty=raw_counterparty,
            canonical_merchant=structured_detail,
            detail=detail,
            source="payment_detail",
            confidence=Decimal("0.99"),
            reason="Strukturierter Kartenumsatz: Händler vor dem ersten Detailtrenner",
            unambiguous=True,
        )
    if raw_counterparty and not _is_generic_counterparty(raw_counterparty):
        return MerchantExtraction(
            raw_counterparty=raw_counterparty,
            canonical_merchant=raw_counterparty[:160],
            detail=detail,
            source="counterparty",
            confidence=Decimal("0.98"),
            reason="Spezifischer Zahlungspartner",
            unambiguous=True,
        )
    if detail:
        candidate = _structured_card_merchant(detail) or detail[:160]
        return MerchantExtraction(
            raw_counterparty=raw_counterparty,
            canonical_merchant=candidate,
            detail=detail,
            source="purpose",
            confidence=Decimal("0.70"),
            reason="Generischer Zahlungspartner; Verwendungszweck als vorsichtiger Fallback",
            unambiguous=False,
        )
    fallback = raw_counterparty or _display_text(booking_text) or "Unbekannter Zahlungspartner"
    return MerchantExtraction(
        raw_counterparty=raw_counterparty,
        canonical_merchant=fallback[:160],
        detail="",
        source="generic_counterparty",
        confidence=Decimal("0.30"),
        reason="Nur generischer Zahlungspartner verfügbar",
        unambiguous=False,
    )


CATEGORY_RULES: tuple[_CategoryRule, ...] = (
    _CategoryRule(
        (
            r"\bLIDL",
            r"\bALDI",
            r"\bREWE",
            r"\bEDEKA",
            r"\bSPAR\b",
            r"\bCOOP\b",
            r"\bKAUFLAND",
            r"\bNETTO\b",
            r"\bGLOBUS MARKTHALLE",
            r"\bALBERT HEIJN",
        ),
        "Lebensmittel",
        "Supermarkt",
        Decimal("0.98"),
        "Spezifisches Supermarkt-Muster",
    ),
    _CategoryRule(
        (r"\bBAGERI\b", r"\bBACKEREI\b"),
        "Lebensmittel",
        "Bäckerei",
        Decimal("0.98"),
        "Spezifisches Bäckerei-Muster",
    ),
    _CategoryRule(
        (r"\bMETZGEREI\b",),
        "Lebensmittel",
        "Metzgerei",
        Decimal("0.97"),
        "Spezifisches Metzgerei-Muster",
    ),
    _CategoryRule(
        (r"\bSCANDLINES\b",),
        "Auto & Mobilität",
        "Fähre",
        Decimal("0.99"),
        "Spezifischer Fähranbieter",
    ),
    _CategoryRule(
        (
            r"\bARAL\b",
            r"\bSHELL\b",
            r"\bESSO\b",
            r"\bJET TANK",
            r"\bTOTALENERGIES\b",
            r"\bTANKSTELLE",
            r"TANKSTELLENAUTOM",
        ),
        "Auto & Mobilität",
        "Tanken",
        Decimal("0.97"),
        "Spezifisches Tankstellen-Muster",
    ),
    _CategoryRule(
        (r"\bPARKSTER\b",),
        "Auto & Mobilität",
        "Parken",
        Decimal("0.98"),
        "Spezifischer Parkanbieter",
    ),
    _CategoryRule(
        (r"\bDM[- ]?FIL", r"\bDM DROGERIE", r"\bROSSMANN\b"),
        "Drogerie & Körperpflege",
        "Drogerie",
        Decimal("0.97"),
        "Spezifisches Drogerie-Muster",
    ),
    _CategoryRule(
        (r"\bAPOTHEKE\b",),
        "Gesundheit",
        "Apotheke / Medikamente",
        Decimal("0.97"),
        "Spezifisches Apotheken-Muster",
    ),
    _CategoryRule(
        (r"\bBAUHAUS\b", r"\bHORNBACH\b", r"\bTOOM\b", r"\bBAUMARKT\b"),
        "Wohnen & Haushalt",
        "Baumarkt / Renovierung",
        Decimal("0.97"),
        "Spezifisches Baumarkt-Muster",
    ),
    _CategoryRule(
        (r"\bDEUTSCHE GLASFASER\b",),
        "Kommunikation",
        "Internet",
        Decimal("0.99"),
        "Spezifischer Internetanbieter",
    ),
    _CategoryRule(
        (r"\bKLARMOBIL\b",),
        "Kommunikation",
        "Mobilfunk",
        Decimal("0.99"),
        "Spezifischer Mobilfunkanbieter",
    ),
    _CategoryRule(
        (r"\bSTROM\b", r"\bVATTENFALL\b", r"\bLICHTBLICK\b", r"\bENBW\b", r"\bE[.]ON\b"),
        "Wohnen & Haushalt",
        "Strom",
        Decimal("0.95"),
        "Spezifischer Stromanbieter oder Stromzweck",
    ),
    _CategoryRule(
        (r"\bKFZ[- ]STEUER\b", r"\bKRAFTFAHRZEUGSTEUER\b"),
        "Auto & Mobilität",
        "Kfz-Steuer",
        Decimal("0.99"),
        "Eindeutige Kfz-Steuer",
    ),
    _CategoryRule(
        (r"DONATE\.JW\.ORG",),
        "Spenden & Unterstützung",
        "Spenden",
        Decimal("0.99"),
        "Eindeutiger Spendenempfänger",
    ),
    _CategoryRule(
        (r"\bRESTAURANT\b", r"\bBURGERME\b", r"\bBURGER\b"),
        "Gastronomie",
        "Restaurant",
        Decimal("0.82"),
        "Gastronomie-Schlüsselwort; manuelle Bestätigung empfohlen",
    ),
)


def suggest_sparda_category(
    counterparty: str,
    booking_text: str,
    purpose: str,
    note: str = "",
) -> CategorySuggestion | None:
    merchant = extract_sparda_merchant(
        counterparty=counterparty,
        booking_text=booking_text,
        purpose=purpose,
        note=note,
    )
    text = _searchable(merchant.canonical_merchant, merchant.detail)
    for rule in CATEGORY_RULES:
        if any(re.search(pattern, text) for pattern in rule.patterns):
            confidence = min(rule.confidence, merchant.confidence)
            return CategorySuggestion(rule.parent, rule.child, confidence, rule.reason)
    return None


def _income_category(text: str) -> CategorySuggestion | None:
    if any(pattern in text for pattern in ("FAMILIENKASSE", "KINDERGELD")):
        return CategorySuggestion("Einnahmen", "Kindergeld", Decimal("0.99"), "Kindergeld-Muster")
    if any(
        pattern in text
        for pattern in ("GEHALT", "LOHN", "BUNDESAGENTUR FUR ARBEIT", "ARBEITSLOSENGELD")
    ):
        return CategorySuggestion(
            "Einnahmen", "Gehalt / ALG", Decimal("0.99"), "Gehalt-/ALG-Muster"
        )
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
    merchant = extract_sparda_merchant(
        counterparty=counterparty,
        booking_text=booking_text,
        purpose=purpose,
        note=note,
    )
    category = suggest_sparda_category(counterparty, booking_text, purpose, note)

    if "AMERICAN EXPRESS" in text:
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.CARD_SETTLEMENT),
            Decimal("1"),
            "american_express_settlement",
            link_type="settlement_leg",
            merchant=merchant,
        )
    if "PAYPAL EUROPE" in text:
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.OWN_ACCOUNT_TRANSFER),
            Decimal("1"),
            "paypal_funding_leg",
            link_type="funding_leg",
            merchant=merchant,
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
            merchant=merchant,
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
            merchant=merchant,
        )

    if amount > 0:
        if "FINANZAMT" in text:
            return SpardaDecision(
                classify_account_movement(AccountMovementKind.INCOME),
                Decimal("0.99"),
                "tax_refund_income",
                link_type="canonical_source",
                category=CategorySuggestion(
                    "Einnahmen",
                    "Erstattung / Rückzahlung",
                    Decimal("0.99"),
                    "Eindeutige Finanzamt-Erstattung",
                ),
                merchant=merchant,
            )
        income_category = _income_category(text)
        if income_category is not None:
            return SpardaDecision(
                classify_account_movement(AccountMovementKind.INCOME),
                Decimal("0.99"),
                "high_confidence_income",
                link_type="canonical_source",
                category=income_category,
                merchant=merchant,
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
                category=CategorySuggestion(
                    "Einnahmen",
                    "Erstattung / Rückzahlung",
                    Decimal("0.95"),
                    "Eindeutiges Erstattungs-Muster",
                ),
                merchant=merchant,
            )
        return SpardaDecision(
            None,
            Decimal("0.50"),
            "ambiguous_credit",
            review_type="economic_type",
            explanation=(
                "Positive Buchung ist nicht eindeutig Einkommen, Erstattung oder Privattransfer."
            ),
            merchant=merchant,
        )

    if amount < 0 and category is not None:
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.ORDINARY_EXPENSE),
            category.confidence,
            "categorized_expense",
            link_type="canonical_source",
            category=category,
            merchant=merchant,
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
        merchant=merchant,
    )

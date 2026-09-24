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
    matched_field: str | None = None
    matched_text: str | None = None

    @property
    def path(self) -> str:
        return f"{self.parent} / {self.child}" if self.child else self.parent


@dataclass(frozen=True)
class MerchantExtraction:
    raw_counterparty: str
    processor: str | None
    canonical_merchant: str
    secondary_detail: str
    source: str
    confidence: Decimal
    reason: str
    unambiguous: bool

    @property
    def detail(self) -> str:
        """Compatibility alias for the secondary source detail."""
        return self.secondary_detail


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
    "B+S CARD SERVICE",
    "PAYONE",
    "WORLDLINE",
    "NEXI",
    "SUMUP",
    "KLARNA BANK AB",
    "KLARNA",
    "PAYPAL EUROPE",
    "PAYPAL",
)


def _is_generic_counterparty(counterparty: str) -> bool:
    normalized = _searchable(counterparty)
    return any(
        normalized == item or normalized.startswith(f"{item} ") for item in GENERIC_COUNTERPARTIES
    )


def _payment_processor(counterparty: str) -> str | None:
    normalized = _searchable(counterparty)
    if normalized.startswith("KLARNA"):
        return _display_text(counterparty)[:160]
    if normalized.startswith("PAYPAL"):
        return _display_text(counterparty)[:160]
    return None


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


def _useful_secondary_detail(
    *, counterparty: str, booking_text: str, purpose: str, note: str
) -> str:
    raw = _searchable(counterparty)
    for value in (purpose, note, booking_text):
        candidate = _display_text(value)
        normalized = _searchable(candidate)
        if not candidate or normalized == raw:
            continue
        if normalized in {
            "KARTENZAHLUNG",
            "KARTENZAHLUNG DEBIT MC",
            "LASTSCHRIFT",
            "UBERWEISUNG",
            "GUTSCHRIFT",
        }:
            continue
        return candidate[:240]
    return ""


def _intermediary_detail_merchant(*, processor: str, detail: str) -> str | None:
    """Extract a merchant only from explicit provider detail, never from IDs."""
    searchable_processor = _searchable(processor)
    if not detail or not any(
        searchable_processor.startswith(prefix) for prefix in ("KLARNA", "PAYPAL")
    ):
        return None
    candidate: str | None = None
    patterns = (
        r"(?:PURCHASE\s+AT|KAUF\s+BEI|ZAHLUNG\s+AN|PAYMENT\s+TO)\s+(.+)",
        r"(?:PAYPAL\s*[*:/-]\s*)(.+)",
    )
    for pattern in patterns:
        match = re.search(pattern, detail, flags=re.IGNORECASE)
        if match:
            candidate = match.group(1)
            break
    if candidate is None and re.search(r"\bEREF\s*:", detail, flags=re.IGNORECASE):
        candidate = re.split(r"\bEREF\s*:", detail, maxsplit=1, flags=re.IGNORECASE)[0]
    if candidate is None:
        return None
    candidate = re.split(
        r"\b(?:EREF|REFERENCE|TRANSACTION\s+ID|TRANSACTIONID)\b",
        candidate,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    candidate = _display_text(candidate).strip("*:;,-")
    normalized = _searchable(candidate)
    if not candidate or len(candidate) < 2 or not re.search(r"[A-ZÄÖÜa-zäöüß]", candidate):
        return None
    if normalized in {
        "INSTANT TRANSFER",
        "TRANSFER",
        "FUNDING",
        "GUTHABEN",
        "PAYPAL",
        "KLARNA",
        "KLARNA BANK AB",
    }:
        return None
    if re.fullmatch(r"[A-Z0-9_-]{8,}", candidate, flags=re.IGNORECASE):
        return None
    return candidate[:160]


def extract_sparda_merchant(
    *, counterparty: str, booking_text: str, purpose: str, note: str = ""
) -> MerchantExtraction:
    raw_counterparty = _display_text(counterparty)
    processor = _payment_processor(raw_counterparty)
    detail = _useful_secondary_detail(
        counterparty=counterparty,
        booking_text=booking_text,
        purpose=purpose,
        note=note,
    )
    booking = _searchable(booking_text)
    structured_detail = (
        _structured_card_merchant(purpose)
        if any(token in booking for token in ("KARTENZAHLUNG", "DEBIT MC", "MAESTRO"))
        else None
    )
    if processor:
        intermediary_merchant = _intermediary_detail_merchant(
            processor=processor,
            detail=detail,
        )
        if intermediary_merchant:
            return MerchantExtraction(
                raw_counterparty=raw_counterparty,
                processor=processor,
                canonical_merchant=intermediary_merchant,
                secondary_detail=detail,
                source="payment_detail",
                confidence=Decimal("0.98"),
                reason="Expliziter Händler im Klarna-/PayPal-Zahlungsdetail",
                unambiguous=True,
            )
        if processor:
            return MerchantExtraction(
                raw_counterparty=raw_counterparty,
                processor=processor,
                canonical_merchant=raw_counterparty or "Unbekannter Zahlungspartner",
                secondary_detail=detail,
                source="generic_counterparty",
                confidence=Decimal("0.30"),
                reason="Provider-Detail enthält keinen sicheren Händlernamen",
                unambiguous=False,
            )
    if structured_detail:
        return MerchantExtraction(
            raw_counterparty=raw_counterparty,
            processor=processor,
            canonical_merchant=structured_detail,
            secondary_detail=detail,
            source="payment_detail",
            confidence=Decimal("0.99"),
            reason="Strukturierter Kartenumsatz: Händler vor dem ersten Detailtrenner",
            unambiguous=True,
        )
    if raw_counterparty and not _is_generic_counterparty(raw_counterparty):
        return MerchantExtraction(
            raw_counterparty=raw_counterparty,
            processor=processor,
            canonical_merchant=raw_counterparty[:160],
            secondary_detail=detail,
            source="counterparty",
            confidence=Decimal("0.98"),
            reason="Spezifischer Zahlungspartner",
            unambiguous=True,
        )
    if detail:
        candidate = _structured_card_merchant(detail) or detail[:160]
        return MerchantExtraction(
            raw_counterparty=raw_counterparty,
            processor=processor,
            canonical_merchant=candidate,
            secondary_detail=detail,
            source="purpose",
            confidence=Decimal("0.70"),
            reason="Generischer Zahlungspartner; Verwendungszweck als vorsichtiger Fallback",
            unambiguous=False,
        )
    fallback = raw_counterparty or _display_text(booking_text) or "Unbekannter Zahlungspartner"
    return MerchantExtraction(
        raw_counterparty=raw_counterparty,
        processor=processor,
        canonical_merchant=fallback[:160],
        secondary_detail=detail,
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
        (
            r"\bH\+M\b",
            r"\bH & M\b",
            r"\bZALANDO\b",
            r"\bC&A\b",
            r"\bPRIMARK\b",
            r"\bUNIQLO\b",
            r"\bABOUT YOU\b",
            r"\bZARA\b",
        ),
        "Kleidung",
        "Kleidung",
        Decimal("0.98"),
        "Spezifisches Bekleidungsmuster",
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
    fields: list[tuple[str, str, str, Decimal]] = []
    canonical_label = (
        "Buchungsdetail" if merchant.source in {"payment_detail", "purpose"} else "Händler"
    )
    fields.append(
        (
            canonical_label,
            merchant.canonical_merchant,
            _searchable(merchant.canonical_merchant),
            merchant.confidence,
        )
    )
    if merchant.secondary_detail:
        fields.append(
            (
                "Buchungsdetail",
                merchant.secondary_detail,
                _searchable(merchant.secondary_detail),
                Decimal("0.98"),
            )
        )
    fields.append(
        (
            "Gegenpartei",
            merchant.raw_counterparty,
            _searchable(merchant.raw_counterparty),
            (
                Decimal("0.95")
                if not _is_generic_counterparty(merchant.raw_counterparty)
                else Decimal("0.50")
            ),
        )
    )
    seen: set[str] = set()
    for field_name, display_value, searchable, field_confidence in fields:
        if not searchable or searchable in seen:
            continue
        seen.add(searchable)
        for rule in CATEGORY_RULES:
            if any(re.search(pattern, searchable) for pattern in rule.patterns):
                confidence = min(rule.confidence, field_confidence)
                snippet = _display_text(display_value)[:80]
                reason = f'{rule.reason} – erkannt aus "{snippet}" im {field_name}'
                return CategorySuggestion(
                    rule.parent,
                    rule.child,
                    confidence,
                    reason,
                    matched_field=field_name,
                    matched_text=snippet,
                )
    return None


def _matching_evidence(
    merchant: MerchantExtraction, patterns: tuple[str, ...]
) -> tuple[str, str] | None:
    fields = (
        (
            "Buchungsdetail" if merchant.source in {"payment_detail", "purpose"} else "Händler",
            merchant.canonical_merchant,
        ),
        ("Buchungsdetail", merchant.secondary_detail),
        ("Gegenpartei", merchant.raw_counterparty),
    )
    seen: set[str] = set()
    for field_name, value in fields:
        searchable = _searchable(value)
        if not searchable or searchable in seen:
            continue
        seen.add(searchable)
        if any(pattern in searchable for pattern in patterns):
            return field_name, _display_text(value)[:80]
    return None


def _explained_category(
    merchant: MerchantExtraction,
    *,
    parent: str,
    child: str,
    confidence: Decimal,
    label: str,
    patterns: tuple[str, ...],
) -> CategorySuggestion:
    evidence = _matching_evidence(merchant, patterns)
    if evidence is None:
        return CategorySuggestion(parent, child, confidence, label)
    field_name, snippet = evidence
    return CategorySuggestion(
        parent,
        child,
        confidence,
        f'{label} – erkannt aus "{snippet}" im {field_name}',
        matched_field=field_name,
        matched_text=snippet,
    )


def _income_category(merchant: MerchantExtraction) -> CategorySuggestion | None:
    if _matching_evidence(merchant, ("FAMILIENKASSE", "KINDERGELD")):
        return _explained_category(
            merchant,
            parent="Einnahmen",
            child="Kindergeld",
            confidence=Decimal("0.99"),
            label="Kindergeld-Muster",
            patterns=("FAMILIENKASSE", "KINDERGELD"),
        )
    income_patterns = (
        "GEHALT",
        "LOHN",
        "BUNDESAGENTUR FUR ARBEIT",
        "ARBEITSLOSENGELD",
    )
    if _matching_evidence(merchant, income_patterns):
        return _explained_category(
            merchant,
            parent="Einnahmen",
            child="Gehalt / ALG",
            confidence=Decimal("0.99"),
            label="Gehalt-/ALG-Muster",
            patterns=income_patterns,
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
    if "PAYPAL EUROPE" in text or (
        _payment_processor(counterparty)
        and _searchable(_payment_processor(counterparty) or "").startswith("PAYPAL")
    ):
        return SpardaDecision(
            classify_account_movement(AccountMovementKind.OWN_ACCOUNT_TRANSFER),
            Decimal("1"),
            "paypal_funding_leg",
            link_type="funding_leg",
            category=category if merchant.unambiguous else None,
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
                category=_explained_category(
                    merchant,
                    parent="Einnahmen",
                    child="Erstattung / Rückzahlung",
                    confidence=Decimal("0.99"),
                    label="Eindeutige Finanzamt-Erstattung",
                    patterns=("FINANZAMT",),
                ),
                merchant=merchant,
            )
        income_category = _income_category(merchant)
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
                category=_explained_category(
                    merchant,
                    parent="Einnahmen",
                    child="Erstattung / Rückzahlung",
                    confidence=Decimal("0.95"),
                    label="Eindeutiges Erstattungs-Muster",
                    patterns=(
                        "ERSTATTUNG",
                        "RUCKERSTATTUNG",
                        "RUCKZAHLUNG",
                        "RETOURE",
                        "REFUND",
                        "STORNO",
                    ),
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

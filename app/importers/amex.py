from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    Account,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    RawImportRecord,
    ReviewItem,
    SourceTransaction,
    SourceTransactionAccount,
)
from app.importers.sparda import PrivateProfileRequiredError
from app.security.redaction import redact_card_numbers, redact_text
from app.services.import_execution import run_atomic_import

REQUIRED_FIELDS = frozenset({"booking_date", "description", "amount"})
HEADER_ALIASES = {
    "datum": "booking_date",
    "buchungsdatum": "booking_date",
    "buchungstag": "booking_date",
    "abrechnungsdatum": "booking_date",
    "date": "booking_date",
    "transactiondate": "transaction_date",
    "transaktionsdatum": "transaction_date",
    "leistungsdatum": "transaction_date",
    "beschreibung": "description",
    "description": "description",
    "handler": "description",
    "merchant": "description",
    "betrag": "amount",
    "amount": "amount",
    "wahrung": "currency",
    "currency": "currency",
    "abrechnungswahrung": "currency",
    "fremdwahrungsbetrag": "original_amount",
    "originalbetrag": "original_amount",
    "originalamount": "original_amount",
    "foreignamount": "original_amount",
    "ursprungsbetrag": "original_amount",
    "fremdwahrung": "original_currency",
    "originalwahrung": "original_currency",
    "originalcurrency": "original_currency",
    "foreigncurrency": "original_currency",
    "wechselkurs": "exchange_rate",
    "umrechnungskurs": "exchange_rate",
    "exchangerate": "exchange_rate",
    "land": "country",
    "country": "country",
    "ort": "location",
    "location": "location",
    "stadtbundesland": "location",
    "citystate": "location",
    "referenz": "reference_id",
    "reference": "reference_id",
    "transaktionsreferenz": "reference_id",
    "transactionreference": "reference_id",
    "transaktionsid": "reference_id",
    "abrechnungsreferenz": "statement_reference",
    "statementreference": "statement_reference",
    "zahlungsreferenz": "statement_reference",
    "relatedreference": "related_reference",
    "zugehorigereferenz": "related_reference",
    "kartenreferenz": "card_reference",
    "cardreference": "card_reference",
    "kartennummer": "card_reference",
    "kartennr": "card_reference",
    "kontonummer": "card_reference",
    "konto": "card_reference",
    "cardnumber": "card_reference",
    "accountnumber": "card_reference",
    "erweitertedetails": "secondary_detail",
    "extendeddetails": "secondary_detail",
    "details": "secondary_detail",
    "erscheintaufihrerabrechnungals": "statement_description",
    "appearsonyourstatementas": "statement_description",
    "kategorie": "source_category",
    "category": "source_category",
    "typ": "source_type",
    "type": "source_type",
}

SETTLEMENT_MARKERS = (
    "zahlung erhalten",
    "ihre zahlung",
    "payment received",
    "payment - thank you",
    "payment thank you",
    "abrechnungszahlung",
    "american express zahlung",
)
REFUND_MARKERS = ("refund", "rückerstattung", "ruckerstattung", "gutschrift", "storno")
FEE_MARKERS = (
    "jahresgebühr",
    "jahresgebuhr",
    "kartenentgelt",
    "fremdwährungsgebühr",
    "fremdwahrungsgebuhr",
    "late payment fee",
    "interest charge",
    "sollzinsen",
    "zinsbelastung",
)
AMBIGUOUS_MARKERS = ("adjustment", "anpassung", "korrektur", "cash advance", "barabhebung")


class AmexFormatError(ValueError):
    def __init__(self, code: str, message: str, row_number: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.row_number = row_number


@dataclass(frozen=True)
class AmexRow:
    row_number: int
    raw_values: dict[str, str]
    raw_text: str
    booked_at: datetime
    transaction_at: datetime | None
    description: str
    secondary_detail: str
    amount: Decimal
    currency: str
    original_amount: Decimal | None
    original_currency: str | None
    exchange_rate: Decimal | None
    country: str
    location: str
    statement_reference: str
    reference_id: str
    related_reference: str
    card_reference: str
    source_type: str
    source_category: str
    fingerprint: str
    raw_hash: str


@dataclass(frozen=True)
class AmexDecision:
    semantic: str
    event_type: str | None
    needs_review: bool
    confidence: Decimal
    explanation: str


@dataclass(frozen=True)
class SettlementMatch:
    amex_row_number: int
    sparda_source_id: int | None
    sparda_event_id: int | None
    confidence: str
    score: Decimal
    reason: str


@dataclass
class AmexImportSummary:
    source_rows_parsed: int = 0
    merchant_purchases: int = 0
    refunds: int = 0
    settlement_rows: int = 0
    fees_interest_other: int = 0
    unresolved_rows: int = 0
    high_confidence_sparda_matches: int = 0
    medium_confidence_sparda_matches: int = 0
    unmatched_settlement_rows: int = 0
    review_items: int = 0
    new_raw_records: int = 0
    source_transactions_created: int = 0
    economic_events_created: int = 0
    duplicate_rows: int = 0
    duplicate_files: int = 0
    failed_rows: int = 0
    repeated_reference_ids: int = 0
    expenses: int = 0
    income: int = 0
    transfers: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class AmexPreviewRow:
    row_number: int
    booked_at: str
    merchant: str
    amount: str
    semantic: str
    match_confidence: str | None
    needs_review: bool


@dataclass(frozen=True)
class AmexDryRunReport:
    source_rows: int
    date_from: str
    date_to: str
    duplicate_file: bool
    existing_batch_status: str | None
    duplicate_rows: int
    merchant_purchases: int
    refunds: int
    settlement_rows: int
    fees_interest_other: int
    unresolved_rows: int
    high_confidence_sparda_matches: int
    medium_confidence_sparda_matches: int
    unmatched_settlement_rows: int
    review_items: int
    repeated_reference_ids: int

    def as_dict(self) -> dict[str, str | int | bool | None]:
        return asdict(self)


def _header_token(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lstrip("\ufeff").casefold())
    ascii_value = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]", "", ascii_value)


def _canonical_header(value: str) -> str:
    token = _header_token(value)
    return HEADER_ALIASES.get(token, token)


def parse_amex_decimal(value: str, *, optional: bool = False) -> Decimal | None:
    cleaned = value.strip().replace("\u00a0", "").replace(" ", "")
    cleaned = cleaned.replace("€", "").replace("$", "").replace("£", "")
    if not cleaned:
        if optional:
            return None
        raise AmexFormatError("missing_amount", "Ein erforderlicher Amex-Betrag fehlt.")
    negative_parentheses = cleaned.startswith("(") and cleaned.endswith(")")
    trailing_minus = cleaned.endswith("-")
    cleaned = cleaned.strip("()")
    cleaned = cleaned.rstrip("-")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        result = Decimal(cleaned)
    except InvalidOperation as exc:
        raise AmexFormatError("invalid_decimal", "Ein Amex-Betrag ist ungültig.") from exc
    return -result if negative_parentheses or trailing_minus else result


def parse_amex_date(value: str, *, optional: bool = False) -> datetime | None:
    cleaned = value.strip()
    if not cleaned and optional:
        return None
    for pattern in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
    raise AmexFormatError("invalid_date", "Ein Amex-Datum ist ungültig.")


def _stable_fingerprint(values: dict[str, str], occurrence: int) -> str:
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    base = hashlib.sha256(f"amex|{payload}".encode()).hexdigest()
    return hashlib.sha256(f"{base}|occurrence:{occurrence}".encode()).hexdigest()


def parse_amex_csv(path: Path) -> list[AmexRow]:
    if path.suffix.casefold() != ".csv":
        raise AmexFormatError("unsupported_format", "Produktive Amex-Importe benötigen CSV.")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise AmexFormatError("invalid_encoding", "Amex CSV muss UTF-8 sein.") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:16384], delimiters=";,\t")
    except csv.Error as exc:
        raise AmexFormatError("unsupported_delimiter", "CSV-Trennzeichen nicht erkannt.") from exc
    physical_lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
    try:
        original_header = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise AmexFormatError("missing_header", "Die Amex-Kopfzeile fehlt.") from exc
    canonical_header = [_canonical_header(column) for column in original_header]
    if len(canonical_header) != len(set(canonical_header)):
        raise AmexFormatError("duplicate_columns", "Die Amex CSV enthält doppelte Spalten.")
    missing = sorted(REQUIRED_FIELDS - set(canonical_header))
    if missing:
        raise AmexFormatError(
            "missing_required_columns",
            f"Erforderliche Amex-Spalten fehlen: {', '.join(missing)}",
        )

    rows: list[AmexRow] = []
    occurrences: Counter[str] = Counter()
    previous_line = reader.line_num
    try:
        for csv_values in reader:
            end_line = reader.line_num
            raw_text = "".join(physical_lines[previous_line:end_line]).rstrip("\r\n")
            previous_line = end_line
            if not csv_values or all(not value.strip() for value in csv_values):
                continue
            row_number = len(rows) + 2
            if len(csv_values) > len(original_header):
                raise AmexFormatError(
                    "unexpected_columns", "Eine Amex-Zeile enthält zu viele Spalten.", row_number
                )
            csv_values.extend([""] * (len(original_header) - len(csv_values)))
            raw_values = dict(zip(original_header, csv_values, strict=True))
            values = dict(zip(canonical_header, csv_values, strict=True))
            try:
                booked_at = parse_amex_date(values["booking_date"])
                transaction_at = parse_amex_date(values.get("transaction_date", ""), optional=True)
                amount = parse_amex_decimal(values["amount"])
                original_amount = parse_amex_decimal(
                    values.get("original_amount", ""), optional=True
                )
                exchange_rate = parse_amex_decimal(values.get("exchange_rate", ""), optional=True)
            except AmexFormatError as exc:
                raise AmexFormatError(exc.code, exc.message, row_number) from exc
            description = values["description"].strip()
            if not description:
                raise AmexFormatError("missing_description", "Amex-Beschreibung fehlt.", row_number)
            currency = values.get("currency", "EUR").strip().upper() or "EUR"
            if len(currency) != 3:
                raise AmexFormatError("invalid_currency", "Amex-Währung ist ungültig.", row_number)
            stable_values = {key: value.strip() for key, value in values.items()}
            if "card_reference" in stable_values:
                stable_values["card_reference"] = redact_card_numbers(
                    stable_values["card_reference"]
                )
            stable_payload = json.dumps(
                stable_values, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            occurrence_key = hashlib.sha256(stable_payload.encode()).hexdigest()
            occurrences[occurrence_key] += 1
            occurrence = occurrences[occurrence_key]
            fingerprint = _stable_fingerprint(stable_values, occurrence)
            raw_hash = hashlib.sha256(
                f"amex-raw|{raw_text}|occurrence:{occurrence}".encode()
            ).hexdigest()
            rows.append(
                AmexRow(
                    row_number=row_number,
                    raw_values=raw_values,
                    raw_text=raw_text,
                    booked_at=booked_at,
                    transaction_at=transaction_at,
                    description=description,
                    secondary_detail=values.get("secondary_detail", "").strip(),
                    amount=amount,
                    currency=currency,
                    original_amount=original_amount,
                    original_currency=(values.get("original_currency", "").strip().upper() or None),
                    exchange_rate=exchange_rate,
                    country=values.get("country", "").strip(),
                    location=values.get("location", "").strip(),
                    statement_reference=values.get("statement_reference", "").strip(),
                    reference_id=values.get("reference_id", "").strip(),
                    related_reference=values.get("related_reference", "").strip(),
                    card_reference=values.get("card_reference", "").strip(),
                    source_type=values.get("source_type", "").strip(),
                    source_category=values.get("source_category", "").strip(),
                    fingerprint=fingerprint,
                    raw_hash=raw_hash,
                )
            )
    except csv.Error as exc:
        raise AmexFormatError("invalid_csv", "Die Amex CSV-Struktur ist ungültig.") from exc
    if not rows:
        raise AmexFormatError("no_data_rows", "Die Amex CSV enthält keine Datenzeilen.")
    return rows


def classify_amex_row(row: AmexRow) -> AmexDecision:
    text = " ".join(
        (row.description, row.secondary_detail, row.source_type, row.source_category)
    ).casefold()
    if any(marker in text for marker in SETTLEMENT_MARKERS):
        return AmexDecision(
            "statement_payment",
            None,
            False,
            Decimal("0.99"),
            "Explizite Amex-Abrechnungszahlung; keine Ausgabe.",
        )
    if any(marker in text for marker in FEE_MARKERS):
        return AmexDecision(
            "fee_interest",
            "expense",
            False,
            Decimal("0.98"),
            "Explizite Karten-Gebühr oder Zinsbelastung.",
        )
    if any(marker in text for marker in AMBIGUOUS_MARKERS) or row.amount == 0:
        return AmexDecision(
            "unresolved",
            None,
            True,
            Decimal("0.35"),
            "Amex-Zeile besitzt keine eindeutig sichere wirtschaftliche Bedeutung.",
        )
    if any(marker in text for marker in REFUND_MARKERS) or row.amount < 0:
        return AmexDecision(
            "refund",
            "refund",
            False,
            Decimal("0.95"),
            "Eindeutige Händlergutschrift auf dem Amex-Konto.",
        )
    if row.amount > 0:
        return AmexDecision(
            "merchant_purchase",
            "expense",
            False,
            Decimal("0.95"),
            "Normale Amex-Händlerbelastung.",
        )
    return AmexDecision(
        "unresolved",
        None,
        True,
        Decimal("0.25"),
        "Amex-Zeile bleibt zur manuellen Prüfung offen.",
    )


def _sparda_settlement_sources(db: Session) -> list[SourceTransaction]:
    sources = db.scalars(
        select(SourceTransaction).where(SourceTransaction.source_system == "sparda")
    )
    return [
        source
        for source in sources
        if (source.metadata_json or {}).get("sparda_semantic") == "credit_card_settlement"
        or "american express" in f"{source.merchant_raw or ''} {source.description_raw}".casefold()
        or " amex " in f" {source.merchant_raw or ''} {source.description_raw} ".casefold()
    ]


def _transfer_event_for_source(db: Session, source_id: int) -> EconomicEvent | None:
    link = db.scalar(
        select(EventSourceLink).where(
            EventSourceLink.source_transaction_id == source_id,
            EventSourceLink.link_type == "canonical_source",
        )
    )
    event = db.get(EconomicEvent, link.economic_event_id) if link else None
    return event if event is not None and event.event_type == "transfer" else None


def match_sparda_settlements(db: Session, rows: list[AmexRow]) -> dict[int, SettlementMatch]:
    sparda = _sparda_settlement_sources(db)
    results: dict[int, SettlementMatch] = {}
    used_high: set[int] = set()
    for row in rows:
        if classify_amex_row(row).semantic != "statement_payment":
            continue
        candidates: list[tuple[SourceTransaction, int, bool, EconomicEvent | None]] = []
        references = {
            value.casefold()
            for value in (row.reference_id, row.statement_reference)
            if len(value.strip()) >= 5
        }
        for source in sparda:
            if abs(abs(source.amount) - abs(row.amount)) > Decimal("0.01"):
                continue
            days = abs((source.booked_at.date() - row.booked_at.date()).days)
            if days > 7:
                continue
            haystack = f"{source.merchant_raw or ''} {source.description_raw}".casefold()
            explicit = any(reference in haystack for reference in references)
            candidates.append((source, days, explicit, _transfer_event_for_source(db, source.id)))
        explicit = [candidate for candidate in candidates if candidate[2] and candidate[3]]
        close = [candidate for candidate in candidates if candidate[1] <= 2 and candidate[3]]
        if len(explicit) == 1 and explicit[0][0].id not in used_high:
            source, _days, _explicit, event = explicit[0]
            used_high.add(source.id)
            results[row.row_number] = SettlementMatch(
                row.row_number,
                source.id,
                event.id,
                "high",
                Decimal("0.99"),
                "Explizite Referenz, identischer Betrag und vorhandener Sparda-Transfer.",
            )
        elif len(close) == 1 and close[0][0].id not in used_high:
            source, _days, _explicit, event = close[0]
            used_high.add(source.id)
            results[row.row_number] = SettlementMatch(
                row.row_number,
                source.id,
                event.id,
                "high",
                Decimal("0.95"),
                "Eindeutiger Sparda-Amex-Transfer mit identischem Betrag und engem Datum.",
            )
        elif len(candidates) == 1:
            source, _days, _explicit, event = candidates[0]
            results[row.row_number] = SettlementMatch(
                row.row_number,
                source.id,
                event.id if event else None,
                "medium",
                Decimal("0.72"),
                "Ein Betragskandidat im erweiterten Datumsfenster; Bestätigung erforderlich.",
            )
        else:
            results[row.row_number] = SettlementMatch(
                row.row_number,
                None,
                None,
                "unresolved",
                Decimal("0"),
                "Kein eindeutiger Sparda-Amex-Abrechnungstransfer gefunden.",
            )
    return results


def _repeated_reference_count(rows: list[AmexRow]) -> int:
    counts = Counter(row.reference_id for row in rows if row.reference_id)
    return sum(1 for count in counts.values() if count > 1)


def _refund_origin(rows: list[AmexRow], refund: AmexRow) -> AmexRow | None:
    purchases = [
        row
        for row in rows
        if classify_amex_row(row).semantic == "merchant_purchase"
        and row.booked_at <= refund.booked_at
    ]
    if refund.related_reference:
        referenced = [row for row in purchases if row.reference_id == refund.related_reference]
        if len(referenced) == 1:
            return referenced[0]
    normalized = " ".join(refund.description.casefold().split())
    candidates = [
        row
        for row in purchases
        if " ".join(row.description.casefold().split()) == normalized
        and abs(abs(row.amount) - abs(refund.amount)) <= Decimal("0.01")
        and 0 <= (refund.booked_at.date() - row.booked_at.date()).days <= 120
    ]
    return candidates[0] if len(candidates) == 1 else None


def _summary(rows: list[AmexRow], matches: dict[int, SettlementMatch]) -> AmexImportSummary:
    summary = AmexImportSummary(
        source_rows_parsed=len(rows), repeated_reference_ids=_repeated_reference_count(rows)
    )
    for row in rows:
        decision = classify_amex_row(row)
        if decision.semantic == "merchant_purchase":
            summary.merchant_purchases += 1
        elif decision.semantic == "refund":
            summary.refunds += 1
        elif decision.semantic == "statement_payment":
            summary.settlement_rows += 1
        elif decision.semantic == "fee_interest":
            summary.fees_interest_other += 1
        else:
            summary.unresolved_rows += 1
        match = matches.get(row.row_number)
        if match and match.confidence == "high":
            summary.high_confidence_sparda_matches += 1
        elif match and match.confidence == "medium":
            summary.medium_confidence_sparda_matches += 1
        elif decision.semantic == "statement_payment":
            summary.unmatched_settlement_rows += 1
        if decision.needs_review or (
            decision.semantic == "statement_payment"
            and (match is None or match.confidence != "high")
        ):
            summary.review_items += 1
        elif decision.semantic == "refund" and _refund_origin(rows, row) is None:
            summary.review_items += 1
    summary.expenses = summary.merchant_purchases + summary.fees_interest_other
    return summary


def preview_amex_file(
    path: Path, db: Session
) -> tuple[list[AmexPreviewRow], AmexImportSummary, dict[int, SettlementMatch]]:
    rows = parse_amex_csv(path)
    matches = match_sparda_settlements(db, rows)
    summary = _summary(rows, matches)
    preview = [
        AmexPreviewRow(
            row_number=row.row_number,
            booked_at=row.booked_at.strftime("%d.%m.%Y"),
            merchant=redact_text(row.description)[:120],
            amount=f"{row.amount:.2f} {row.currency}",
            semantic=classify_amex_row(row).semantic,
            match_confidence=(
                matches[row.row_number].confidence if row.row_number in matches else None
            ),
            needs_review=(
                classify_amex_row(row).needs_review
                or (row.row_number in matches and matches[row.row_number].confidence != "high")
                or (
                    classify_amex_row(row).semantic == "refund"
                    and _refund_origin(rows, row) is None
                )
            ),
        )
        for row in rows
    ]
    return preview, summary, matches


def dry_run_amex_file(path: Path, db: Session) -> AmexDryRunReport:
    """Analyze an Amex CSV without staging it or mutating database state."""
    rows = parse_amex_csv(path)
    matches = match_sparda_settlements(db, rows)
    summary = _summary(rows, matches)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    existing_batch = db.scalar(select(ImportBatch).where(ImportBatch.source_hash == source_hash))
    duplicate_file = existing_batch is not None and existing_batch.status == "imported"
    fingerprints = {row.fingerprint for row in rows}
    duplicate_rows = len(
        set(
            db.scalars(
                select(SourceTransaction.fingerprint).where(
                    SourceTransaction.fingerprint.in_(fingerprints)
                )
            )
        )
    )
    dates = [row.booked_at.date() for row in rows]
    return AmexDryRunReport(
        source_rows=len(rows),
        date_from=min(dates).isoformat(),
        date_to=max(dates).isoformat(),
        duplicate_file=duplicate_file,
        existing_batch_status=existing_batch.status if existing_batch else None,
        duplicate_rows=duplicate_rows,
        merchant_purchases=summary.merchant_purchases,
        refunds=summary.refunds,
        settlement_rows=summary.settlement_rows,
        fees_interest_other=summary.fees_interest_other,
        unresolved_rows=summary.unresolved_rows,
        high_confidence_sparda_matches=summary.high_confidence_sparda_matches,
        medium_confidence_sparda_matches=summary.medium_confidence_sparda_matches,
        unmatched_settlement_rows=summary.unmatched_settlement_rows,
        review_items=summary.review_items,
        repeated_reference_ids=summary.repeated_reference_ids,
    )


def _amex_account(db: Session) -> Account | None:
    return db.scalar(select(Account).where(Account.name == "American Express"))


def _protected_raw_values(row: AmexRow) -> dict[str, str]:
    return {key: redact_card_numbers(value) for key, value in row.raw_values.items()}


def _import_rows(db: Session, batch: ImportBatch, path: Path) -> int:
    rows = parse_amex_csv(path)
    matches = match_sparda_settlements(db, rows)
    summary = _summary(rows, matches)
    summary.review_items = 0
    account = _amex_account(db)
    created_sources: dict[int, SourceTransaction] = {}
    created_events: dict[int, EconomicEvent] = {}
    seen: set[str] = set()

    for row in rows:
        if row.fingerprint in seen or db.scalar(
            select(SourceTransaction.id).where(SourceTransaction.fingerprint == row.fingerprint)
        ):
            summary.duplicate_rows += 1
            continue
        seen.add(row.fingerprint)
        decision = classify_amex_row(row)
        raw = RawImportRecord(
            import_batch_id=batch.id,
            source_row_key=f"row-{row.row_number}",
            raw_payload_json=_protected_raw_values(row),
            raw_text=redact_card_numbers(row.raw_text),
            raw_hash=row.raw_hash,
        )
        db.add(raw)
        db.flush()
        description_parts = [row.secondary_detail, row.location, row.country]
        source = SourceTransaction(
            import_batch_id=batch.id,
            raw_record_id=raw.id,
            source_system="amex",
            source_transaction_id=f"amex:{row.fingerprint}",
            related_source_transaction_id=(
                f"amex-ref:{hashlib.sha256(row.related_reference.encode()).hexdigest()}"
                if row.related_reference
                else None
            ),
            booked_at=row.booked_at,
            value_at=row.transaction_at,
            merchant_raw=redact_card_numbers(row.description)[:255],
            description_raw=redact_card_numbers(
                " | ".join(value for value in description_parts if value)
            ),
            amount=row.amount,
            currency=row.currency,
            source_account_hint="American Express",
            status="booked",
            metadata_json={
                "amex_semantic": decision.semantic,
                "canonical_merchant": redact_card_numbers(row.description)[:160],
                "original_amount": (
                    format(row.original_amount, "f") if row.original_amount is not None else None
                ),
                "original_currency": row.original_currency,
                "exchange_rate": (
                    format(row.exchange_rate, "f") if row.exchange_rate is not None else None
                ),
                "country": row.country,
                "location": row.location,
                "statement_reference": redact_card_numbers(row.statement_reference),
                "reference_id": redact_card_numbers(row.reference_id),
                "card_reference": redact_card_numbers(row.card_reference),
            },
            fingerprint=row.fingerprint,
        )
        db.add(source)
        db.flush()
        if account is not None:
            db.add(
                SourceTransactionAccount(
                    source_transaction_id=source.id,
                    account_id=account.id,
                    role="source",
                )
            )
        created_sources[row.row_number] = source
        summary.new_raw_records += 1
        summary.source_transactions_created += 1

        if decision.event_type:
            event = EconomicEvent(
                event_type=decision.event_type,
                occurred_at=row.transaction_at or row.booked_at,
                description=redact_text(row.description)[:255],
                amount=abs(row.amount),
                currency=row.currency,
                account_id=account.id if account else None,
                status="booked",
                confidence=decision.confidence,
            )
            db.add(event)
            db.flush()
            db.add(
                EventSourceLink(
                    economic_event_id=event.id,
                    source_transaction_id=source.id,
                    link_type="canonical_source",
                    confidence=decision.confidence,
                )
            )
            created_events[row.row_number] = event
            summary.economic_events_created += 1
        elif decision.needs_review:
            db.add(
                ReviewItem(
                    source_transaction_id=source.id,
                    review_type="amex_economic_type",
                    confidence=decision.confidence,
                    explanation=decision.explanation,
                    status="open",
                )
            )
            summary.review_items += 1

    for row in rows:
        source = created_sources.get(row.row_number)
        if source is None:
            continue
        decision = classify_amex_row(row)
        if decision.semantic == "statement_payment":
            match = matches.get(row.row_number)
            if match and match.confidence == "high" and match.sparda_event_id is not None:
                existing = db.scalar(
                    select(EventSourceLink.id).where(
                        EventSourceLink.economic_event_id == match.sparda_event_id,
                        EventSourceLink.source_transaction_id == source.id,
                        EventSourceLink.link_type == "settlement_leg",
                    )
                )
                if existing is None:
                    db.add(
                        EventSourceLink(
                            economic_event_id=match.sparda_event_id,
                            source_transaction_id=source.id,
                            link_type="settlement_leg",
                            confidence=match.score,
                            notes="Konservativ bestätigtes Amex-/Sparda-Settlement-Match",
                        )
                    )
            else:
                db.add(
                    ReviewItem(
                        source_transaction_id=source.id,
                        review_type="amex_sparda_settlement",
                        proposed_event_type="transfer",
                        confidence=match.score if match else Decimal("0"),
                        explanation=(
                            match.reason if match else "Kein Sparda-Abrechnungstransfer gefunden."
                        ),
                        status="open",
                    )
                )
                summary.review_items += 1

    for row in rows:
        if classify_amex_row(row).semantic != "refund":
            continue
        refund_event = created_events.get(row.row_number)
        refund_source = created_sources.get(row.row_number)
        origin = _refund_origin(rows, row)
        original_source = created_sources.get(origin.row_number) if origin else None
        if refund_event is not None and original_source is not None:
            db.add(
                EventSourceLink(
                    economic_event_id=refund_event.id,
                    source_transaction_id=original_source.id,
                    link_type="refund_origin",
                    confidence=Decimal("0.99"),
                )
            )
        elif refund_source is not None:
            db.add(
                ReviewItem(
                    economic_event_id=refund_event.id if refund_event else None,
                    source_transaction_id=refund_source.id,
                    review_type="amex_refund_origin",
                    proposed_event_type="refund",
                    confidence=Decimal("0.50"),
                    explanation="Die ursprüngliche Amex-Händlerbelastung ist nicht eindeutig.",
                    status="open",
                )
            )
            summary.review_items += 1

    batch.metadata_json = {**(batch.metadata_json or {}), "import_summary": summary.as_dict()}
    return len(rows)


def import_amex_batch(
    session_factory: sessionmaker[Session], batch_id: int, settings: Settings
) -> None:
    if settings.demo_mode:
        raise PrivateProfileRequiredError("Produktive Amex-Importe sind im Demo-Profil gesperrt.")
    run_atomic_import(session_factory, batch_id, _import_rows, settings)

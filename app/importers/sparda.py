from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    Account,
    Category,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    RawImportRecord,
    ReviewItem,
    SourceTransaction,
)
from app.domain.sparda import CategorySuggestion, SpardaDecision, classify_sparda_transaction
from app.security.redaction import redact_text
from app.services.import_execution import run_atomic_import

KNOWN_COLUMNS = (
    "Bezeichnung Auftragskonto",
    "IBAN Auftragskonto",
    "BIC Auftragskonto",
    "Bankname Auftragskonto",
    "Buchungstag",
    "Valutadatum",
    "Name Zahlungsbeteiligter",
    "IBAN Zahlungsbeteiligter",
    "BIC (SWIFT-Code) Zahlungsbeteiligter",
    "Buchungstext",
    "Verwendungszweck",
    "Betrag",
    "Waehrung",
    "Saldo nach Buchung",
    "Bemerkung",
    "Gekennzeichneter Umsatz",
    "Glaeubiger ID",
    "Mandatsreferenz",
)
REQUIRED_COLUMNS = frozenset(
    {
        "Buchungstag",
        "Valutadatum",
        "Name Zahlungsbeteiligter",
        "Buchungstext",
        "Verwendungszweck",
        "Betrag",
        "Waehrung",
    }
)


class SpardaFormatError(ValueError):
    def __init__(self, code: str, message: str, row_number: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.row_number = row_number


class PrivateProfileRequiredError(RuntimeError):
    """Raised when a productive import is attempted in the demo profile."""


@dataclass(frozen=True)
class SpardaRow:
    row_number: int
    raw_values: dict[str, str]
    raw_text: str
    booked_at: datetime
    value_at: datetime | None
    amount: Decimal
    balance_after: Decimal | None
    currency: str
    counterparty: str
    booking_text: str
    purpose: str
    note: str
    fingerprint: str
    raw_hash: str


@dataclass
class ImportSummary:
    source_rows_parsed: int = 0
    new_raw_records: int = 0
    source_transactions_created: int = 0
    economic_events_created: int = 0
    transfers: int = 0
    expenses: int = 0
    income: int = 0
    refunds: int = 0
    review_items: int = 0
    review_only_rows: int = 0
    duplicate_rows: int = 0
    duplicate_files: int = 0
    failed_rows: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class PreviewRow:
    row_number: int
    booked_at: str
    amount: str
    event_type: str
    description: str
    category: str | None
    needs_review: bool


SUMMARY_EVENT_FIELDS = {
    "transfer": "transfers",
    "expense": "expenses",
    "income": "income",
    "refund": "refunds",
}


def _increment_event(summary: ImportSummary, event_type: str) -> None:
    field = SUMMARY_EVENT_FIELDS[event_type]
    setattr(summary, field, getattr(summary, field) + 1)


def parse_german_decimal(value: str, *, optional: bool = False) -> Decimal | None:
    cleaned = value.strip().replace("\u00a0", "").replace(" ", "")
    if not cleaned:
        if optional:
            return None
        raise SpardaFormatError("missing_amount", "Ein erforderlicher Geldbetrag fehlt.")
    normalized = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except InvalidOperation as exc:
        raise SpardaFormatError("invalid_decimal", "Ein Geldbetrag ist ungültig.") from exc


def parse_german_date(value: str, *, optional: bool = False) -> datetime | None:
    cleaned = value.strip()
    if not cleaned:
        if optional:
            return None
        raise SpardaFormatError("missing_date", "Ein erforderliches Buchungsdatum fehlt.")
    for pattern in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(cleaned, pattern)
        except ValueError:
            continue
    raise SpardaFormatError("invalid_date", "Ein deutsches Datum ist ungültig.")


def _fingerprint(values: dict[str, str], amount: Decimal, booked_at: datetime) -> str:
    stable = {
        "account_iban": values.get("IBAN Auftragskonto", "").replace(" ", "").upper(),
        "booked_at": booked_at.date().isoformat(),
        "value_date": values.get("Valutadatum", "").strip(),
        "counterparty": values.get("Name Zahlungsbeteiligter", "").strip(),
        "counterparty_iban": values.get("IBAN Zahlungsbeteiligter", "").replace(" ", "").upper(),
        "booking_text": values.get("Buchungstext", "").strip(),
        "purpose": values.get("Verwendungszweck", "").strip(),
        "amount": format(amount, "f"),
        "currency": values.get("Waehrung", "").strip().upper(),
        "balance": values.get("Saldo nach Buchung", "").strip(),
        "creditor_id": values.get("Glaeubiger ID", "").strip(),
        "mandate_reference": values.get("Mandatsreferenz", "").strip(),
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_sparda_csv(path: Path) -> list[SpardaRow]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise SpardaFormatError("invalid_encoding", "Sparda CSV muss UTF-8 sein.") from exc
    physical_lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=";", strict=True)
    try:
        header = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise SpardaFormatError("missing_header", "Die CSV-Kopfzeile fehlt.") from exc
    header = [column.strip() for column in header]
    if len(header) != len(set(header)):
        raise SpardaFormatError("duplicate_columns", "Die CSV enthält doppelte Spaltennamen.")
    missing = sorted(REQUIRED_COLUMNS - set(header))
    if missing:
        raise SpardaFormatError(
            "missing_required_columns",
            f"Erforderliche Spalten fehlen: {', '.join(missing)}",
        )

    rows: list[SpardaRow] = []
    previous_line = reader.line_num
    try:
        for values in reader:
            end_line = reader.line_num
            raw_text = "".join(physical_lines[previous_line:end_line]).rstrip("\r\n")
            previous_line = end_line
            if not values or all(not value.strip() for value in values):
                continue
            row_number = len(rows) + 2
            if len(values) > len(header):
                raise SpardaFormatError(
                    "unexpected_columns", "Eine Datenzeile enthält zu viele Spalten.", row_number
                )
            values.extend([""] * (len(header) - len(values)))
            raw_values = {column: value for column, value in zip(header, values, strict=True)}
            try:
                booked_at = parse_german_date(raw_values["Buchungstag"])
                value_at = parse_german_date(raw_values["Valutadatum"], optional=True)
                amount = parse_german_decimal(raw_values["Betrag"])
                balance = parse_german_decimal(
                    raw_values.get("Saldo nach Buchung", ""), optional=True
                )
            except SpardaFormatError as exc:
                raise SpardaFormatError(exc.code, exc.message, row_number) from exc
            currency = raw_values["Waehrung"].strip().upper()
            if not currency:
                raise SpardaFormatError("missing_currency", "Die Währung fehlt.", row_number)
            fingerprint = _fingerprint(raw_values, amount, booked_at)
            rows.append(
                SpardaRow(
                    row_number=row_number,
                    raw_values=raw_values,
                    raw_text=raw_text,
                    booked_at=booked_at,
                    value_at=value_at,
                    amount=amount,
                    balance_after=balance,
                    currency=currency,
                    counterparty=raw_values["Name Zahlungsbeteiligter"].strip(),
                    booking_text=raw_values["Buchungstext"].strip(),
                    purpose=raw_values["Verwendungszweck"].strip(),
                    note=raw_values.get("Bemerkung", "").strip(),
                    fingerprint=fingerprint,
                    raw_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                )
            )
    except csv.Error as exc:
        raise SpardaFormatError("invalid_csv", "Die CSV-Struktur ist ungültig.") from exc
    if not rows:
        raise SpardaFormatError("no_data_rows", "Die CSV enthält keine Buchungszeilen.")
    return rows


def preview_sparda_file(path: Path) -> tuple[list[PreviewRow], ImportSummary]:
    rows = parse_sparda_csv(path)
    summary = ImportSummary(source_rows_parsed=len(rows))
    previews: list[PreviewRow] = []
    for row in rows:
        decision = _decision(row)
        event_type = decision.event_type or "review"
        if decision.event_type:
            _increment_event(summary, decision.event_type)
        if decision.review_type:
            summary.review_items += 1
            if decision.event_type is None:
                summary.review_only_rows += 1
        previews.append(
            PreviewRow(
                row_number=row.row_number,
                booked_at=row.booked_at.strftime("%d.%m.%Y"),
                amount=f"{row.amount:.2f} {row.currency}",
                event_type=event_type,
                description=_safe_description(row, decision),
                category=decision.category.path if decision.category else None,
                needs_review=decision.review_type is not None,
            )
        )
    return previews, summary


def _decision(row: SpardaRow) -> SpardaDecision:
    return classify_sparda_transaction(
        amount=row.amount,
        counterparty=row.counterparty,
        booking_text=row.booking_text,
        purpose=row.purpose,
        note=row.note,
    )


def _safe_description(row: SpardaRow, decision: SpardaDecision) -> str:
    fixed = {
        "american_express_settlement": "American-Express-Abrechnung",
        "paypal_funding_leg": "PayPal-Finanzierung",
        "amazon_visa_settlement": "Amazon-Visa-Abrechnung",
        "cash_withdrawal": "Bargeldabhebung",
        "tax_refund_income": "Zahlung Finanzamt",
        "high_confidence_income": "Einkommenseingang",
    }
    if decision.semantic in fixed:
        return fixed[decision.semantic]
    candidate = redact_text(row.counterparty or row.booking_text or "Sparda-Buchung")
    return candidate[:160]


def _category_id(db: Session, suggestion: CategorySuggestion | None) -> int | None:
    if suggestion is None:
        return None
    parent = db.scalar(
        select(Category).where(Category.name == suggestion.parent, Category.parent_id.is_(None))
    )
    if parent is None:
        parent = Category(name=suggestion.parent)
        db.add(parent)
        db.flush()
    if suggestion.child:
        child = db.scalar(
            select(Category).where(
                Category.name == suggestion.child, Category.parent_id == parent.id
            )
        )
        if child is None:
            child = Category(name=suggestion.child, parent_id=parent.id)
            db.add(child)
            db.flush()
        return child.id
    return parent.id


def _source_account(db: Session, account_name: str) -> Account | None:
    if account_name:
        exact = db.scalar(select(Account).where(Account.name == account_name))
        if exact is not None:
            return exact
    accounts = list(
        db.scalars(
            select(Account).where(Account.account_type == "checking", Account.is_active.is_(True))
        )
    )
    return accounts[0] if len(accounts) == 1 else None


def _import_rows(db: Session, batch: ImportBatch, path: Path) -> int:
    rows = parse_sparda_csv(path)
    summary = ImportSummary(source_rows_parsed=len(rows))
    seen: set[str] = set()
    for row in rows:
        if row.fingerprint in seen or db.scalar(
            select(SourceTransaction.id).where(SourceTransaction.fingerprint == row.fingerprint)
        ):
            summary.duplicate_rows += 1
            continue
        seen.add(row.fingerprint)
        decision = _decision(row)
        raw = RawImportRecord(
            import_batch_id=batch.id,
            source_row_key=f"row-{row.row_number}",
            raw_payload_json=row.raw_values,
            raw_text=row.raw_text,
            raw_hash=row.raw_hash,
        )
        db.add(raw)
        db.flush()
        source = SourceTransaction(
            import_batch_id=batch.id,
            raw_record_id=raw.id,
            source_system="sparda",
            source_transaction_id=f"sparda:{row.fingerprint}",
            booked_at=row.booked_at,
            value_at=row.value_at,
            merchant_raw=row.counterparty or None,
            description_raw=" | ".join(value for value in (row.booking_text, row.purpose) if value),
            amount=row.amount,
            currency=row.currency,
            source_account_hint=row.raw_values.get("Bezeichnung Auftragskonto", "") or None,
            balance_after=row.balance_after,
            status="booked",
            metadata_json={
                "sparda_semantic": decision.semantic,
                "category_suggestion": decision.category.path if decision.category else None,
                "target_account_type": decision.target_account_type,
            },
            fingerprint=row.fingerprint,
        )
        db.add(source)
        db.flush()
        summary.new_raw_records += 1
        summary.source_transactions_created += 1

        event: EconomicEvent | None = None
        if decision.event_type is not None:
            category_id = _category_id(db, decision.category)
            account = _source_account(
                db, row.raw_values.get("Bezeichnung Auftragskonto", "").strip()
            )
            event = EconomicEvent(
                event_type=decision.event_type,
                occurred_at=row.booked_at,
                description=_safe_description(row, decision),
                amount=abs(row.amount),
                currency=row.currency,
                account_id=account.id if account else None,
                category_id=category_id,
                status="booked",
                confidence=decision.confidence,
            )
            db.add(event)
            db.flush()
            db.add(
                EventSourceLink(
                    economic_event_id=event.id,
                    source_transaction_id=source.id,
                    link_type=decision.link_type or "canonical_source",
                    confidence=decision.confidence,
                )
            )
            summary.economic_events_created += 1
            _increment_event(summary, decision.event_type)

        if decision.review_type:
            proposed_type = decision.event_type
            if proposed_type is None and decision.semantic == "ambiguous_debit":
                proposed_type = "expense"
            db.add(
                ReviewItem(
                    economic_event_id=event.id if event else None,
                    source_transaction_id=source.id,
                    review_type=decision.review_type,
                    proposed_category_id=_category_id(db, decision.category),
                    proposed_event_type=proposed_type,
                    confidence=decision.confidence,
                    explanation=decision.explanation or "Manuelle Prüfung erforderlich.",
                    status="open",
                )
            )
            summary.review_items += 1
            if decision.event_type is None:
                summary.review_only_rows += 1

    batch.metadata_json = {**(batch.metadata_json or {}), "import_summary": summary.as_dict()}
    return len(rows)


def import_sparda_batch(
    session_factory: sessionmaker[Session], batch_id: int, settings: Settings
) -> None:
    if settings.demo_mode:
        raise PrivateProfileRequiredError("Produktive Sparda-Importe sind im Demo-Profil gesperrt.")
    run_atomic_import(session_factory, batch_id, _import_rows, settings)

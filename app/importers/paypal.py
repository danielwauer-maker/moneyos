from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import asdict, dataclass, replace
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
from app.domain.paypal import classify_paypal_semantic
from app.importers.sparda import PrivateProfileRequiredError
from app.security.redaction import redact_text
from app.services.import_execution import run_atomic_import
from app.services.import_identity import (
    IdentityStatus,
    SourceIdentityTracker,
    add_source_conflict,
)
from app.services.import_staging import preferred_batch_for_source_hash

REQUIRED_COLUMNS = frozenset(
    {
        "Datum",
        "Uhrzeit",
        "Name",
        "Typ",
        "Status",
        "Währung",
        "Brutto",
        "Netto",
        "Transaktionscode",
    }
)


class PayPalFormatError(ValueError):
    def __init__(self, code: str, message: str, row_number: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.row_number = row_number


@dataclass(frozen=True)
class PayPalRow:
    row_number: int
    raw_values: dict[str, str]
    raw_text: str
    occurred_at: datetime
    name: str
    transaction_type: str
    status: str
    currency: str
    gross: Decimal
    fee: Decimal
    net: Decimal
    transaction_code: str
    related_transaction_code: str
    item_name: str
    subject: str
    note: str
    balance_impact: str
    fingerprint: str
    raw_hash: str
    group_key: str = ""


@dataclass(frozen=True)
class PayPalDecision:
    semantic: str
    event_type: str | None
    is_technical: bool
    is_funding: bool
    needs_review: bool
    confidence: Decimal
    explanation: str


@dataclass(frozen=True)
class FundingMatch:
    paypal_row_number: int
    sparda_source_id: int | None
    confidence: str
    score: Decimal
    reason: str


@dataclass
class PayPalImportSummary:
    source_rows_parsed: int = 0
    merchant_payments: int = 0
    refunds: int = 0
    technical_funding_rows: int = 0
    unresolved_rows: int = 0
    high_confidence_sparda_matches: int = 0
    medium_confidence_sparda_matches: int = 0
    unmatched_funding_rows: int = 0
    review_items: int = 0
    new_raw_records: int = 0
    source_transactions_created: int = 0
    economic_events_created: int = 0
    duplicate_rows: int = 0
    duplicate_rows_within_file: int = 0
    existing_exact_rows: int = 0
    conflicting_rows: int = 0
    unique_source_rows: int = 0
    new_rows: int = 0
    duplicate_files: int = 0
    failed_rows: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class PayPalPreviewRow:
    row_number: int
    occurred_at: str
    merchant: str
    amount: str
    semantic: str
    needs_review: bool


@dataclass(frozen=True)
class PayPalDryRunReport:
    source_rows: int
    date_from: str
    date_to: str
    duplicate_file: bool
    existing_batch_status: str | None
    duplicate_rows: int
    merchant_payments: int
    refunds: int
    technical_funding_rows: int
    unresolved_rows: int
    high_confidence_sparda_matches: int
    medium_confidence_sparda_matches: int
    unmatched_funding_rows: int
    review_items: int

    def as_dict(self) -> dict[str, str | int | bool]:
        return asdict(self)


def _normalize_header(value: str) -> str:
    normalized = value.strip().lstrip("\ufeff")
    return "Währung" if normalized == "Waehrung" else normalized


def parse_paypal_decimal(value: str, *, optional: bool = False) -> Decimal:
    cleaned = value.strip().replace("\u00a0", "").replace(" ", "").replace("€", "")
    if not cleaned:
        if optional:
            return Decimal("0")
        raise PayPalFormatError("missing_amount", "Ein erforderlicher PayPal-Betrag fehlt.")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise PayPalFormatError("invalid_decimal", "Ein PayPal-Betrag ist ungültig.") from exc


def parse_paypal_datetime(date_value: str, time_value: str) -> datetime:
    combined = f"{date_value.strip()} {time_value.strip() or '00:00:00'}"
    for pattern in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%y %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(combined, pattern)
        except ValueError:
            continue
    raise PayPalFormatError("invalid_date", "PayPal-Datum oder -Uhrzeit ist ungültig.")


def _fingerprint(values: dict[str, str], occurred_at: datetime, net: Decimal) -> str:
    stable = {
        "transaction_code": values.get("Transaktionscode", "").strip(),
        "related_code": values.get("Zugehöriger Transaktionscode", "").strip(),
        "occurred_at": occurred_at.isoformat(),
        "type": values.get("Typ", "").strip(),
        "status": values.get("Status", "").strip(),
        "name": values.get("Name", "").strip(),
        "net": format(net, "f"),
        "currency": values.get("Währung", "").strip().upper(),
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _natural_key(values: dict[str, str]) -> str:
    stable = {
        "transaction_code": values.get("Transaktionscode", "").strip(),
        "occurred_at": " ".join(
            value.strip() for value in (values.get("Datum", ""), values.get("Uhrzeit", ""))
        ),
        "type": values.get("Typ", "").strip(),
        "name": values.get("Name", "").strip(),
        "net": values.get("Netto", "").strip(),
        "currency": values.get("Währung", "").strip().upper(),
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"paypal|{payload}".encode()).hexdigest()


def _apply_identity_summary(
    summary: PayPalImportSummary, rows: list[PayPalRow], db: Session
) -> None:
    tracker = SourceIdentityTracker(db, "paypal", _natural_key)
    for row in rows:
        result = tracker.classify(
            natural_key=_natural_key(row.raw_values),
            content_hash=row.fingerprint,
            payload=row.raw_values,
        )
        if result.status == IdentityStatus.DUPLICATE_WITHIN_FILE:
            summary.duplicate_rows_within_file += 1
        elif result.status == IdentityStatus.EXISTING_EXACT:
            summary.existing_exact_rows += 1
        elif result.status == IdentityStatus.EXISTING_CONFLICT:
            summary.conflicting_rows += 1
        else:
            summary.new_rows += 1
    summary.duplicate_rows = summary.duplicate_rows_within_file + summary.existing_exact_rows
    summary.unique_source_rows = len(rows) - summary.duplicate_rows_within_file


def _group_rows(rows: list[PayPalRow]) -> list[PayPalRow]:
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for row in rows:
        code = row.transaction_code or row.fingerprint
        find(code)
        if row.related_transaction_code:
            union(code, row.related_transaction_code)
    grouped: list[PayPalRow] = []
    for row in rows:
        code = row.transaction_code or row.fingerprint
        root = find(code)
        group_key = hashlib.sha256(root.encode("utf-8")).hexdigest()
        grouped.append(replace(row, group_key=group_key))
    return grouped


def parse_paypal_csv(path: Path) -> list[PayPalRow]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeError as exc:
        raise PayPalFormatError("invalid_encoding", "PayPal CSV muss UTF-8 sein.") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:16384], delimiters=";,")
    except csv.Error as exc:
        raise PayPalFormatError("unsupported_delimiter", "CSV-Trennzeichen nicht erkannt.") from exc
    if dialect.delimiter not in {";", ","}:
        raise PayPalFormatError("unsupported_delimiter", "CSV-Trennzeichen nicht unterstützt.")
    physical_lines = text.splitlines(keepends=True)
    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=dialect.delimiter,
        quotechar='"',
        doublequote=True,
        strict=True,
    )
    try:
        original_header = next(reader)
    except (StopIteration, csv.Error) as exc:
        raise PayPalFormatError("missing_header", "Die PayPal-Kopfzeile fehlt.") from exc
    header = [_normalize_header(column) for column in original_header]
    if len(header) != len(set(header)):
        raise PayPalFormatError("duplicate_columns", "Die PayPal CSV enthält doppelte Spalten.")
    missing = sorted(REQUIRED_COLUMNS - set(header))
    if missing:
        raise PayPalFormatError(
            "missing_required_columns",
            f"Erforderliche PayPal-Spalten fehlen: {', '.join(missing)}",
        )

    rows: list[PayPalRow] = []
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
                raise PayPalFormatError(
                    "unexpected_columns",
                    "Eine PayPal-Zeile enthält zu viele Spalten.",
                    row_number,
                )
            values.extend([""] * (len(header) - len(values)))
            raw_values = {column: value for column, value in zip(header, values, strict=True)}
            try:
                occurred_at = parse_paypal_datetime(raw_values["Datum"], raw_values["Uhrzeit"])
                gross = parse_paypal_decimal(raw_values["Brutto"])
                fee = parse_paypal_decimal(raw_values.get("Gebühr", ""), optional=True)
                net = parse_paypal_decimal(raw_values["Netto"])
            except PayPalFormatError as exc:
                raise PayPalFormatError(exc.code, exc.message, row_number) from exc
            currency = raw_values["Währung"].strip().upper()
            if not currency:
                raise PayPalFormatError("missing_currency", "PayPal-Währung fehlt.", row_number)
            transaction_code = raw_values["Transaktionscode"].strip()
            fingerprint = _fingerprint(raw_values, occurred_at, net)
            rows.append(
                PayPalRow(
                    row_number=row_number,
                    raw_values=raw_values,
                    raw_text=raw_text,
                    occurred_at=occurred_at,
                    name=raw_values["Name"].strip(),
                    transaction_type=raw_values["Typ"].strip(),
                    status=raw_values["Status"].strip(),
                    currency=currency,
                    gross=gross,
                    fee=fee,
                    net=net,
                    transaction_code=transaction_code,
                    related_transaction_code=raw_values.get(
                        "Zugehöriger Transaktionscode", ""
                    ).strip(),
                    item_name=raw_values.get("Artikelbezeichnung", "").strip(),
                    subject=raw_values.get("Betreff", "").strip(),
                    note=raw_values.get("Hinweis", "").strip(),
                    balance_impact=raw_values.get("Auswirkung auf Guthaben", "").strip(),
                    fingerprint=fingerprint,
                    raw_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
                )
            )
    except csv.Error as exc:
        raise PayPalFormatError("invalid_csv", "Die PayPal CSV-Struktur ist ungültig.") from exc
    if not rows:
        raise PayPalFormatError("no_data_rows", "Die PayPal CSV enthält keine Datenzeilen.")
    return _group_rows(rows)


def classify_paypal_row(row: PayPalRow) -> PayPalDecision:
    semantic = classify_paypal_semantic(
        row.transaction_type,
        row.status,
        row.gross,
        has_name=bool(row.name),
    )
    if semantic in {"technical", "funding"}:
        return PayPalDecision(
            semantic=semantic,
            event_type=None,
            is_technical=True,
            is_funding=semantic == "funding",
            needs_review=False,
            confidence=Decimal("0.99"),
            explanation="Technische PayPal-Zeile ohne eigenes wirtschaftliches Ereignis.",
        )
    if semantic == "refund":
        return PayPalDecision(
            semantic="refund",
            event_type="refund",
            is_technical=False,
            is_funding=False,
            needs_review=False,
            confidence=Decimal("0.95"),
            explanation="Abgeschlossene PayPal-Rückzahlung.",
        )
    if semantic == "merchant_payment":
        return PayPalDecision(
            semantic="merchant_payment",
            event_type="expense",
            is_technical=False,
            is_funding=False,
            needs_review=False,
            confidence=Decimal("0.95"),
            explanation="Abgeschlossene PayPal-Händlerzahlung.",
        )
    return PayPalDecision(
        semantic="unresolved",
        event_type=None,
        is_technical=False,
        is_funding=False,
        needs_review=True,
        confidence=Decimal("0.40"),
        explanation="PayPal-Zeile besitzt keine eindeutig sichere wirtschaftliche Bedeutung.",
    )


def _sparda_funding_sources(db: Session) -> list[SourceTransaction]:
    return list(
        db.scalars(select(SourceTransaction).where(SourceTransaction.source_system == "sparda"))
    )


def match_sparda_funding(db: Session, rows: list[PayPalRow]) -> dict[int, FundingMatch]:
    sparda = [
        source
        for source in _sparda_funding_sources(db)
        if (source.metadata_json or {}).get("sparda_semantic") == "paypal_funding_leg"
        or "paypal" in (source.merchant_raw or "").casefold()
        or "paypal" in source.description_raw.casefold()
    ]
    results: dict[int, FundingMatch] = {}
    used_high: set[int] = set()
    for row in rows:
        decision = classify_paypal_row(row)
        if not decision.is_funding:
            continue
        candidates = []
        references = {
            value.casefold()
            for value in (row.transaction_code, row.related_transaction_code)
            if value
        }
        for source in sparda:
            if abs(abs(source.amount) - abs(row.gross)) > Decimal("0.01") and abs(
                abs(source.amount) - abs(row.net)
            ) > Decimal("0.01"):
                continue
            days = abs((source.booked_at.date() - row.occurred_at.date()).days)
            if days > 3:
                continue
            haystack = f"{source.merchant_raw or ''} {source.description_raw}".casefold()
            explicit = any(reference in haystack for reference in references)
            candidates.append((source, days, explicit))
        explicit_candidates = [item for item in candidates if item[2]]
        same_day = [item for item in candidates if item[1] <= 1]
        if len(explicit_candidates) == 1 and explicit_candidates[0][0].id not in used_high:
            source = explicit_candidates[0][0]
            used_high.add(source.id)
            results[row.row_number] = FundingMatch(
                row.row_number,
                source.id,
                "high",
                Decimal("0.99"),
                "Explizite Zahlungsreferenz und identischer Betrag.",
            )
        elif len(same_day) == 1 and same_day[0][0].id not in used_high:
            source = same_day[0][0]
            used_high.add(source.id)
            results[row.row_number] = FundingMatch(
                row.row_number,
                source.id,
                "high",
                Decimal("0.95"),
                "Eindeutiger PayPal-Transfer mit identischem Betrag und engem Datumsfenster.",
            )
        elif len(candidates) == 1:
            results[row.row_number] = FundingMatch(
                row.row_number,
                candidates[0][0].id,
                "medium",
                Decimal("0.75"),
                "Eindeutiger Betragskandidat innerhalb von drei Tagen; Bestätigung erforderlich.",
            )
        else:
            results[row.row_number] = FundingMatch(
                row.row_number,
                None,
                "unresolved",
                Decimal("0"),
                "Kein eindeutiger Sparda-Finanzierungstransfer gefunden.",
            )
    return results


def _summary(rows: list[PayPalRow], matches: dict[int, FundingMatch]) -> PayPalImportSummary:
    summary = PayPalImportSummary(source_rows_parsed=len(rows))
    for row in rows:
        decision = classify_paypal_row(row)
        if decision.semantic == "merchant_payment":
            summary.merchant_payments += 1
        elif decision.semantic == "refund":
            summary.refunds += 1
        elif decision.is_technical:
            summary.technical_funding_rows += 1
        else:
            summary.unresolved_rows += 1
        match = matches.get(row.row_number)
        if match and match.confidence == "high":
            summary.high_confidence_sparda_matches += 1
        elif match and match.confidence == "medium":
            summary.medium_confidence_sparda_matches += 1
        elif decision.is_funding:
            summary.unmatched_funding_rows += 1
        ambiguous_technical_link = (
            decision.is_technical and len(_event_target_row_numbers(row, rows)) > 1
        )
        if (
            decision.needs_review
            or ambiguous_technical_link
            or (match and match.confidence != "high")
        ):
            summary.review_items += 1
    return summary


def preview_paypal_file(
    path: Path, db: Session
) -> tuple[list[PayPalPreviewRow], PayPalImportSummary, dict[int, FundingMatch]]:
    rows = parse_paypal_csv(path)
    matches = match_sparda_funding(db, rows)
    summary = _summary(rows, matches)
    _apply_identity_summary(summary, rows, db)
    preview = [
        PayPalPreviewRow(
            row_number=row.row_number,
            occurred_at=row.occurred_at.strftime("%d.%m.%Y"),
            merchant=redact_text(row.name or "PayPal")[:120],
            amount=f"{row.gross:.2f} {row.currency}",
            semantic=classify_paypal_row(row).semantic,
            needs_review=(
                classify_paypal_row(row).needs_review
                or (row.row_number in matches and matches[row.row_number].confidence != "high")
            ),
        )
        for row in rows
    ]
    return preview, summary, matches


def dry_run_paypal_file(path: Path, db: Session) -> PayPalDryRunReport:
    """Analyze a PayPal export without staging it or mutating database state."""
    rows = parse_paypal_csv(path)
    matches = match_sparda_funding(db, rows)
    summary = _summary(rows, matches)
    _apply_identity_summary(summary, rows, db)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    existing_batch = preferred_batch_for_source_hash(db, source_hash)
    duplicate_file = existing_batch is not None and existing_batch.status == "imported"
    dates = [row.occurred_at.date() for row in rows]
    return PayPalDryRunReport(
        source_rows=len(rows),
        date_from=min(dates).isoformat(),
        date_to=max(dates).isoformat(),
        duplicate_file=duplicate_file,
        existing_batch_status=existing_batch.status if existing_batch else None,
        duplicate_rows=summary.duplicate_rows,
        merchant_payments=summary.merchant_payments,
        refunds=summary.refunds,
        technical_funding_rows=summary.technical_funding_rows,
        unresolved_rows=summary.unresolved_rows,
        high_confidence_sparda_matches=summary.high_confidence_sparda_matches,
        medium_confidence_sparda_matches=summary.medium_confidence_sparda_matches,
        unmatched_funding_rows=summary.unmatched_funding_rows,
        review_items=summary.review_items,
    )


def _paypal_account(db: Session) -> Account | None:
    return db.scalar(select(Account).where(Account.name == "PayPal"))


def _event_target_row_numbers(row: PayPalRow, rows: list[PayPalRow]) -> set[int]:
    direct_candidates: set[int] = set()
    for candidate in rows:
        if classify_paypal_row(candidate).event_type is None:
            continue
        directly_related = (
            bool(row.related_transaction_code)
            and candidate.transaction_code == row.related_transaction_code
        ) or (
            bool(candidate.related_transaction_code)
            and candidate.related_transaction_code == row.transaction_code
        )
        if directly_related:
            direct_candidates.add(candidate.row_number)
    if direct_candidates:
        return direct_candidates
    group_candidates = {
        candidate.row_number
        for candidate in rows
        if candidate.group_key == row.group_key
        and classify_paypal_row(candidate).event_type is not None
    }
    return group_candidates


def _linked_event_for_row(
    row: PayPalRow,
    rows: list[PayPalRow],
    created_events: dict[int, EconomicEvent],
) -> EconomicEvent | None:
    targets = _event_target_row_numbers(row, rows)
    if len(targets) != 1:
        return None
    return created_events.get(next(iter(targets)))


def _import_rows(db: Session, batch: ImportBatch, path: Path) -> int:
    rows = parse_paypal_csv(path)
    matches = match_sparda_funding(db, rows)
    summary = _summary(rows, matches)
    summary.review_items = 0
    account = _paypal_account(db)
    created_sources: dict[int, SourceTransaction] = {}
    sources_by_code: dict[str, SourceTransaction] = {}
    created_events: dict[int, EconomicEvent] = {}
    tracker = SourceIdentityTracker(db, "paypal", _natural_key)

    for row in rows:
        natural_key = _natural_key(row.raw_values)
        identity = tracker.classify(
            natural_key=natural_key,
            content_hash=row.fingerprint,
            payload=row.raw_values,
        )
        if identity.status != IdentityStatus.NEW:
            if identity.status == IdentityStatus.DUPLICATE_WITHIN_FILE:
                summary.duplicate_rows_within_file += 1
                summary.duplicate_rows += 1
            elif identity.status == IdentityStatus.EXISTING_EXACT:
                summary.existing_exact_rows += 1
                summary.duplicate_rows += 1
            else:
                summary.conflicting_rows += 1
                add_source_conflict(
                    db,
                    batch,
                    source_system="paypal",
                    natural_key=natural_key,
                    incoming_content_hash=row.fingerprint,
                    result=identity,
                )
            continue
        summary.new_rows += 1
        decision = classify_paypal_row(row)
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
            source_system="paypal",
            source_transaction_id=f"paypal:{row.fingerprint}",
            related_source_transaction_id=(
                f"paypal:{row.related_transaction_code}" if row.related_transaction_code else None
            ),
            booked_at=row.occurred_at,
            value_at=row.occurred_at,
            merchant_raw=row.name or None,
            description_raw=" | ".join(
                value
                for value in (row.transaction_type, row.item_name, row.subject, row.note)
                if value
            ),
            amount=row.net,
            currency=row.currency,
            source_account_hint="PayPal",
            status=row.status or "unknown",
            metadata_json={
                "paypal_semantic": decision.semantic,
                "paypal_group_key": row.group_key,
                "balance_impact": row.balance_impact,
                "technical": decision.is_technical,
            },
            fingerprint=row.fingerprint,
            source_natural_key=natural_key,
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
        if row.transaction_code:
            sources_by_code[row.transaction_code] = source
        summary.new_raw_records += 1
        summary.source_transactions_created += 1

        if decision.event_type:
            event = EconomicEvent(
                event_type=decision.event_type,
                occurred_at=row.occurred_at,
                description=redact_text(row.name or row.item_name or "PayPal")[:255],
                amount=abs(row.gross),
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
                    review_type="paypal_economic_type",
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
        decision = classify_paypal_row(row)
        if not decision.is_technical:
            continue
        event = _linked_event_for_row(row, rows, created_events)
        link_type = (
            "funding_leg"
            if decision.is_funding
            else ("authorization" if "autoris" in row.transaction_type.casefold() else "enrichment")
        )
        if event is not None:
            db.add(
                EventSourceLink(
                    economic_event_id=event.id,
                    source_transaction_id=source.id,
                    link_type=link_type,
                    confidence=decision.confidence,
                )
            )
        elif not decision.is_funding and len(_event_target_row_numbers(row, rows)) > 1:
            db.add(
                ReviewItem(
                    source_transaction_id=source.id,
                    review_type="paypal_group_link",
                    confidence=Decimal("0"),
                    explanation="Technische Zeile ist keinem einzelnen PayPal-Ereignis zuordenbar.",
                    status="open",
                )
            )
            summary.review_items += 1
        match = matches.get(row.row_number)
        if decision.is_funding and match and match.confidence == "high" and event is not None:
            db.add(
                EventSourceLink(
                    economic_event_id=event.id,
                    source_transaction_id=match.sparda_source_id,
                    link_type="funding_leg",
                    confidence=match.score,
                    notes="Konservativ bestätigtes PayPal-/Sparda-Funding-Match",
                )
            )
        elif decision.is_funding and match and (match.confidence != "high" or event is None):
            db.add(
                ReviewItem(
                    economic_event_id=event.id if event else None,
                    source_transaction_id=source.id,
                    review_type="paypal_sparda_match",
                    proposed_event_type="transfer",
                    confidence=match.score,
                    explanation=(
                        match.reason
                        if event is not None
                        else "Funding-Zeile ist keinem einzelnen PayPal-Ereignis zuordenbar."
                    ),
                    status="open",
                )
            )
            summary.review_items += 1

    for row in rows:
        if classify_paypal_row(row).semantic != "refund" or not row.related_transaction_code:
            continue
        refund_event = created_events.get(row.row_number)
        original_source = sources_by_code.get(row.related_transaction_code)
        if refund_event is not None and original_source is not None:
            db.add(
                EventSourceLink(
                    economic_event_id=refund_event.id,
                    source_transaction_id=original_source.id,
                    link_type="refund_origin",
                    confidence=Decimal("0.99"),
                )
            )

    summary.unique_source_rows = len(rows) - summary.duplicate_rows_within_file
    batch.metadata_json = {**(batch.metadata_json or {}), "import_summary": summary.as_dict()}
    return len(rows)


def import_paypal_batch(
    session_factory: sessionmaker[Session], batch_id: int, settings: Settings
) -> None:
    if settings.demo_mode:
        raise PrivateProfileRequiredError("Produktive PayPal-Importe sind im Demo-Profil gesperrt.")
    run_atomic_import(session_factory, batch_id, _import_rows, settings)

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    AmazonEnrichmentRecord,
    AmazonPaymentMatch,
    EconomicEvent,
    EventSourceLink,
    ImportBatch,
    ImportConflict,
    RawImportRecord,
    SourceTransaction,
)
from app.importers.sparda import PrivateProfileRequiredError
from app.security.redaction import redact_text
from app.services.import_execution import run_atomic_import
from app.services.import_identity import differing_field_names
from app.services.import_staging import preferred_batch_for_source_hash

RELEVANT_FILES = {
    "Your Amazon Orders/Order History.csv": "order_item",
    "Your Amazon Orders/Digital Content Orders.csv": "digital_order_item",
    "Additional Data/Your Orders.Returns.2/Your Orders.Returns.2.csv": "refund",
    "Your Amazon Orders/Digital Returns.csv": "refund",
    "Your Returns & Refunds/Refund Details.csv": "refund",
    "Your Returns & Refunds/Return Requests.csv": "return",
    "Your Returns & Refunds/Returns Status.csv": "return",
    "Your Returns & Refunds/Replacement Orders.csv": "replacement",
}
MAX_MEMBER_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 250 * 1024 * 1024
DEFAULT_AMAZON_SCOPE_START = date(2026, 1, 1)


class AmazonFormatError(ValueError):
    def __init__(self, code: str, message: str, source_file: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.source_file = source_file


@dataclass(frozen=True)
class AmazonRow:
    source_file: str
    row_number: int
    record_type: str
    raw_values: dict[str, str]
    raw_text: str
    natural_key: str
    content_hash: str
    raw_hash: str
    order_key: str | None
    item_key: str | None
    occurred_at: datetime | None
    amount: Decimal | None
    currency: str | None
    product_title: str | None
    asin: str | None
    payment_method: str | None
    gift_card_or_split: bool
    category_suggestion: str | None


@dataclass(frozen=True)
class AmazonMatch:
    group_key: str
    economic_event_id: int | None
    confidence: str
    score: Decimal
    match_type: str
    reason: str


@dataclass(frozen=True)
class AmazonPreviewRow:
    row_number: int
    source_kind: str
    occurred_at: str
    safe_title: str
    amount: str
    match_confidence: str
    category_suggestion: str


@dataclass
class AmazonImportSummary:
    archive_source_rows: int = 0
    archive_orders: int = 0
    archive_order_items: int = 0
    archive_returns_refunds: int = 0
    archive_replacements: int = 0
    archive_date_from: str = ""
    archive_date_to: str = ""
    scope_start_date: str = ""
    scope_date_from: str = ""
    scope_date_to: str = ""
    excluded_before_scope: int = 0
    excluded_undated: int = 0
    source_rows_parsed: int = 0
    unique_source_rows: int = 0
    duplicate_rows_within_file: int = 0
    existing_exact_rows: int = 0
    new_rows: int = 0
    conflicting_rows: int = 0
    orders: int = 0
    order_items: int = 0
    returns_refunds: int = 0
    high_confidence_payment_matches: int = 0
    medium_confidence_payment_matches: int = 0
    unmatched: int = 0
    gift_card_split_cases: int = 0
    enrichment_links_would_create: int = 0
    review_items: int = 0
    new_raw_records: int = 0
    source_transactions_created: int = 0
    economic_events_created: int = 0
    duplicate_rows: int = 0
    duplicate_files: int = 0
    failed_rows: int = 0

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(frozen=True)
class AmazonAnalysis:
    rows: list[AmazonRow]
    summary: AmazonImportSummary
    matches: dict[str, AmazonMatch]
    identity_status: dict[tuple[str, str, int], str]


def _hash(namespace: str, value: str) -> str:
    return hashlib.sha256(f"{namespace}|{value}".encode()).hexdigest()


def _decode(data: bytes, source_file: str) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise AmazonFormatError(
        "invalid_encoding", "Amazon CSV kann nicht gelesen werden.", source_file
    )


def _decimal(value: str) -> Decimal | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = re.sub(r"[^0-9,.-]", "", cleaned)
    if not cleaned:
        return None
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
        raise AmazonFormatError("invalid_decimal", "Amazon-Betrag ist ungültig.") from exc
    return -result if negative else result


def _date(value: str) -> datetime | None:
    cleaned = value.strip()
    if not cleaned or cleaned.casefold() in {"not applicable", "n/a", "na"}:
        return None
    for candidate in (cleaned, cleaned[:19], cleaned[:10]):
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            return parsed.replace(tzinfo=None)
        except ValueError:
            pass
    for pattern in ("%m/%d/%Y", "%d.%m.%Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(cleaned, pattern)
        except ValueError:
            pass
    raise AmazonFormatError("invalid_date", "Amazon-Datum ist ungültig.")


def _first(values: dict[str, str], *keys: str) -> str:
    return next((values[key].strip() for key in keys if values.get(key, "").strip()), "")


def _identity_value(values: dict[str, str], *keys: str) -> str:
    value = _first(values, *keys)
    if value.casefold() in {"not applicable", "n/a", "na", "none", "null"}:
        return ""
    return value


def _category(title: str) -> str | None:
    text = title.casefold()
    rules = (
        (("shirt", "hose", "kleid", "schuh", "sock", "jacke"), "Kleidung / Kleidung"),
        (("spielzeug", "lego", "game", "puzzle", "hobby"), "Freizeit"),
        (("kabel", "adapter", "software", "elektronik", "computer"), "Elektronik"),
        (("küche", "haushalt", "reiniger", "möbel", "lampe"), "Wohnen & Haushalt"),
    )
    for markers, path in rules:
        if any(marker in text for marker in markers):
            return path
    return None


def _natural_components(source_file: str, record_type: str, values: dict[str, str]) -> list[str]:
    order_id = _identity_value(values, "Order ID")
    if source_file.endswith("Order History.csv"):
        return [
            order_id,
            _first(values, "ASIN"),
            _first(values, "Original Quantity"),
            _first(values, "Ship Date"),
            _first(values, "Product Name"),
        ]
    if source_file.endswith("Digital Content Orders.csv"):
        return [
            order_id,
            _identity_value(values, "Digital Order Item ID"),
            _identity_value(values, "Component Type"),
            _identity_value(values, "Offer Type Code"),
            _identity_value(values, "Claim Code"),
            _identity_value(values, "Transaction Amount"),
            _identity_value(values, "Payment Information"),
        ]
    if source_file.endswith("Your Orders.Returns.2.csv"):
        return [
            _identity_value(values, "Return Item ID"),
            order_id,
            _identity_value(values, "Order Item ID"),
            _identity_value(values, "Request Date"),
            _identity_value(values, "Refund Amount"),
            _identity_value(values, "Refund Destination"),
        ]
    if source_file.endswith("Digital Returns.csv"):
        return [
            order_id,
            _identity_value(values, "Digital Order Item ID"),
            _identity_value(values, "Return Date"),
            _identity_value(values, "Settle Type"),
            _identity_value(values, "Monetary Component Type"),
            _identity_value(values, "Amount Refunded"),
            _identity_value(values, "Transaction Amount"),
            _identity_value(values, "Payment Information"),
        ]
    if source_file.endswith("Refund Details.csv"):
        return [
            order_id,
            _first(values, "Creation Date"),
            _first(values, "Refund Date"),
            _first(values, "Disbursement Type"),
            _first(values, "Refund Amount", "Direct Debit Refund Amount"),
            _first(values, "Quantity"),
        ]
    if source_file.endswith("Returns Status.csv"):
        return [
            _identity_value(values, "Return Authorization ID"),
            order_id,
            _identity_value(values, "Contract Unit ID", "Carrier Package ID"),
            _identity_value(values, "Contract ID"),
            _identity_value(values, "Return Receivable Tracking ID"),
            _identity_value(values, "Return Receivable Initiation Date"),
            _identity_value(values, "Return Creation Date", "Date of Return"),
            _identity_value(values, "Return Amount"),
            _identity_value(values, "Return Reason"),
        ]
    if source_file.endswith("Return Requests.csv"):
        return [order_id, _first(values, "ASIN"), _first(values, "Return Reason Code")]
    if source_file.endswith("Replacement Orders.csv"):
        return [order_id, _first(values, "Replacement Order ID")]
    return [record_type, json.dumps(values, ensure_ascii=False, sort_keys=True)]


def _parse_member(source_file: str, record_type: str, data: bytes) -> list[AmazonRow]:
    text = _decode(data, source_file)
    try:
        dialect = csv.Sniffer().sniff(text[:16384], delimiters=",;\t")
    except csv.Error:
        header = text.splitlines()[0] if text.splitlines() else ""
        delimiter = max((",", ";", "\t"), key=header.count)
        if not header or header.count(delimiter) == 0:
            raise AmazonFormatError(
                "invalid_csv", "Amazon CSV-Struktur ist ungültig.", source_file
            ) from None
        dialect = None
    try:
        if dialect is None:
            reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        else:
            reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect)
        fieldnames = next(reader, None)
    except csv.Error as exc:
        raise AmazonFormatError(
            "invalid_csv", "Amazon CSV-Struktur ist ungültig.", source_file
        ) from exc
    if not fieldnames:
        raise AmazonFormatError("missing_header", "Amazon CSV-Kopfzeile fehlt.", source_file)
    fieldnames = [str(field).strip() for field in fieldnames]
    rows: list[AmazonRow] = []
    try:
        for row_number, source in enumerate(reader, start=2):
            if len(source) > len(fieldnames) and "Product Name" in fieldnames:
                surplus = len(source) - len(fieldnames)
                product_index = fieldnames.index("Product Name")
                product_parts = source[product_index : product_index + surplus + 1]
                source = (
                    source[:product_index]
                    + [",".join(product_parts)]
                    + source[product_index + surplus + 1 :]
                )
            if len(source) != len(fieldnames):
                raise AmazonFormatError(
                    "invalid_row_width",
                    "Amazon CSV-Zeile besitzt eine ungültige Spaltenzahl.",
                    source_file,
                    row_number,
                )
            values = {
                key: str(value or "").strip() for key, value in zip(fieldnames, source, strict=True)
            }
            if not any(values.values()):
                continue
            payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            natural_payload = json.dumps(
                _natural_components(source_file, record_type, values),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            order_id = _first(values, "Order ID")
            item_id = _first(
                values,
                "Order Item ID",
                "Digital Order Item ID",
                "Return Item ID",
                "Return Authorization ID",
                "ASIN",
            )
            occurred = _date(
                _first(
                    values,
                    "Order Date",
                    "Refund Date",
                    "Return Date",
                    "Request Date",
                    "Return Creation Date",
                    "Date of Return",
                    "Creation Date",
                )
            )
            amount = _decimal(
                _first(
                    values,
                    "Shipment Item Subtotal",
                    "Transaction Amount",
                    "Refund Amount",
                    "Amount Refunded",
                    "Return Amount",
                )
            )
            currency = _first(
                values,
                "Currency",
                "Currency Code",
                "Base Currency",
                "Return Amount Currency",
            ).upper()
            currency = currency if len(currency) == 3 else None
            title = _first(values, "Product Name") or None
            payment = (
                _first(
                    values,
                    "Payment Method Type",
                    "Payment Information",
                    "Payment Method Service Type",
                )
                or None
            )
            gift_fields = " ".join(
                _first(values, key)
                for key in ("Gift Card", "Refund to Gift Certificate", "Gift Redemption")
            )
            gift_or_split = "gift" in f"{payment or ''} {gift_fields}".casefold()
            rows.append(
                AmazonRow(
                    source_file=source_file,
                    row_number=row_number,
                    record_type=record_type,
                    raw_values=values,
                    raw_text=payload,
                    natural_key=_hash(f"amazon-{record_type}", natural_payload),
                    content_hash=_hash(f"amazon-content-{source_file}", payload),
                    raw_hash=_hash(f"amazon-raw-{source_file}", payload),
                    order_key=_hash("amazon-order", order_id) if order_id else None,
                    item_key=_hash("amazon-item", item_id) if item_id else None,
                    occurred_at=occurred,
                    amount=amount,
                    currency=currency,
                    product_title=title,
                    asin=_first(values, "ASIN") or None,
                    payment_method=payment,
                    gift_card_or_split=gift_or_split,
                    category_suggestion=_category(title or ""),
                )
            )
    except csv.Error as exc:
        raise AmazonFormatError(
            "invalid_csv", "Amazon CSV-Struktur ist ungültig.", source_file
        ) from exc
    return rows


def parse_amazon_export(path: Path) -> list[AmazonRow]:
    if path.suffix.casefold() != ".zip":
        raise AmazonFormatError("unsupported_format", "Amazon-Enrichment benötigt das Bestell-ZIP.")
    try:
        with zipfile.ZipFile(path) as archive:
            files = [entry for entry in archive.infolist() if not entry.is_dir()]
            if sum(entry.file_size for entry in files) > MAX_TOTAL_BYTES:
                raise AmazonFormatError("archive_too_large", "Amazon-Archiv ist entpackt zu groß.")
            names = {entry.filename: entry for entry in files}
            if any(
                PurePosixPath(entry.filename).is_absolute()
                or ".." in PurePosixPath(entry.filename).parts
                for entry in files
            ):
                raise AmazonFormatError(
                    "unsafe_archive_path", "Amazon-Archiv enthält unsichere Pfade."
                )
            if "Your Amazon Orders/Order History.csv" not in names:
                raise AmazonFormatError(
                    "missing_order_history", "Amazon Order History.csv fehlt im Archiv."
                )
            rows: list[AmazonRow] = []
            for source_file, record_type in RELEVANT_FILES.items():
                entry = names.get(source_file)
                if entry is None:
                    continue
                if entry.file_size > MAX_MEMBER_BYTES:
                    raise AmazonFormatError(
                        "archive_member_too_large", "Amazon CSV ist zu groß.", source_file
                    )
                rows.extend(_parse_member(source_file, record_type, archive.read(entry)))
    except zipfile.BadZipFile as exc:
        raise AmazonFormatError(
            "invalid_zip", "Amazon-Archiv ist keine gültige ZIP-Datei."
        ) from exc
    if not rows:
        raise AmazonFormatError("no_data_rows", "Amazon-Archiv enthält keine relevanten Daten.")
    return rows


def _event_candidates(db: Session) -> list[tuple[EconomicEvent, SourceTransaction]]:
    return list(
        db.execute(
            select(EconomicEvent, SourceTransaction)
            .join(EventSourceLink, EventSourceLink.economic_event_id == EconomicEvent.id)
            .join(SourceTransaction, SourceTransaction.id == EventSourceLink.source_transaction_id)
            .where(
                EventSourceLink.link_type == "canonical_source",
                SourceTransaction.source_system.in_(("amex", "paypal", "sparda")),
            )
        )
    )


def _amazon_source(source: SourceTransaction) -> bool:
    text = " ".join(
        (
            source.merchant_raw or "",
            source.description_raw or "",
            str((source.metadata_json or {}).get("canonical_merchant", "")),
        )
    ).casefold()
    return any(marker in text for marker in ("amazon", "amzn", "audible", "kindle"))


def _payment_sources(payment_method: str | None) -> set[str]:
    text = (payment_method or "").casefold()
    result: set[str] = set()
    if "american express" in text or "amex" in text:
        result.add("amex")
    if "paypal" in text:
        result.add("paypal")
    if any(marker in text for marker in ("bank", "lastschrift", "giro", "sepa")):
        result.add("sparda")
    return result


def match_amazon_payments(db: Session, rows: list[AmazonRow]) -> dict[str, AmazonMatch]:
    events = [(event, source) for event, source in _event_candidates(db) if _amazon_source(source)]
    groups: dict[str, list[AmazonRow]] = defaultdict(list)
    for row in rows:
        if row.order_key and row.record_type in {"order_item", "digital_order_item"}:
            groups[f"payment:{row.order_key}"].append(row)
        elif row.record_type == "refund" and row.amount is not None:
            groups[f"refund:{row.natural_key}"].append(row)

    candidate_map: dict[str, list[tuple[EconomicEvent, SourceTransaction, int]]] = {}
    group_meta: dict[str, tuple[str, bool]] = {}
    for group_key, group in groups.items():
        match_type = "refund" if group_key.startswith("refund:") else "payment"
        amounts = {
            abs(value)
            for row in group
            if (
                value := (
                    _decimal(_first(row.raw_values, "Total Amount"))
                    if match_type == "payment"
                    else row.amount
                )
            )
            is not None
            and value != 0
        }
        amount = next(iter(amounts)) if len(amounts) == 1 else None
        dates = [row.occurred_at for row in group if row.occurred_at]
        occurred = min(dates) if dates else None
        hints = set().union(*(_payment_sources(row.payment_method) for row in group))
        split = any(row.gift_card_or_split for row in group) or len(hints) > 1
        group_meta[group_key] = (match_type, split)
        candidates: list[tuple[EconomicEvent, SourceTransaction, int]] = []
        if amount is not None and occurred is not None:
            for event, source in events:
                if event.event_type != match_type.replace("payment", "expense"):
                    continue
                if abs(event.amount) != amount or (hints and source.source_system not in hints):
                    continue
                days = abs((event.occurred_at.date() - occurred.date()).days)
                if days <= (21 if match_type == "refund" else 10):
                    candidates.append((event, source, days))
        candidate_map[group_key] = sorted(candidates, key=lambda candidate: candidate[2])

    event_frequency = Counter(
        candidates[0][0].id for candidates in candidate_map.values() if len(candidates) == 1
    )
    matches: dict[str, AmazonMatch] = {}
    for group_key, candidates in candidate_map.items():
        match_type, split = group_meta[group_key]
        if len(candidates) == 1 and not split and event_frequency[candidates[0][0].id] == 1:
            event, _source, days = candidates[0]
            matches[group_key] = AmazonMatch(
                group_key,
                event.id,
                "high",
                Decimal("0.98") if days <= 3 else Decimal("0.95"),
                match_type,
                "Eindeutiger Amazon-Betrag, Zahlungsweg und Datumsabstand.",
            )
        elif candidates:
            event, _source, _days = candidates[0]
            matches[group_key] = AmazonMatch(
                group_key,
                event.id,
                "medium",
                Decimal("0.65"),
                match_type,
                "Plausibles Amazon-Match benötigt manuelle Bestätigung.",
            )
        else:
            matches[group_key] = AmazonMatch(
                group_key,
                None,
                "unmatched",
                Decimal("0"),
                match_type,
                "Kein ausreichend sicheres vorhandenes Zahlungsereignis.",
            )
    return matches


def _effective_dates(rows: list[AmazonRow]) -> dict[tuple[str, str, int], datetime | None]:
    order_dates: dict[str, datetime] = {}
    for row in rows:
        order_id = _identity_value(row.raw_values, "Order ID")
        if order_id and row.occurred_at and row.record_type in {"order_item", "digital_order_item"}:
            current = order_dates.get(order_id)
            if current is None or row.occurred_at < current:
                order_dates[order_id] = row.occurred_at
    return {
        (row.source_file, row.record_type, row.row_number): (
            row.occurred_at or order_dates.get(_identity_value(row.raw_values, "Order ID"))
        )
        for row in rows
    }


def analyze_amazon_export(
    path: Path,
    db: Session,
    scope_start: date = DEFAULT_AMAZON_SCOPE_START,
) -> AmazonAnalysis:
    archive_rows = parse_amazon_export(path)
    effective_dates = _effective_dates(archive_rows)
    dated_archive = [value for value in effective_dates.values() if value is not None]
    rows = [
        row
        for row in archive_rows
        if (effective := effective_dates[(row.source_file, row.record_type, row.row_number)])
        is not None
        and effective.date() >= scope_start
    ]
    scoped_dates = [
        effective_dates[(row.source_file, row.record_type, row.row_number)] for row in rows
    ]
    existing = {
        (record.record_type, record.natural_key): record
        for record in db.scalars(select(AmazonEnrichmentRecord))
    }
    seen_content: set[str] = set()
    seen_natural: dict[tuple[str, str], AmazonRow] = {}
    statuses: dict[tuple[str, str, int], str] = {}
    summary = AmazonImportSummary(
        archive_source_rows=len(archive_rows),
        archive_orders=len(
            {
                row.order_key
                for row in archive_rows
                if row.order_key and row.record_type in {"order_item", "digital_order_item"}
            }
        ),
        archive_order_items=sum(
            row.record_type in {"order_item", "digital_order_item"} for row in archive_rows
        ),
        archive_returns_refunds=sum(
            row.record_type in {"refund", "return"} for row in archive_rows
        ),
        archive_replacements=sum(row.record_type == "replacement" for row in archive_rows),
        archive_date_from=min(dated_archive).date().isoformat() if dated_archive else "",
        archive_date_to=max(dated_archive).date().isoformat() if dated_archive else "",
        scope_start_date=scope_start.isoformat(),
        scope_date_from=min(scoped_dates).date().isoformat() if scoped_dates else "",
        scope_date_to=max(scoped_dates).date().isoformat() if scoped_dates else "",
        excluded_before_scope=sum(
            value is not None and value.date() < scope_start for value in effective_dates.values()
        ),
        excluded_undated=sum(value is None for value in effective_dates.values()),
        source_rows_parsed=len(rows),
    )
    for row in rows:
        row_key = (row.source_file, row.record_type, row.row_number)
        natural = (row.record_type, row.natural_key)
        if row.content_hash in seen_content:
            status = "duplicate_within_file"
            summary.duplicate_rows_within_file += 1
        elif natural in seen_natural and seen_natural[natural].content_hash != row.content_hash:
            status = "existing_conflict"
            summary.conflicting_rows += 1
        elif natural in existing:
            if existing[natural].content_hash == row.content_hash:
                status = "existing_exact"
                summary.existing_exact_rows += 1
            else:
                status = "existing_conflict"
                summary.conflicting_rows += 1
        else:
            status = "new"
            summary.new_rows += 1
            seen_natural[natural] = row
        seen_content.add(row.content_hash)
        statuses[row_key] = status

    summary.duplicate_rows = summary.duplicate_rows_within_file + summary.existing_exact_rows
    summary.unique_source_rows = len(rows) - summary.duplicate_rows_within_file
    summary.order_items = sum(
        row.record_type in {"order_item", "digital_order_item"} for row in rows
    )
    summary.orders = len(
        {
            row.order_key
            for row in rows
            if row.order_key and row.record_type in {"order_item", "digital_order_item"}
        }
    )
    summary.returns_refunds = sum(row.record_type in {"refund", "return"} for row in rows)
    summary.gift_card_split_cases = len(
        {row.order_key for row in rows if row.order_key and row.gift_card_or_split}
    )
    matches = match_amazon_payments(db, rows)
    summary.high_confidence_payment_matches = sum(
        match.confidence == "high" for match in matches.values()
    )
    summary.medium_confidence_payment_matches = sum(
        match.confidence == "medium" for match in matches.values()
    )
    summary.unmatched = sum(match.confidence == "unmatched" for match in matches.values())
    summary.enrichment_links_would_create = sum(
        len(
            {
                (row.record_type, row.natural_key)
                for row in rows
                if row.order_key and match.group_key == f"payment:{row.order_key}"
            }
        )
        if match.confidence == "high"
        else 0
        for match in matches.values()
    )
    summary.review_items = summary.medium_confidence_payment_matches + summary.conflicting_rows
    return AmazonAnalysis(rows, summary, matches, statuses)


def preview_amazon_file(
    path: Path,
    db: Session,
    scope_start: date = DEFAULT_AMAZON_SCOPE_START,
) -> tuple[list[AmazonPreviewRow], AmazonImportSummary, dict[str, AmazonMatch]]:
    analysis = analyze_amazon_export(path, db, scope_start)
    previews = []
    for row in analysis.rows:
        group_key = (
            f"payment:{row.order_key}"
            if row.order_key and row.record_type in {"order_item", "digital_order_item"}
            else f"refund:{row.natural_key}"
        )
        match = analysis.matches.get(group_key)
        previews.append(
            AmazonPreviewRow(
                row_number=row.row_number,
                source_kind=row.record_type,
                occurred_at=row.occurred_at.strftime("%d.%m.%Y") if row.occurred_at else "–",
                safe_title=redact_text(row.product_title or row.record_type)[:120],
                amount=(
                    f"{row.amount:.2f} {row.currency or ''}" if row.amount is not None else "–"
                ),
                match_confidence=match.confidence if match else "unmatched",
                category_suggestion=row.category_suggestion or "–",
            )
        )
    return previews, analysis.summary, analysis.matches


def dry_run_amazon_file(
    path: Path,
    db: Session,
    scope_start: date = DEFAULT_AMAZON_SCOPE_START,
) -> dict[str, object]:
    analysis = analyze_amazon_export(path, db, scope_start)
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    batch = preferred_batch_for_source_hash(db, source_hash)
    return {
        **analysis.summary.as_dict(),
        "duplicate_file": bool(batch and batch.status == "imported"),
        "existing_batch_status": batch.status if batch else None,
    }


def _import_rows(
    db: Session,
    batch: ImportBatch,
    path: Path,
    *,
    scope_start: date = DEFAULT_AMAZON_SCOPE_START,
) -> int:
    analysis = analyze_amazon_export(path, db, scope_start)
    summary = analysis.summary
    existing = {
        (record.record_type, record.natural_key): record
        for record in db.scalars(select(AmazonEnrichmentRecord))
    }
    created: dict[tuple[str, str], AmazonEnrichmentRecord] = {}
    first_rows: dict[tuple[str, str], AmazonRow] = {}
    for row in analysis.rows:
        status = analysis.identity_status[(row.source_file, row.record_type, row.row_number)]
        natural = (row.record_type, row.natural_key)
        if status in {"duplicate_within_file", "existing_exact"}:
            continue
        if status == "existing_conflict":
            prior = existing.get(natural) or created.get(natural)
            prior_row = first_rows.get(natural)
            conflict_exists = db.scalar(
                select(ImportConflict.id).where(
                    ImportConflict.import_batch_id == batch.id,
                    ImportConflict.source_system == "amazon",
                    ImportConflict.natural_key == row.natural_key,
                    ImportConflict.incoming_content_hash == row.content_hash,
                )
            )
            if conflict_exists is None:
                db.add(
                    ImportConflict(
                        import_batch_id=batch.id,
                        source_system="amazon",
                        natural_key=row.natural_key,
                        incoming_content_hash=row.content_hash,
                        existing_amazon_record_id=prior.id if prior else None,
                        differing_fields_json=(
                            list(differing_field_names(prior_row.raw_values, row.raw_values))
                            if prior_row
                            else ["immutable_source_content"]
                        ),
                        status="open",
                    )
                )
            continue
        raw = RawImportRecord(
            import_batch_id=batch.id,
            source_row_key=f"{row.source_file}:{row.row_number}",
            raw_payload_json=row.raw_values,
            raw_text=row.raw_text,
            raw_hash=row.raw_hash,
        )
        db.add(raw)
        db.flush()
        record = AmazonEnrichmentRecord(
            import_batch_id=batch.id,
            raw_record_id=raw.id,
            record_type=row.record_type,
            natural_key=row.natural_key,
            content_hash=row.content_hash,
            order_key=row.order_key,
            item_key=row.item_key,
            occurred_at=row.occurred_at,
            amount=abs(row.amount) if row.amount is not None else None,
            currency=row.currency,
            product_title=row.product_title,
            asin=row.asin,
            payment_method=row.payment_method,
            metadata_json={
                "source_file": row.source_file,
                "category_suggestion": row.category_suggestion,
                "gift_card_or_split": row.gift_card_or_split,
            },
        )
        db.add(record)
        db.flush()
        created[natural] = record
        first_rows[natural] = row
        summary.new_raw_records += 1

    for natural, record in created.items():
        row = first_rows[natural]
        group_key = (
            f"payment:{row.order_key}"
            if row.order_key and row.record_type in {"order_item", "digital_order_item"}
            else f"refund:{row.natural_key}"
        )
        match = analysis.matches.get(group_key)
        if match is None or match.economic_event_id is None or match.confidence == "unmatched":
            continue
        db.add(
            AmazonPaymentMatch(
                amazon_record_id=record.id,
                economic_event_id=match.economic_event_id,
                match_type=match.match_type,
                status="linked" if match.confidence == "high" else "review",
                confidence=match.score,
                reason=match.reason,
            )
        )

    batch.metadata_json = {**(batch.metadata_json or {}), "import_summary": summary.as_dict()}
    return len(analysis.rows)


def import_amazon_batch(
    session_factory: sessionmaker[Session], batch_id: int, settings: Settings
) -> None:
    if settings.demo_mode:
        raise PrivateProfileRequiredError("Produktive Amazon-Importe sind im Demo-Profil gesperrt.")
    run_atomic_import(
        session_factory,
        batch_id,
        partial(_import_rows, scope_start=settings.amazon_import_start_date),
        settings,
    )

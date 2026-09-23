from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings
from app.db.models import (
    Account,
    BalanceConfirmation,
    Envelope,
    EnvelopeMovement,
    SourceTransaction,
)
from app.domain.envelopes import physical_balance

D = Decimal
SOURCE_TYPES = frozenset(
    {"manual_count", "bank_statement", "imported_balance", "card_statement", "payment_provider"}
)
STATUSES = frozenset({"confirmed", "provisional", "unreconciled"})
SOURCE_LABELS = {
    "manual_count": "Manuelle Zählung",
    "bank_statement": "Kontoauszug",
    "imported_balance": "Importierter Sparda-Saldo",
    "card_statement": "Kartenabrechnung",
    "payment_provider": "Zahlungsanbieter",
}
STATUS_LABELS = {
    "confirmed": "Bestätigt",
    "provisional": "Vorläufig",
    "unreconciled": "Nicht abgestimmt",
}


@dataclass(frozen=True)
class AccountBalanceView:
    account: Account
    current: BalanceConfirmation | None
    latest: BalanceConfirmation | None

    @property
    def display_balance(self) -> Decimal | None:
        if self.current is None:
            return None
        return abs(self.current.balance) if self.account.is_liability else self.current.balance


def account_balance_view(db: Session, account: Account) -> AccountBalanceView:
    history = list(
        db.scalars(
            select(BalanceConfirmation)
            .where(BalanceConfirmation.account_id == account.id)
            .order_by(BalanceConfirmation.confirmed_at.desc(), BalanceConfirmation.id.desc())
        )
    )
    current = next((row for row in history if row.status == "confirmed"), None)
    return AccountBalanceView(
        account=account, current=current, latest=history[0] if history else None
    )


def account_balance_views(
    db: Session, *, include_inactive: bool = True
) -> list[AccountBalanceView]:
    query = select(Account).order_by(Account.id)
    if not include_inactive:
        query = query.where(Account.is_active.is_(True))
    return [account_balance_view(db, account) for account in db.scalars(query)]


def confirmation_history(db: Session, account_id: int) -> list[BalanceConfirmation]:
    return list(
        db.scalars(
            select(BalanceConfirmation)
            .where(BalanceConfirmation.account_id == account_id)
            .order_by(BalanceConfirmation.confirmed_at.desc(), BalanceConfirmation.id.desc())
        )
    )


def parse_money(value: str) -> Decimal:
    cleaned = value.strip().replace(" ", "")
    normalized = cleaned.replace(".", "").replace(",", ".") if "," in cleaned else cleaned
    try:
        return D(normalized).quantize(D("0.01"))
    except InvalidOperation as exc:
        raise ValueError("Ungültiger Geldbetrag") from exc


def calculate_envelope_cash_total(db: Session) -> Decimal:
    envelopes = list(
        db.scalars(
            select(Envelope).options(selectinload(Envelope.snapshots)).where(Envelope.is_active)
        )
    )
    total = D("0")
    for envelope in envelopes:
        confirmed = [snapshot for snapshot in envelope.snapshots if snapshot.is_confirmed]
        if not confirmed:
            continue
        snapshot = max(confirmed, key=lambda row: (row.snapshot_date, row.id))
        calculated = snapshot.physical_balance
        movements = db.scalars(
            select(EnvelopeMovement).where(EnvelopeMovement.envelope_id == envelope.id)
        )
        for movement in movements:
            if movement.occurred_at.date() <= snapshot.snapshot_date:
                continue
            direction = -1 if movement.movement_type in {"expense", "transfer_out"} else 1
            calculated += direction * movement.amount
        total += physical_balance(calculated).physical
    return total


def create_balance_confirmation(
    db: Session,
    *,
    account: Account,
    confirmed_at: datetime,
    entered_balance: Decimal,
    source_type: str,
    status: str,
    source_reference: str | None = None,
    notes: str | None = None,
    envelope_cash_total: Decimal | None = None,
) -> BalanceConfirmation:
    if source_type not in SOURCE_TYPES:
        raise ValueError("Ungültige Saldoquelle")
    if status not in STATUSES:
        raise ValueError("Ungültiger Abstimmstatus")
    if account.account_type in {"cash_wallet", "cash_vault"} and entered_balance < 0:
        raise ValueError("Bargeldbestand darf nicht negativ sein")
    if account.is_liability:
        if entered_balance < 0:
            raise ValueError("Kartenverbindlichkeit als positiven Betrag eingeben")
        stored_balance = -entered_balance
    else:
        stored_balance = entered_balance

    calculated_envelopes: Decimal | None = None
    warning: str | None = None
    if account.account_type == "cash_vault":
        if envelope_cash_total is None:
            raise ValueError("Gezählter Umschlagbestand fehlt")
        if envelope_cash_total < 0:
            raise ValueError("Umschlagbestand darf nicht negativ sein")
        if envelope_cash_total > entered_balance:
            raise ValueError("Umschlagbestand darf Tresor-Gesamtbestand nicht überschreiten")
        calculated_envelopes = calculate_envelope_cash_total(db)
        if envelope_cash_total != calculated_envelopes:
            warning = "envelope_total_mismatch"
    elif envelope_cash_total is not None:
        raise ValueError("Umschlagbestand ist nur für den Tresor zulässig")

    confidence = {"confirmed": D("1"), "provisional": D("0.5"), "unreconciled": D("0")}[status]
    confirmation = BalanceConfirmation(
        account_id=account.id,
        confirmed_at=confirmed_at,
        balance=stored_balance,
        currency=account.currency,
        source_type=source_type,
        source_reference=source_reference.strip()[:255] if source_reference else None,
        confidence=confidence,
        status=status,
        notes=notes.strip() if notes else None,
        envelope_cash_total=envelope_cash_total,
        calculated_envelope_total=calculated_envelopes,
        reconciliation_warning=warning,
    )
    db.add(confirmation)
    if status == "confirmed":
        account.balance = stored_balance
        account.balance_confirmed = True
    return confirmation


def vault_free_cash(confirmation: BalanceConfirmation | None) -> Decimal | None:
    if confirmation is None or confirmation.envelope_cash_total is None:
        return None
    return confirmation.balance - confirmation.envelope_cash_total


def _latest_sparda_source(sources: list[SourceTransaction]) -> SourceTransaction | None:
    with_balance = [source for source in sources if source.balance_after is not None]
    if not with_balance:
        return None
    latest_date = max(source.booked_at for source in with_balance)
    candidates = [source for source in with_balance if source.booked_at == latest_date]
    export_descending = with_balance[0].booked_at >= with_balance[-1].booked_at
    return (
        min(candidates, key=lambda row: row.id)
        if export_descending
        else max(candidates, key=lambda row: row.id)
    )


def backfill_sparda_imported_balance(db: Session, settings: Settings) -> bool:
    if settings.demo_mode:
        raise ValueError("Sparda balance backfill requires the private profile")
    account = db.scalar(select(Account).where(Account.name == "Sparda Girokonto"))
    if account is None:
        raise ValueError("Sparda Girokonto master data is missing")
    existing = db.scalar(
        select(BalanceConfirmation.id).where(
            BalanceConfirmation.account_id == account.id,
            BalanceConfirmation.source_type == "imported_balance",
        )
    )
    if existing is not None:
        return False
    sources = list(
        db.scalars(
            select(SourceTransaction)
            .where(SourceTransaction.source_system == "sparda")
            .order_by(SourceTransaction.id)
        )
    )
    latest = _latest_sparda_source(sources)
    if latest is None or latest.balance_after is None:
        return False
    create_balance_confirmation(
        db,
        account=account,
        confirmed_at=latest.booked_at,
        entered_balance=latest.balance_after,
        source_type="imported_balance",
        status="confirmed",
        notes="Aus jüngstem enthaltenen Sparda-Buchungssaldo übernommen",
    )
    return True

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    or_,
    select,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

MONEY = Numeric(14, 2)


def utc_now() -> datetime:
    """Return a SQLite-compatible naive timestamp normalized to UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


class EconomicType(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    REFUND = "refund"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    account_type: Mapped[str] = mapped_column(String(30))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    balance: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    balance_confirmed: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_liability: Mapped[bool] = mapped_column(Boolean, default=False)
    parent_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    balance_confirmations: Mapped[list[BalanceConfirmation]] = relationship(
        back_populates="account"
    )


class BalanceConfirmation(Base):
    __tablename__ = "balance_confirmations"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('manual_count','bank_statement','imported_balance',"
            "'card_statement','payment_provider')",
            name="ck_balance_confirmation_source_type",
        ),
        CheckConstraint(
            "status IN ('confirmed','provisional','unreconciled')",
            name="ck_balance_confirmation_status",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_balance_confirmation_confidence",
        ),
        CheckConstraint(
            "envelope_cash_total IS NULL OR envelope_cash_total >= 0",
            name="ck_balance_confirmation_envelope_cash",
        ),
        CheckConstraint(
            "calculated_envelope_total IS NULL OR calculated_envelope_total >= 0",
            name="ck_balance_confirmation_calculated_envelope",
        ),
        Index("ix_balance_confirmations_account_date", "account_id", "confirmed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime)
    balance: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    source_type: Mapped[str] = mapped_column(String(30))
    source_reference: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    status: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    envelope_cash_total: Mapped[Decimal | None] = mapped_column(MONEY)
    calculated_envelope_total: Mapped[Decimal | None] = mapped_column(MONEY)
    reconciliation_warning: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    account: Mapped[Account] = relationship(back_populates="balance_confirmations")


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(30))
    filename: Mapped[str] = mapped_column(String(255))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    source_hash: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(30), default="uploaded")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    stored_filename: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    detected_mime: Mapped[str | None] = mapped_column(String(100))
    validation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=utc_now)


class BackupRecord(Base):
    __tablename__ = "backup_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), unique=True)
    checksum: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    backup_type: Mapped[str] = mapped_column(String(20), default="regular")
    status: Mapped[str] = mapped_column(String(20), default="successful")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class RawImportRecord(Base):
    __tablename__ = "raw_import_records"
    __table_args__ = (UniqueConstraint("import_batch_id", "source_row_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    source_row_key: Mapped[str] = mapped_column(String(255))
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    raw_text: Mapped[str | None] = mapped_column(Text)
    raw_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class SourceTransaction(Base):
    __tablename__ = "source_transactions"
    __table_args__ = (UniqueConstraint("source_system", "source_transaction_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    raw_record_id: Mapped[int | None] = mapped_column(ForeignKey("raw_import_records.id"))
    source_system: Mapped[str] = mapped_column(String(30))
    source_transaction_id: Mapped[str] = mapped_column(String(255))
    related_source_transaction_id: Mapped[str | None] = mapped_column(String(255))
    booked_at: Mapped[datetime] = mapped_column(DateTime)
    value_at: Mapped[datetime | None] = mapped_column(DateTime)
    merchant_raw: Mapped[str | None] = mapped_column(String(255))
    description_raw: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    source_account_hint: Mapped[str | None] = mapped_column(String(100))
    balance_after: Mapped[Decimal | None] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(30), default="booked")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    account_links: Mapped[list[SourceTransactionAccount]] = relationship(
        back_populates="source_transaction"
    )


class SourceTransactionAccount(Base):
    __tablename__ = "source_transaction_accounts"
    __table_args__ = (
        UniqueConstraint("source_transaction_id", "role"),
        CheckConstraint("role IN ('source', 'target')", name="ck_source_transaction_account_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_transaction_id: Mapped[int] = mapped_column(ForeignKey("source_transactions.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    source_transaction: Mapped[SourceTransaction] = relationship(back_populates="account_links")
    account: Mapped[Account] = relationship()


class Merchant(Base):
    __tablename__ = "merchants"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(160), unique=True)
    merchant_type: Mapped[str | None] = mapped_column(String(60))
    aliases_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    parent: Mapped[Category | None] = relationship(remote_side=[id], backref="children")


class Envelope(Base):
    __tablename__ = "envelopes"
    __table_args__ = (
        CheckConstraint("target_amount IS NULL OR target_amount >= 0", name="ck_envelopes_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    target_rule_type: Mapped[str] = mapped_column(String(40), default="monthly_contribution")
    target_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text)


class EnvelopeRulePeriod(Base):
    __tablename__ = "envelope_rule_periods"
    __table_args__ = (
        CheckConstraint("valid_to IS NULL OR valid_to >= valid_from", name="ck_rule_period_dates"),
        CheckConstraint(
            "monthly_amount IS NULL OR monthly_amount >= 0", name="ck_rule_period_monthly"
        ),
        CheckConstraint(
            "target_amount IS NULL OR target_amount >= 0", name="ck_rule_period_target"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    envelope_id: Mapped[int] = mapped_column(ForeignKey("envelopes.id"))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    monthly_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    rule_type: Mapped[str] = mapped_column(String(40))
    target_amount: Mapped[Decimal | None] = mapped_column(MONEY)
    notes: Mapped[str | None] = mapped_column(Text)
    envelope: Mapped[Envelope] = relationship(backref="rule_periods")


class EnvelopeSnapshot(Base):
    __tablename__ = "envelope_snapshots"
    __table_args__ = (
        UniqueConstraint("envelope_id", "snapshot_date"),
        CheckConstraint("physical_balance >= 0", name="ck_snapshot_physical_balance"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    envelope_id: Mapped[int] = mapped_column(ForeignKey("envelopes.id"))
    snapshot_date: Mapped[date] = mapped_column(Date)
    physical_balance: Mapped[Decimal] = mapped_column(MONEY)
    source: Mapped[str] = mapped_column(String(50), default="manual")
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)
    envelope: Mapped[Envelope] = relationship(backref="snapshots")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(140), unique=True)
    starts_at: Mapped[date | None] = mapped_column(Date)
    ends_at: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(30), default="active")
    notes: Mapped[str | None] = mapped_column(Text)


class EconomicEvent(Base):
    __tablename__ = "economic_events"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('expense', 'income', 'transfer', 'refund')",
            name="ck_economic_event_type",
        ),
        CheckConstraint("amount >= 0", name="ck_economic_event_amount"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_economic_event_confidence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(20))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id"))
    description: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    source_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    target_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    envelope_id: Mapped[int | None] = mapped_column(ForeignKey("envelopes.id"))
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    status: Mapped[str] = mapped_column(String(30), default="booked")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    account: Mapped[Account | None] = relationship(foreign_keys=[account_id])
    source_account: Mapped[Account | None] = relationship(foreign_keys=[source_account_id])
    target_account: Mapped[Account | None] = relationship(foreign_keys=[target_account_id])
    category: Mapped[Category | None] = relationship()
    envelope: Mapped[Envelope | None] = relationship()
    project: Mapped[Project | None] = relationship()


class EventSourceLink(Base):
    __tablename__ = "event_source_links"
    __table_args__ = (
        UniqueConstraint("economic_event_id", "source_transaction_id", "link_type"),
        CheckConstraint(
            "link_type IN ('canonical_source', 'funding_leg', 'settlement_leg', "
            "'enrichment', 'refund_origin', 'duplicate_source', 'authorization', "
            "'third_party_payment')",
            name="ck_event_source_link_type",
        ),
        Index(
            "uq_event_source_links_canonical_source",
            "source_transaction_id",
            unique=True,
            sqlite_where=text("link_type = 'canonical_source'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    economic_event_id: Mapped[int] = mapped_column(ForeignKey("economic_events.id"))
    source_transaction_id: Mapped[int] = mapped_column(ForeignKey("source_transactions.id"))
    link_type: Mapped[str] = mapped_column(String(40))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    notes: Mapped[str | None] = mapped_column(Text)


class EnvelopeMovement(Base):
    __tablename__ = "envelope_movements"
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_envelope_movement_amount"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    envelope_id: Mapped[int] = mapped_column(ForeignKey("envelopes.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    movement_type: Mapped[str] = mapped_column(String(40))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    economic_event_id: Mapped[int | None] = mapped_column(ForeignKey("economic_events.id"))
    source_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    target_account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    notes: Mapped[str | None] = mapped_column(Text)


class RecurringItem(Base):
    __tablename__ = "recurring_items"
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_recurring_item_amount"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(140))
    direction: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    frequency: Mapped[str] = mapped_column(String(30))
    next_due_at: Mapped[date] = mapped_column(Date)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    envelope_id: Mapped[int | None] = mapped_column(ForeignKey("envelopes.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_review_confidence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    economic_event_id: Mapped[int | None] = mapped_column(ForeignKey("economic_events.id"))
    source_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("source_transactions.id"))
    review_type: Mapped[str] = mapped_column(String(40))
    proposed_category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    proposed_envelope_id: Mapped[int | None] = mapped_column(ForeignKey("envelopes.id"))
    proposed_project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"))
    proposed_event_type: Mapped[str | None] = mapped_column(String(20))
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    explanation: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_notes: Mapped[str | None] = mapped_column(Text)
    economic_event: Mapped[EconomicEvent | None] = relationship()
    source_transaction: Mapped[SourceTransaction | None] = relationship()
    proposed_category: Mapped[Category | None] = relationship(foreign_keys=[proposed_category_id])
    proposed_envelope: Mapped[Envelope | None] = relationship(foreign_keys=[proposed_envelope_id])


class AssignmentRule(Base):
    __tablename__ = "assignment_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_type: Mapped[str] = mapped_column(String(40))
    priority: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    condition_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    action_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date)
    created_from_review_id: Mapped[int | None] = mapped_column(ForeignKey("review_items.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_date: Mapped[date] = mapped_column(Date)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    vault_total: Mapped[Decimal | None] = mapped_column(MONEY)
    envelope_total: Mapped[Decimal | None] = mapped_column(MONEY)
    free_vault_cash_before: Mapped[Decimal | None] = mapped_column(MONEY)
    deposit_total: Mapped[Decimal | None] = mapped_column(MONEY)
    withdraw_total: Mapped[Decimal | None] = mapped_column(MONEY)
    transfer_money: Mapped[Decimal | None] = mapped_column(MONEY)
    free_vault_cash_used: Mapped[Decimal | None] = mapped_column(MONEY)
    bank_withdrawal_needed: Mapped[Decimal | None] = mapped_column(MONEY)
    free_vault_cash_after: Mapped[Decimal | None] = mapped_column(MONEY)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ForecastEntry(Base):
    __tablename__ = "forecast_entries"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_forecast_amount"),
        CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name="ck_forecast_probability",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    forecast_date: Mapped[date] = mapped_column(Date)
    source_type: Mapped[str] = mapped_column(String(30))
    source_id: Mapped[int | None] = mapped_column(Integer)
    direction: Mapped[str] = mapped_column(String(20))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"))
    probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    notes: Mapped[str | None] = mapped_column(Text)


class ImmutableRecordError(ValueError):
    """Raised when append-only financial source/history data is mutated."""


def _reject_mutation(_mapper: object, _connection: object, target: object) -> None:
    raise ImmutableRecordError(f"{type(target).__name__} records are append-only")


def _validate_rule_period(_mapper: object, connection: object, target: EnvelopeRulePeriod) -> None:
    if target.valid_to is not None and target.valid_to < target.valid_from:
        raise ValueError("Envelope rule valid_to must not be before valid_from")

    table = EnvelopeRulePeriod.__table__
    end = target.valid_to or date.max
    overlap = connection.execute(
        select(table.c.id)
        .where(
            table.c.envelope_id == target.envelope_id,
            table.c.rule_type == target.rule_type,
            table.c.valid_from <= end,
            or_(table.c.valid_to.is_(None), table.c.valid_to >= target.valid_from),
        )
        .limit(1)
    ).first()
    if overlap is not None:
        raise ValueError("Envelope rule periods must not overlap")


for _immutable_model in (
    RawImportRecord,
    SourceTransaction,
    EnvelopeRulePeriod,
    BalanceConfirmation,
):
    event.listen(_immutable_model, "before_update", _reject_mutation)
    event.listen(_immutable_model, "before_delete", _reject_mutation)

event.listen(EnvelopeRulePeriod, "before_insert", _validate_rule_period)

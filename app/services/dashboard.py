from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import EconomicEvent, RecurringItem
from app.services.balance_confirmations import account_balance_views, vault_free_cash
from app.services.envelope_targets import calculate_envelope_targets
from app.services.reviews import actionable_review_count
from app.services.transaction_details import event_transaction_views


def build_dashboard(session: Session, *, calculation_date: date | None = None) -> dict[str, object]:
    today = calculation_date or date.today()
    account_rows = account_balance_views(session, include_inactive=False)
    confirmed_rows = [row for row in account_rows if row.current is not None]

    asset_total = sum(
        (row.current.balance for row in confirmed_rows if not row.account.is_liability),
        Decimal(),
    )
    liability_total = sum(
        (abs(row.current.balance) for row in confirmed_rows if row.account.is_liability),
        Decimal(),
    )
    wealth = asset_total - liability_total

    vault_rows = [row for row in account_rows if row.account.account_type == "cash_vault"]
    vault_balance_confirmed = bool(vault_rows) and all(
        row.current is not None for row in vault_rows
    )
    free_vault = sum(
        (max(vault_free_cash(row.current) or Decimal(), Decimal()) for row in vault_rows),
        Decimal(),
    )

    envelope_targets = calculate_envelope_targets(
        session,
        calculation_date=today,
        free_vault_cash=free_vault,
    )
    envelope_rows = envelope_targets.rows
    reserved = sum((row.actual for row in envelope_rows), Decimal())

    liquid_types = {"checking", "paypal", "cash_wallet"}
    liquidity_rows = [
        row
        for row in account_rows
        if (not row.account.is_liability and row.account.account_type in liquid_types)
        or row.account.account_type == "cash_vault"
        or row.account.is_liability
    ]
    liquidity_complete = all(row.current is not None for row in liquidity_rows)
    liquid_assets = sum(
        (
            row.current.balance
            for row in confirmed_rows
            if not row.account.is_liability and row.account.account_type in liquid_types
        ),
        Decimal(),
    )
    free_available = liquid_assets + free_vault - liability_total if liquidity_complete else None

    month_start = today.replace(day=1)
    next_month = (
        date(today.year + 1, 1, 1) if today.month == 12 else date(today.year, today.month + 1, 1)
    )

    def month_total(event_type: str) -> Decimal:
        return (
            session.scalar(
                select(func.coalesce(func.sum(EconomicEvent.amount), 0)).where(
                    EconomicEvent.event_type == event_type,
                    EconomicEvent.occurred_at >= month_start,
                    EconomicEvent.occurred_at < next_month,
                    EconomicEvent.status.in_(("booked", "confirmed")),
                )
            )
            or Decimal()
        )

    income = month_total("income")
    expenses = month_total("expense")
    refunds = month_total("refund")
    cashflow = income + refunds - expenses
    open_reviews = actionable_review_count(session)

    recent_events = list(
        session.scalars(
            select(EconomicEvent)
            .options(
                selectinload(EconomicEvent.account),
                selectinload(EconomicEvent.source_account),
                selectinload(EconomicEvent.target_account),
                selectinload(EconomicEvent.category),
                selectinload(EconomicEvent.envelope),
                selectinload(EconomicEvent.project),
            )
            .where(EconomicEvent.status.in_(("booked", "confirmed")))
            .order_by(EconomicEvent.occurred_at.desc())
            .limit(6)
        )
    )
    recurring = list(
        session.scalars(
            select(RecurringItem)
            .where(RecurringItem.active)
            .order_by(RecurringItem.next_due_at)
            .limit(5)
        )
    )
    return {
        "accounts": account_rows,
        "wealth": wealth,
        "wealth_has_unknown": any(row.current is None for row in account_rows),
        "unreconciled_active_count": sum(row.current is None for row in account_rows),
        "reserved": reserved,
        "free_available": free_available,
        "card_liabilities": liability_total
        if all(row.current is not None for row in account_rows if row.account.is_liability)
        else None,
        "income": income,
        "expenses": expenses,
        "refunds": refunds,
        "cashflow": cashflow,
        "open_reviews": open_reviews,
        "envelopes": envelope_rows,
        "free_vault": free_vault,
        "vault_balance_confirmed": vault_balance_confirmed,
        "reconciliation": envelope_targets.reconciliation,
        "envelope_targets": envelope_targets,
        "recent_transaction_views": event_transaction_views(session, recent_events),
        "recurring": recurring,
    }

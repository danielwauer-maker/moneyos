from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import EconomicEvent, RecurringItem
from app.services.balance_confirmations import account_balance_views, vault_free_cash
from app.services.envelope_targets import calculate_envelope_targets
from app.services.reviews import actionable_open_review_count
from app.services.transaction_details import event_transaction_views


def build_dashboard(session: Session) -> dict[str, object]:
    account_rows = account_balance_views(session, include_inactive=False)

    known_balances = [
        row.current.balance for row in account_rows if row.current is not None
    ]
    wealth = sum(known_balances, Decimal())
    wealth_has_unknown = any(row.current is None for row in account_rows)

    def balance_for_type(account_type: str) -> Decimal | None:
        rows = [row for row in account_rows if row.account.account_type == account_type]
        if not rows or any(row.current is None for row in rows):
            return None
        return sum((row.current.balance for row in rows if row.current), Decimal())

    checking_balance = balance_for_type("checking")
    liability_rows = [row for row in account_rows if row.account.is_liability]
    card_liabilities = (
        None
        if any(row.current is None for row in liability_rows)
        else sum(
            (abs(row.current.balance) for row in liability_rows if row.current),
            Decimal(),
        )
    )

    vault = next(
        (row for row in account_rows if row.account.account_type == "cash_vault"),
        None,
    )
    vault_balance_confirmed = bool(vault and vault.current)
    confirmed_free_vault = vault_free_cash(vault.current) if vault else None
    free_vault = max(confirmed_free_vault or Decimal(), Decimal())

    envelope_targets = calculate_envelope_targets(
        session,
        calculation_date=date.today(),
        free_vault_cash=free_vault,
    )
    envelope_rows = envelope_targets.rows
    reserved = sum((row.actual for row in envelope_rows), Decimal())

    month_start = datetime.combine(date.today().replace(day=1), time.min)
    income = session.scalar(
        select(func.coalesce(func.sum(EconomicEvent.amount), 0)).where(
            EconomicEvent.event_type == "income",
            EconomicEvent.occurred_at >= month_start,
        )
    )
    expenses = session.scalar(
        select(func.coalesce(func.sum(EconomicEvent.amount), 0)).where(
            EconomicEvent.event_type == "expense",
            EconomicEvent.occurred_at >= month_start,
        )
    )
    refunds = session.scalar(
        select(func.coalesce(func.sum(EconomicEvent.amount), 0)).where(
            EconomicEvent.event_type == "refund",
            EconomicEvent.occurred_at >= month_start,
        )
    )
    open_reviews = actionable_open_review_count(session)

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
        "wealth_has_unknown": wealth_has_unknown,
        "unreconciled_active_count": sum(row.current is None for row in account_rows),
        "reserved": reserved,
        # A confirmed current-account balance already contains all booked movements.
        # Subtracting the month's expenses again would double count them.
        "free_available": checking_balance,
        "card_liabilities": card_liabilities,
        "income": income,
        "expenses": expenses,
        "refunds": refunds,
        "cashflow": income + refunds - expenses,
        "open_reviews": open_reviews,
        "envelopes": envelope_rows,
        "free_vault": free_vault,
        "vault_balance_confirmed": vault_balance_confirmed,
        "reconciliation": envelope_targets.reconciliation,
        "envelope_targets": envelope_targets,
        "recent_transaction_views": event_transaction_views(session, recent_events),
        "recurring": recurring,
    }

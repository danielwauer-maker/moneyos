from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    EconomicEvent,
    RecurringItem,
    ReviewItem,
)
from app.domain.accounting import WealthInputs, total_wealth
from app.services.balance_confirmations import account_balance_views, vault_free_cash
from app.services.envelope_targets import calculate_envelope_targets


def build_dashboard(session: Session) -> dict[str, object]:
    account_rows = account_balance_views(session, include_inactive=False)
    by_type = {row.account.account_type: row for row in account_rows}

    def confirmed_balance(account_type: str) -> Decimal:
        row = by_type.get(account_type)
        return row.current.balance if row and row.current else Decimal()

    wealth = total_wealth(
        WealthInputs(
            checking=confirmed_balance("checking"),
            paypal=confirmed_balance("paypal"),
            wallet=confirmed_balance("cash_wallet"),
            vault=confirmed_balance("cash_vault"),
            credit_card_liabilities=(abs(confirmed_balance("credit_card"))),
        )
    )
    vault = by_type.get("cash_vault")
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
    month_start = date.today().replace(day=1)
    income = session.scalar(
        select(func.coalesce(func.sum(EconomicEvent.amount), 0)).where(
            EconomicEvent.event_type == "income", EconomicEvent.occurred_at >= month_start
        )
    )
    expenses = session.scalar(
        select(func.coalesce(func.sum(EconomicEvent.amount), 0)).where(
            EconomicEvent.event_type == "expense", EconomicEvent.occurred_at >= month_start
        )
    )
    open_reviews = session.scalar(
        select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "open")
    )
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
        "wealth_has_unknown": any(row.current is None for row in account_rows),
        "unreconciled_active_count": sum(row.current is None for row in account_rows),
        "reserved": reserved,
        "free_available": (
            by_type["checking"].current.balance - expenses
            if by_type.get("checking") and by_type["checking"].current
            else None
        ),
        "card_liabilities": (
            abs(by_type["credit_card"].current.balance)
            if by_type.get("credit_card") and by_type["credit_card"].current
            else None
        ),
        "income": income,
        "expenses": expenses,
        "cashflow": income - expenses,
        "open_reviews": open_reviews,
        "envelopes": envelope_rows,
        "free_vault": free_vault,
        "vault_balance_confirmed": vault_balance_confirmed,
        "reconciliation": envelope_targets.reconciliation,
        "envelope_targets": envelope_targets,
        "recent_events": recent_events,
        "recurring": recurring,
    }

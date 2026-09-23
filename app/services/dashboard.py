from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Account,
    EconomicEvent,
    Envelope,
    EnvelopeMovement,
    RecurringItem,
    ReviewItem,
)
from app.domain.accounting import WealthInputs, total_wealth
from app.domain.envelopes import physical_balance
from app.domain.reconciliation import reconcile_envelopes


def build_dashboard(session: Session) -> dict[str, object]:
    accounts = list(session.scalars(select(Account).where(Account.is_active).order_by(Account.id)))
    by_type = {account.account_type: account for account in accounts}

    def confirmed_balance(account_type: str) -> Decimal:
        account = by_type.get(account_type)
        return account.balance if account and account.balance_confirmed else Decimal()

    wealth = total_wealth(
        WealthInputs(
            checking=confirmed_balance("checking"),
            paypal=confirmed_balance("paypal"),
            wallet=confirmed_balance("cash_wallet"),
            vault=confirmed_balance("cash_vault"),
            credit_card_liabilities=(abs(confirmed_balance("credit_card"))),
        )
    )
    envelopes = list(
        session.scalars(
            select(Envelope).options(selectinload(Envelope.snapshots)).order_by(Envelope.sort_order)
        )
    )
    envelope_rows = []
    for envelope in envelopes:
        snapshot = max(envelope.snapshots, key=lambda row: row.snapshot_date)
        movements = list(
            session.scalars(
                select(EnvelopeMovement).where(
                    EnvelopeMovement.envelope_id == envelope.id,
                    EnvelopeMovement.occurred_at > snapshot.snapshot_date,
                )
            )
        )
        calculated = snapshot.physical_balance
        for movement in movements:
            direction = -1 if movement.movement_type in {"expense", "transfer_out"} else 1
            calculated += direction * movement.amount
        target_info = physical_balance(calculated)
        if envelope.target_rule_type == "target_balance" and envelope.target_amount is not None:
            actual = target_info.physical
            target = physical_balance(envelope.target_amount).physical
        else:
            actual = snapshot.physical_balance
            target = target_info.physical
        envelope_rows.append(
            {
                "envelope": envelope,
                "actual": actual,
                "target": target,
                "delta": target - actual,
                "deficit": target_info.deficit,
            }
        )
    reserved = sum((row["actual"] for row in envelope_rows), Decimal())
    vault = by_type.get("cash_vault")
    vault_balance_confirmed = bool(vault and vault.balance_confirmed)
    free_vault = max(confirmed_balance("cash_vault") - reserved, Decimal())
    reconciliation = reconcile_envelopes([row["delta"] for row in envelope_rows], free_vault)
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
        "accounts": accounts,
        "wealth": wealth,
        "wealth_has_unknown": any(not account.balance_confirmed for account in accounts),
        "reserved": reserved,
        "free_available": (
            by_type["checking"].balance - expenses
            if by_type.get("checking") and by_type["checking"].balance_confirmed
            else None
        ),
        "card_liabilities": (
            abs(by_type["credit_card"].balance)
            if by_type.get("credit_card") and by_type["credit_card"].balance_confirmed
            else None
        ),
        "income": income,
        "expenses": expenses,
        "cashflow": income - expenses,
        "open_reviews": open_reviews,
        "envelopes": envelope_rows,
        "free_vault": free_vault,
        "vault_balance_confirmed": vault_balance_confirmed,
        "reconciliation": reconciliation,
        "recent_events": recent_events,
        "recurring": recurring,
    }

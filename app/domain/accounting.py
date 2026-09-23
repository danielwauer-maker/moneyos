from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class AccountMovementKind(StrEnum):
    PURCHASE = "purchase"
    ORDINARY_EXPENSE = "ordinary_expense"
    INCOME = "income"
    OWN_ACCOUNT_TRANSFER = "own_account_transfer"
    CARD_SETTLEMENT = "card_settlement"
    CASH_WITHDRAWAL = "cash_withdrawal"
    REFUND = "refund"


MOVEMENT_EVENT_TYPES = {
    AccountMovementKind.PURCHASE: "expense",
    AccountMovementKind.ORDINARY_EXPENSE: "expense",
    AccountMovementKind.INCOME: "income",
    AccountMovementKind.OWN_ACCOUNT_TRANSFER: "transfer",
    AccountMovementKind.CARD_SETTLEMENT: "transfer",
    AccountMovementKind.CASH_WITHDRAWAL: "transfer",
    AccountMovementKind.REFUND: "refund",
}


@dataclass(frozen=True)
class WealthInputs:
    checking: Decimal
    paypal: Decimal
    wallet: Decimal
    vault: Decimal
    credit_card_liabilities: Decimal


def total_wealth(values: WealthInputs) -> Decimal:
    """Vault already contains envelope cash; envelopes are deliberately absent."""
    return (
        values.checking
        + values.paypal
        + values.wallet
        + values.vault
        - values.credit_card_liabilities
    )


def counts_as_expense(event_type: str) -> bool:
    return event_type == "expense"


def classify_account_movement(kind: AccountMovementKind) -> str:
    """Canonicalize account movements before creating an economic event."""
    return MOVEMENT_EVENT_TYPES[kind]


def economic_expense_total(events: list[tuple[str, Decimal]]) -> Decimal:
    return sum(
        (amount for event_type, amount in events if counts_as_expense(event_type)), Decimal()
    )

from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(frozen=True)
class ReconciliationResult:
    deposit_total: Decimal
    withdraw_total: Decimal
    transfer_money: Decimal
    remaining_need: Decimal
    free_vault_cash_used: Decimal
    bank_withdrawal_needed: Decimal
    free_vault_cash_after: Decimal


def reconcile_envelopes(deltas: list[Decimal], free_vault_cash: Decimal) -> ReconciliationResult:
    deposit_total = sum((max(delta, ZERO) for delta in deltas), start=ZERO)
    withdraw_total = sum((max(-delta, ZERO) for delta in deltas), start=ZERO)
    transfer_money = min(deposit_total, withdraw_total)
    remaining_need = deposit_total - transfer_money
    free_vault_cash_used = min(remaining_need, max(free_vault_cash, ZERO))
    bank_withdrawal_needed = max(remaining_need - free_vault_cash, ZERO)
    free_vault_cash_after = max(free_vault_cash - remaining_need, ZERO)
    return ReconciliationResult(
        deposit_total,
        withdraw_total,
        transfer_money,
        remaining_need,
        free_vault_cash_used,
        bank_withdrawal_needed,
        free_vault_cash_after,
    )

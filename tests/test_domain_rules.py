from decimal import Decimal

from app.domain.accounting import (
    AccountMovementKind,
    WealthInputs,
    classify_account_movement,
    economic_expense_total,
    total_wealth,
)
from app.domain.envelopes import dentist_refill, physical_balance
from app.domain.paypal import classify_paypal_type
from app.domain.reconciliation import reconcile_envelopes

D = Decimal


def test_five_euro_rounding_examples() -> None:
    assert physical_balance(D("273.96")).physical == D("270")
    assert physical_balance(D("250.00")).physical == D("250")
    assert physical_balance(D("2.49")).physical == D("0")
    negative = physical_balance(D("-17.30"))
    assert negative.physical == D("0")
    assert negative.deficit == D("17.30")


def test_envelope_reconciliation_uses_transfers_then_vault_cash() -> None:
    result = reconcile_envelopes([D("205"), D("-25")], D("315.54"))
    assert result.deposit_total == D("205")
    assert result.withdraw_total == D("25")
    assert result.transfer_money == D("25")
    assert result.remaining_need == D("180")
    assert result.free_vault_cash_used == D("180")
    assert result.bank_withdrawal_needed == D("0")
    assert result.free_vault_cash_after == D("135.54")


def test_envelope_reconciliation_can_require_bank_withdrawal() -> None:
    result = reconcile_envelopes([D("250"), D("-20")], D("50"))
    assert result.transfer_money == D("20")
    assert result.free_vault_cash_used == D("50")
    assert result.bank_withdrawal_needed == D("180")
    assert result.free_vault_cash_after == D("0")


def test_dentist_refill_returns_target_to_500() -> None:
    after_expense = D("500") - D("120")
    refill = dentist_refill(after_expense)
    assert refill == D("120")
    assert after_expense + refill == D("500")


def test_vault_envelopes_are_not_double_counted() -> None:
    result = total_wealth(WealthInputs(D("1000"), D("100"), D("50"), D("600"), D("200")))
    assert result == D("1550")


def test_card_purchase_and_settlement_count_one_expense() -> None:
    events = [("expense", D("75")), ("transfer", D("75"))]
    assert economic_expense_total(events) == D("75")


def test_owned_account_movements_and_card_settlements_are_transfers() -> None:
    assert classify_account_movement(AccountMovementKind.PURCHASE) == "expense"
    assert classify_account_movement(AccountMovementKind.CARD_SETTLEMENT) == "transfer"
    assert classify_account_movement(AccountMovementKind.OWN_ACCOUNT_TRANSFER) == "transfer"
    assert classify_account_movement(AccountMovementKind.CASH_WITHDRAWAL) == "transfer"


def test_paypal_technical_rows_and_refunds() -> None:
    assert classify_paypal_type("Allgemeine Autorisierung") is None
    assert classify_paypal_type("Bankgutschrift auf PayPal-Konto") is None
    assert classify_paypal_type("Rückzahlung") == "refund"
    assert classify_paypal_type("Zahlung") == "expense"
    assert classify_paypal_type("Unbekannter technischer Typ") == "review"

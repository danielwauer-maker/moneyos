from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

FIVE = Decimal("5")
ZERO = Decimal("0")


@dataclass(frozen=True)
class PhysicalBalance:
    calculated: Decimal
    physical: Decimal
    deficit: Decimal


def physical_balance(calculated: Decimal) -> PhysicalBalance:
    """Round physical envelope cash down to €5 and expose deficits separately."""
    deficit = max(-calculated, ZERO)
    clamped = max(calculated, ZERO)
    rounded = (clamped / FIVE).to_integral_value(rounding=ROUND_FLOOR) * FIVE
    return PhysicalBalance(calculated=calculated, physical=rounded, deficit=deficit)


def dentist_refill(current_balance: Decimal, target: Decimal = Decimal("500")) -> Decimal:
    return max(target - current_balance, ZERO)

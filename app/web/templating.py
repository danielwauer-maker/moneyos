from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def euro(value: object) -> str:
    amount = value if isinstance(value, Decimal) else Decimal(str(value or 0))
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


templates.env.filters["euro"] = euro

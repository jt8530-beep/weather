from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


FEE_PRECISION = Decimal("0.00001")


def taker_fee(shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Polymarket taker fee estimate: C * feeRate * p * (1-p), rounded to 5 decimals."""
    raw = shares * fee_rate * price * (Decimal("1") - price)
    if raw <= 0:
        return Decimal("0")
    rounded = raw.quantize(FEE_PRECISION, rounding=ROUND_HALF_UP)
    return rounded if rounded >= FEE_PRECISION else Decimal("0")

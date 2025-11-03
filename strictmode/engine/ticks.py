from __future__ import annotations

from math import floor
from decimal import Decimal, getcontext, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING

__all__ = ["hk_tick", "round_to_increment"]


def hk_tick(price: float) -> float:
    p = float(price)
    if p <= 0:
        return 0.1
    if p < 0.25:
        return 0.001
    if p < 0.5:
        return 0.005
    if p < 10:
        return 0.01
    if p < 20:
        return 0.02
    if p < 100:
        return 0.05
    if p < 200:
        return 0.10
    if p < 500:
        return 0.20
    if p < 1000:
        return 0.50
    if p < 2000:
        return 1.00
    if p < 5000:
        return 2.00
    return 5.00


def round_to_increment(price: float, inc: float, mode: str = "nearest") -> float:
    if inc <= 0:
        return float(price)
    # Use Decimal to avoid banker rounding / FP artifacts
    getcontext().prec = 28
    p = Decimal(str(price))
    i = Decimal(str(inc))
    steps = p / i
    if mode == "down":
        steps_q = steps.to_integral_value(rounding=ROUND_FLOOR)
    elif mode == "up":
        steps_q = steps.to_integral_value(rounding=ROUND_CEILING)
    else:
        steps_q = steps.to_integral_value(rounding=ROUND_HALF_UP)
    return float((steps_q * i).quantize(i))

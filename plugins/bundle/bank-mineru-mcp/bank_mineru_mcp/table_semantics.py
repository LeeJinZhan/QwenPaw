"""Shared table value rules used by fixed queries and controlled Python.

Source facts are never coerced. Numeric interpretation applies only to an
explicit calculation, and exact decimal results accompany JSON numbers.
"""
from decimal import Decimal, InvalidOperation, localcontext
import math
import re

ENGINE_VERSION = "table-facts-3"
DECIMAL_PRECISION = 128


def numeric_value(value, *, numeric_text="strict"):
    if value is None or value == "" or isinstance(value, bool):
        return None
    text = str(value)
    if isinstance(value, str) and "," in text:
        if numeric_text != "thousands" or not re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
            return None
        text = text.replace(",", "")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or len(number.as_tuple().digits) > 120 or abs(number.adjusted()) > 120:
        return None
    return number


def decimal_result(number):
    """Exact text is authoritative when a JSON float cannot carry precision."""
    exact = format(number, "f")
    if number == number.to_integral_value():
        return int(number), exact
    value = float(number)
    return (value if math.isfinite(value) else exact), exact


def decimal_add(left, right):
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return left + right


def decimal_average(total, count):
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return total / count

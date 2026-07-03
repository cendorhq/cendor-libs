"""Money arithmetic must be exact (Decimal) and never use float. See write-tests skill."""

from decimal import Decimal

import pytest
from cendor.core.types import Money, Usage


def test_money_coerces_to_decimal_without_float_noise():
    # 0.1 + 0.2 is the classic float trap; Money must stay exact.
    assert (Money(0.1) + Money(0.2)).amount == Decimal("0.3")
    assert Money(0.1).amount == Decimal("0.1")
    assert Money("0.0000025").amount == Decimal("0.0000025")


def test_money_arithmetic():
    assert (Money(Decimal("0.01")) + Money(Decimal("0.02"))).amount == Decimal("0.03")
    assert (Money(Decimal("0.05")) - Money(Decimal("0.02"))).amount == Decimal("0.03")
    assert (Money(Decimal("0.001")) * 5).amount == Decimal("0.005")
    assert (3 * Money(Decimal("0.002"))).amount == Decimal("0.006")


def test_money_sum_starts_at_zero():
    total = sum([Money(Decimal("0.01")), Money(Decimal("0.02")), Money(Decimal("0.03"))])
    assert total == Money(Decimal("0.06"))


def test_money_comparisons():
    assert Money(Decimal("1")) < Money(Decimal("2"))
    assert Money(Decimal("2")) >= Money(Decimal("2"))
    assert Money.zero() == Money(Decimal("0"))


def test_money_currency_mismatch_raises():
    with pytest.raises(ValueError):
        Money(Decimal("1"), "USD") + Money(Decimal("1"), "EUR")
    with pytest.raises(ValueError):
        _ = Money(Decimal("1"), "USD") < Money(Decimal("1"), "EUR")


def test_usage_total():
    assert Usage(input_tokens=100, output_tokens=50).total_tokens == 150

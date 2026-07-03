"""Property tests (Layer E): invariants that must hold for arbitrary inputs. No network."""

from decimal import Decimal

from cendor.core import prices
from cendor.core.types import Money
from hypothesis import given
from hypothesis import strategies as st

_money = st.decimals(min_value=0, max_value=10**9, allow_nan=False, allow_infinity=False, places=8)


@given(a=_money, b=_money)
def test_money_add_sub_roundtrips_exactly(a, b):
    # Decimal-backed money never loses precision: (a + b) - b == a, exactly.
    assert (Money(a) + Money(b) - Money(b)).amount == Money(a).amount


@given(lo=st.integers(0, 10**6), hi=st.integers(0, 10**6), out=st.integers(0, 10**6))
def test_estimate_is_monotonic_in_input_tokens(lo, hi, out):
    lo, hi = sorted((lo, hi))
    cheaper = prices.estimate("gpt-4o", lo, out)
    dearer = prices.estimate("gpt-4o", hi, out)
    assert dearer.amount >= cheaper.amount
    assert isinstance(cheaper.amount, Decimal)

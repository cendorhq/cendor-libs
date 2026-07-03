"""Property test (Layer E): compression is always exactly reversible, for any input. No network."""

from cendor.core import tokens
from cendor.squeeze import compress
from hypothesis import given
from hypothesis import strategies as st


@given(s=st.text(max_size=2000))
def test_compress_then_expand_round_trips(s):
    # Whatever the content (and however hard it's squeezed), the original restores byte-for-byte.
    _small, handle = compress(s, kind="auto")
    assert handle.expand() == s


@given(
    s=st.text(min_size=1, max_size=2000),
    kind=st.sampled_from(["json", "logs", "code", "prose"]),
    target=st.integers(1, 200),
)
def test_target_tokens_is_never_exceeded(s, kind, target):
    # "compress to a budget, never exceeds it" — for every content kind, including prose.
    small, _handle = compress(s, kind=kind, target_tokens=target)
    assert tokens.count(small, "gpt-4o") <= target


@given(
    s=st.text(min_size=1, max_size=2000), kind=st.sampled_from(["json", "logs", "code", "prose"])
)
def test_reversible_for_every_kind(s, kind):
    _small, handle = compress(s, kind=kind)
    assert handle.expand() == s

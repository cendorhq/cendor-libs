"""``on_missing_compressor`` — how loud a silently-truncated ``compress`` block is (Q5).

A block declaring ``evict="compress"`` with no compressor available is **truncated** instead. That
is a different operation: truncation discards content and is not reversible, while a squeeze
compression hands back a ``Handle`` you can ``.expand()``. The substitution has always been recorded
as a note on the block's ``BlockDecision`` — but a note lives inside the ``AssemblyReport`` and
nothing obliges a caller to read one, so a forgotten ``contextkit[squeeze]`` extra quietly degraded
every compress block in production while the assembly still reported success.

The knob is additive and **the default is unchanged** (``"note"``), which the first test pins.
"""

import warnings

import pytest
from cendor.contextkit import (
    Block,
    Context,
    MissingCompressorError,
    MissingCompressorWarning,
    use_compressor,
)

LONG = "alpha bravo charlie delta echo foxtrot golf hotel india juliet " * 40


@pytest.fixture(autouse=True)
def no_compressor():
    """Remove any process-wide compressor for the duration of the test, and restore it after.

    ``squeeze`` IS installed in this workspace, so the auto-discovery in ``_get_compressor`` would
    otherwise find it and none of these paths would be reachable.
    """
    previous = use_compressor(None)
    import cendor.contextkit as ck

    real_get = ck.Context._get_compressor
    ck.Context._get_compressor = lambda self: self._compressor  # type: ignore[method-assign]
    yield
    ck.Context._get_compressor = real_get  # type: ignore[method-assign]
    use_compressor(previous)


def _ctx(**kw) -> Context:
    ctx = Context(budget_tokens=120, model="gpt-4o", **kw)
    ctx.add(Block("keep me", priority=10, pin=True, role="system"))
    ctx.add(Block(LONG, priority=1, evict="compress", role="user"))
    return ctx


def test_default_is_note_and_nothing_changes():
    """The historical behaviour: truncate, record a note, do not warn and do not raise."""
    ctx = _ctx()
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning at all would fail this test
        messages = ctx.assemble()
    assert messages
    decisions = [d for d in ctx.report().decisions if d.role == "user"]
    assert decisions[0].action == "truncated"
    assert "squeeze not installed" in decisions[0].note
    assert decisions[0].handle is None  # truncation is not reversible


def test_warn_mode_emits_a_typed_warning_and_still_assembles():
    ctx = _ctx(on_missing_compressor="warn")
    with pytest.warns(MissingCompressorWarning, match="TRUNCATED"):
        messages = ctx.assemble()
    assert messages  # the assembly still succeeds — warn is not a refusal
    assert [d for d in ctx.report().decisions if d.role == "user"][0].action == "truncated"


def test_error_mode_refuses_instead_of_truncating():
    ctx = _ctx(on_missing_compressor="error")
    with pytest.raises(MissingCompressorError, match="contextkit\\[squeeze\\]"):
        ctx.assemble()


def test_error_mode_message_names_every_way_out():
    ctx = _ctx(on_missing_compressor="error")
    with pytest.raises(MissingCompressorError) as exc:
        ctx.assemble()
    text = str(exc.value)
    for remedy in ("contextkit[squeeze]", "compressor=", "use_compressor", "on_missing_compressor"):
        assert remedy in text


def test_an_invalid_mode_is_rejected_at_construction():
    with pytest.raises(ValueError, match="on_missing_compressor"):
        Context(budget_tokens=100, model="gpt-4o", on_missing_compressor="shout")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- negative controls


def test_a_compressor_is_present_so_no_mode_fires():
    """NEGATIVE CONTROL: with a compressor available, none of the three modes changes anything —
    not even ``error``. The knob must only ever speak when the compressor is genuinely missing."""

    def compressor(text, target_tokens=0, **_kw):
        # A Compressor returns `(compressed_text, handle)` — the handle is what makes a compression
        # reversible, and is exactly what the truncation fallback cannot give you.
        return text[: max(1, target_tokens)], object()

    for mode in ("note", "warn", "error"):
        ctx = Context(budget_tokens=120, model="gpt-4o", compressor=compressor)
        ctx.on_missing_compressor = mode  # type: ignore[assignment]
        ctx.add(Block("keep me", priority=10, pin=True, role="system"))
        ctx.add(Block(LONG, priority=1, evict="compress", role="user"))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            ctx.assemble()  # must not raise and must not warn
        assert [d for d in ctx.report().decisions if d.role == "user"][0].action == "compressed"


def test_a_truncate_block_is_untouched_by_the_knob():
    """NEGATIVE CONTROL: a block that ASKED for truncation is not the case this knob is about."""
    for mode in ("note", "warn", "error"):
        ctx = Context(budget_tokens=120, model="gpt-4o", on_missing_compressor=mode)  # type: ignore[arg-type]
        ctx.add(Block("keep me", priority=10, pin=True, role="system"))
        ctx.add(Block(LONG, priority=1, evict="truncate", role="user"))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            ctx.assemble()
        assert [d for d in ctx.report().decisions if d.role == "user"][0].action == "truncated"


def test_a_block_that_fits_never_reaches_the_eviction_path():
    """NEGATIVE CONTROL: `error` mode must not refuse an assembly that never needed to evict."""
    ctx = Context(budget_tokens=4000, model="gpt-4o", on_missing_compressor="error")
    ctx.add(Block("short", priority=1, evict="compress", role="user"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ctx.assemble()
    assert [d for d in ctx.report().decisions if d.role == "user"][0].action == "kept"

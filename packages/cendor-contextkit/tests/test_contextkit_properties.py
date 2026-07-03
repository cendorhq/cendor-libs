"""Property test (Layer E): assembly never exceeds the budget, for any blocks. No network.

The invariant holds for any tokenizer, so these tests don't pin the heuristic.
"""

from cendor.contextkit import Block, Context
from cendor.core import tokens
from hypothesis import given
from hypothesis import strategies as st

# Non-pinned blocks with shrink/drop strategies → assembly never raises and never overflows.
_blocks = st.lists(
    st.builds(
        lambda content, priority, evict: Block(
            content, priority=priority, role="user", evict=evict
        ),
        content=st.text(max_size=300),
        priority=st.integers(0, 10),
        evict=st.sampled_from(["drop_oldest", "truncate"]),
    ),
    max_size=8,
)


@given(blocks=_blocks, budget=st.integers(1, 500), reserve=st.integers(0, 100))
def test_assembled_tokens_never_exceed_budget(blocks, budget, reserve):
    ctx = Context(budget_tokens=budget, model="gpt-4o", reserve_output=reserve)
    for b in blocks:
        ctx.add(b)
    ctx.assemble()
    report = ctx.report()
    assert report.used <= max(0, budget - reserve)


@given(blocks=_blocks, budget=st.integers(1, 500), reserve=st.integers(0, 100))
def test_used_matches_message_recount(blocks, budget, reserve):
    # For text content, the receipt's `used` equals the provider-level message recount exactly —
    # the "guaranteed within budget" promise holds at the message level, not just on content sums.
    ctx = Context(budget_tokens=budget, model="gpt-4o", reserve_output=reserve)
    for b in blocks:
        ctx.add(b)
    msgs = ctx.assemble()
    if msgs:  # the empty assembly is a degenerate edge (core counts a bare 3-token priming)
        assert tokens.count(msgs, "gpt-4o") == ctx.report().used


@given(blocks=_blocks, budget=st.integers(1, 500))
def test_assembly_is_deterministic(blocks, budget):
    def build():
        c = Context(budget_tokens=budget, model="gpt-4o")
        for b in blocks:
            c.add(Block(b.content, priority=b.priority, role=b.role, evict=b.evict))
        return c.assemble()

    assert build() == build()

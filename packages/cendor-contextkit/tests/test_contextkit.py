"""Budgeted assembly + the receipt. Deterministic, offline (heuristic token counts)."""

import pytest
from cendor.contextkit import Block, BudgetError, Context
from cendor.core import bus, tokens


@pytest.fixture(autouse=True)
def _heuristic_tokens(monkeypatch):
    # Force the offline heuristic so token math is deterministic regardless of tiktoken.
    monkeypatch.setattr(tokens, "_tiktoken_encoding", lambda model: None)
    yield


def test_public_api_present():
    import cendor.contextkit as ck

    for name in (
        "Block",
        "Context",
        "AssemblyReport",
        "BlockDecision",
        "BudgetError",
        "use_compressor",
    ):
        assert hasattr(ck, name)


def test_block_defaults():
    b = Block("hi", priority=5, pin=True, role="system")
    assert b.evict == "drop_oldest" and b.role == "system" and b.pin


def test_assemble_keeps_everything_under_budget():
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block("system prompt", priority=10, pin=True, role="system"))
    ctx.add(Block("the question", priority=9, pin=True, role="user"))
    messages = ctx.assemble()
    assert [m["role"] for m in messages] == ["system", "user"]  # system first, user last
    assert all(d.action == "kept" for d in ctx.report().decisions)


def test_drop_oldest_evicts_low_priority_when_tight():
    ctx = Context(budget_tokens=8, model="gpt-4o")  # ~8 tokens of room
    ctx.add(Block("x" * 4, priority=10, role="system"))  # ~1 tok, kept
    ctx.add(Block("y" * 200, priority=1, role="user", evict="drop_oldest"))  # too big -> dropped
    messages = ctx.assemble()
    roles = [m["role"] for m in messages]
    assert "user" not in roles  # low-priority block was dropped
    dropped = [d for d in ctx.report().decisions if d.action == "dropped"]
    assert len(dropped) == 1 and dropped[0].role == "user"


def test_truncate_shrinks_to_fit():
    ctx = Context(budget_tokens=40, model="gpt-4o")
    ctx.add(Block("s", priority=10, role="system"))
    ctx.add(Block("z" * 400, priority=1, role="user", evict="truncate"))
    ctx.assemble()
    decision = next(d for d in ctx.report().decisions if d.role == "user")
    assert decision.action == "truncated"
    assert decision.tokens_after < decision.tokens_before
    assert ctx.report().used <= ctx.report().budget - ctx.report().reserved_output


def test_pinned_overflow_raises():
    ctx = Context(budget_tokens=5, model="gpt-4o")
    ctx.add(Block("w" * 400, priority=10, pin=True, role="system"))
    with pytest.raises(BudgetError):
        ctx.assemble()


def test_reserve_output_reduces_usable_budget():
    ctx = Context(budget_tokens=100, model="gpt-4o", reserve_output=80)
    ctx.add(Block("a" * 200, priority=1, role="user", evict="truncate"))
    ctx.assemble()
    # only ~20 tokens usable -> truncated small, and never over the usable budget
    assert ctx.report().used <= 20
    assert next(d for d in ctx.report().decisions if d.role == "user").action == "truncated"


def test_whatif_does_not_commit():
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block("hello there", priority=5, role="user"))
    ctx.assemble()
    committed_used = ctx.report().used
    preview = ctx.whatif(budget_tokens=3)
    assert preview.budget == 3
    assert ctx.report().used == committed_used  # committed report unchanged


def test_assembly_is_deterministic():
    def build():
        c = Context(budget_tokens=50, model="gpt-4o")
        c.add(Block("alpha", priority=5, role="user"))
        c.add(Block("beta", priority=5, role="assistant"))
        c.add(Block("gamma", priority=9, role="system"))
        return c.assemble()

    assert build() == build()


def test_report_emitted_on_bus():
    bus._reset()
    seen = []
    bus.subscribe(seen.append)
    try:
        ctx = Context(budget_tokens=100, model="gpt-4o")
        ctx.add(Block("hi", role="user"))
        ctx.assemble()
    finally:
        bus._reset()
    assert len(seen) == 1 and seen[0].model == "gpt-4o"


def test_attention_order_edge_loads_priority():
    # Lost-in-the-middle: highest-priority context blocks ride the edges, weakest in the center.
    ctx = Context(budget_tokens=1000, model="gpt-4o", order="attention")
    ctx.add(Block("SYS", priority=100, pin=True, role="system"))
    ctx.add(Block("p9", priority=9, role="assistant"))
    ctx.add(Block("p1", priority=1, role="assistant"))
    ctx.add(Block("p5", priority=5, role="assistant"))
    ctx.add(Block("p7", priority=7, role="assistant"))
    ctx.add(Block("USER", priority=10, pin=True, role="user"))
    msgs = ctx.assemble()
    assert msgs[0]["content"] == "SYS"  # system anchored first
    assert msgs[-1]["content"] == "USER"  # user turn anchored last
    middle = [m["content"] for m in msgs[1:-1]]
    # desc by priority = [p9,p7,p5,p1] -> edge-loaded -> [p9,p5,p1,p7]: strongest on the edges
    assert middle[0] == "p9" and middle[-1] == "p7"
    assert middle[len(middle) // 2] == "p1"  # weakest in the center
    assert ctx.report().order == "attention"


def test_cache_order_puts_pinned_prefix_first():
    ctx = Context(budget_tokens=1000, model="gpt-4o", order="cache")
    ctx.add(Block("volatile", priority=8, pin=False, role="user"))
    ctx.add(Block("stable-hi", priority=10, pin=True, role="system"))
    ctx.add(Block("stable-lo", priority=2, pin=True, role="assistant"))
    msgs = ctx.assemble()
    # pinned blocks form the stable prefix (priority desc), volatile trails
    assert [m["content"] for m in msgs] == ["stable-hi", "stable-lo", "volatile"]


def test_invalid_order_rejected():
    with pytest.raises(ValueError):
        Context(budget_tokens=10, model="gpt-4o", order="bogus")


def test_default_order_unchanged():
    ctx = Context(budget_tokens=1000, model="gpt-4o")  # default
    ctx.add(Block("u", priority=9, role="user"))
    ctx.add(Block("s", priority=1, role="system"))
    assert [m["role"] for m in ctx.assemble()] == ["system", "user"]
    assert ctx.report().order == "default"


def test_for_anthropic_splits_system():
    ctx = Context(budget_tokens=1000, model="claude-opus-4-8")
    ctx.add(Block("you are helpful", priority=10, pin=True, role="system"))
    ctx.add(Block("hello", priority=9, pin=True, role="user"))
    system, messages = ctx.for_anthropic()
    assert system == "you are helpful"
    assert all(m["role"] != "system" for m in messages)


def test_multimodal_image_token_cost():
    ctx = Context(budget_tokens=1000, model="gpt-4o", image_tokens=85)
    block = Block(
        [{"type": "text", "text": "look"}, {"type": "image", "image_url": "..."}],
        priority=9,
        pin=True,
        role="user",
    )
    ctx.add(block)
    ctx.assemble()
    d = ctx.report().decisions[0]
    # text("look") ~1 tok + 1 image * 85 = ~86
    assert d.tokens_before >= 85
    # multimodal content is preserved as a list in the rendered message
    assert isinstance(ctx.assemble()[0]["content"], list)


def test_multimodal_block_dropped_when_too_large():
    ctx = Context(budget_tokens=20, model="gpt-4o", image_tokens=1000)
    ctx.add(Block("keep", priority=10, role="system"))
    ctx.add(Block([{"type": "image"}], priority=1, role="user", evict="drop_oldest"))
    ctx.assemble()
    dropped = [d for d in ctx.report().decisions if d.action == "dropped"]
    assert len(dropped) == 1


async def test_async_summarizer_via_aassemble():
    calls = {"n": 0}

    async def summarizer(text, target):
        calls["n"] += 1
        return "async summary"

    ctx = Context(budget_tokens=40, model="gpt-4o")
    ctx.add(Block("s", priority=10, role="system"))
    ctx.add(Block("z" * 400, priority=1, role="user", evict="summarize", summarizer=summarizer))
    msgs = await ctx.aassemble()
    assert calls["n"] == 1  # the async summarizer ran
    assert "async summary" in [m["content"] for m in msgs]
    assert any(d.action == "summarized" for d in ctx.report().decisions)


def test_sync_assemble_falls_back_for_async_summarizer():
    async def summarizer(text, target):
        return "nope"

    ctx = Context(budget_tokens=12, model="gpt-4o")
    ctx.add(Block("z" * 400, priority=1, role="user", evict="summarize", summarizer=summarizer))
    ctx.assemble()  # sync path can't await -> truncates with a note
    d = ctx.report().decisions[0]
    assert d.action == "truncated" and "aassemble" in d.note


def test_for_gemini_adapter():
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block("be helpful", priority=10, pin=True, role="system"))
    ctx.add(Block("prior reply", priority=5, role="assistant"))
    ctx.add(Block("question", priority=9, pin=True, role="user"))
    system, contents = ctx.for_gemini()
    assert system == "be helpful"
    roles = [c["role"] for c in contents]
    assert "model" in roles and "user" in roles and "system" not in roles  # assistant -> model
    assert contents[0]["parts"] == [{"text": "be helpful"}] or contents[0]["parts"][0]["text"]


def test_use_compressor_sets_global_default():
    import cendor.contextkit as ck

    def fake(text, target_tokens=None):  # a Compressor-shaped callable (e.g. an external backend)
        return ("CX", None)

    previous = ck.use_compressor(fake)
    try:
        ctx = Context(budget_tokens=50, model="gpt-4o")
        ctx.add(Block("s", priority=10, role="system"))
        ctx.add(
            Block("z" * 400, priority=1, role="user", evict="compress")
        )  # overflows -> compress
        messages = ctx.assemble()
        d = next(d for d in ctx.report().decisions if d.role == "user")
        assert d.action == "compressed"
        assert "CX" in [m["content"] for m in messages]  # the pluggable backend ran
    finally:
        ck.use_compressor(previous)


def test_per_context_compressor_overrides_default():
    import cendor.contextkit as ck

    ck.use_compressor(lambda text, target_tokens=None: ("GLOBAL", None))
    try:
        ctx = Context(
            budget_tokens=50,
            model="gpt-4o",
            compressor=lambda text, target_tokens=None: ("LOCAL", None),
        )
        ctx.add(Block("z" * 400, priority=1, role="user", evict="compress"))
        messages = ctx.assemble()
        assert "LOCAL" in [m["content"] for m in messages]  # per-Context wins over the global
    finally:
        ck.use_compressor(None)


def test_compress_eviction_exposes_working_handle():
    # reversibility is squeeze's USP — the receipt must surface the Handle so a caller can expand().
    pytest.importorskip("cendor.squeeze")
    import cendor.contextkit as ck

    ck.use_compressor(None)  # use the auto-discovered squeeze.compress default path
    original = "The quarterly report covers revenue, churn, retention, and the 2026 roadmap. " * 20
    ctx = Context(budget_tokens=60, model="gpt-4o")
    ctx.add(Block("be helpful", priority=10, role="system"))
    ctx.add(Block(original, priority=1, role="user", evict="compress"))
    ctx.assemble()

    d = next(d for d in ctx.report().decisions if d.role == "user")
    assert d.action == "compressed"
    assert d.handle is not None  # the squeeze Handle is on the receipt
    assert d.handle.expand() == original  # and it reverses to the exact original


def test_default_path_forwards_context_model():
    # The default compress path must forward the Context's model, not squeeze's gpt-4o default.
    import cendor.contextkit as ck

    seen = {}

    def spy(text, target_tokens=None, model=None):
        seen["model"] = model
        return ("SMALL", None)

    previous = ck.use_compressor(spy)
    try:
        ctx = Context(budget_tokens=50, model="claude-opus-4-8")
        ctx.add(Block("z" * 400, priority=1, role="user", evict="compress"))
        ctx.assemble()
        assert seen["model"] == "claude-opus-4-8"
    finally:
        ck.use_compressor(previous)


def test_legacy_compressor_without_model_still_works():
    # A (text, target_tokens) callable that doesn't take model must not break (no model forwarded).
    import cendor.contextkit as ck

    previous = ck.use_compressor(lambda text, target_tokens=None: ("SMALL", None))
    try:
        ctx = Context(budget_tokens=50, model="claude-opus-4-8")
        ctx.add(Block("z" * 400, priority=1, role="user", evict="compress"))
        messages = ctx.assemble()
        assert "SMALL" in [m["content"] for m in messages]
    finally:
        ck.use_compressor(previous)


def test_for_anthropic_coerces_nonstandard_roles():
    # The Anthropic Messages API accepts only user/assistant — a tool block must be coerced, not
    # passed through as role="tool" (which the API rejects).
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block("be helpful", priority=10, pin=True, role="system"))
    ctx.add(Block("assistant turn", priority=9, pin=True, role="assistant"))
    ctx.add(Block("tool output here", priority=8, pin=True, role="tool"))
    system, messages = ctx.for_anthropic()

    assert system  # system split out of messages
    assert all(m["role"] in ("user", "assistant") for m in messages)  # only API-valid roles
    assert not any(m["role"] == "system" for m in messages)
    # the tool block landed as a user message (its content preserved)
    assert any(m["role"] == "user" and "tool output here" in str(m["content"]) for m in messages)


def test_for_bedrock_adapter():
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block("be helpful", priority=10, pin=True, role="system"))
    ctx.add(Block("question", priority=9, pin=True, role="user"))
    system, messages = ctx.for_bedrock()
    assert system == [{"text": "be helpful"}]
    assert messages == [{"role": "user", "content": [{"text": "question"}]}]
    assert all(m["role"] in ("user", "assistant") for m in messages)


# --------------------------------------------------------------------- budget accuracy


def test_used_matches_message_level_recount():
    # The receipt's `used` must equal what the provider actually sees (content + per-message
    # framing). A bare content-sum under-reports by priming + 4*N tokens.
    ctx = Context(budget_tokens=200, model="gpt-4o")
    for i in range(5):
        ctx.add(Block(f"block number {i} with some text", priority=5, role="user"))
    msgs = ctx.assemble()
    assert tokens.count(msgs, "gpt-4o") == ctx.report().used


def test_assembly_stays_within_budget_when_remeasured():
    # Tight budget, no reserve: the assembled messages re-counted must not exceed the budget.
    ctx = Context(budget_tokens=40, model="gpt-4o", reserve_output=0)
    for i in range(6):
        ctx.add(Block(f"chunk {i} of context", priority=5, role="user", evict="truncate"))
    msgs = ctx.assemble()
    assert tokens.count(msgs, "gpt-4o") <= 40


# --------------------------------------------------------------------- message-list blocks


def test_history_block_peels_oldest_turns():
    turns = [
        {"role": "user", "content": "oldest " * 10},
        {"role": "assistant", "content": "middle " * 10},
        {"role": "user", "content": "newest " * 10},
    ]
    ctx = Context(budget_tokens=40, model="gpt-4o")
    ctx.add(Block(messages=turns, priority=5, evict="drop_oldest"))
    msgs = ctx.assemble()
    contents = " ".join(m["content"] for m in msgs)
    assert "newest" in contents  # most recent turn kept
    assert "oldest" not in contents  # oldest turn peeled
    d = ctx.report().decisions[0]
    assert d.action == "truncated" and "kept" in d.note and "of 3 turns" in d.note


def test_empty_history_block_reports_kept_not_dropped():
    # L5: Block(messages=[]) with plenty of budget must not claim "dropped all 0 turns (no room)".
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block(messages=[], priority=5))
    ctx.assemble()
    hist = [d for d in ctx.report().decisions if d.role == "history"]
    if hist:  # an empty history may or may not emit a decision; if it does, it's not a false drop
        assert hist[0].action == "kept"
        assert "dropped all 0" not in hist[0].note


def test_history_block_kept_whole_when_it_fits():
    turns = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block(messages=turns, priority=5))
    msgs = ctx.assemble()
    assert [m["content"] for m in msgs] == ["hi", "hello"]  # chronological order preserved
    assert ctx.report().decisions[0].action == "kept"


def test_history_block_orders_in_the_middle():
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block("the question", priority=9, pin=True, role="user"))
    ctx.add(Block("you are helpful", priority=10, pin=True, role="system"))
    ctx.add(Block(messages=[{"role": "user", "content": "earlier turn"}], priority=5))
    roles = [m["role"] for m in ctx.assemble()]
    assert roles[0] == "system" and roles[-1] == "user"  # history sits between system and the turn


def test_history_truncate_trims_newest_when_alone_too_big():
    turns = [{"role": "user", "content": "z" * 800}]
    ctx = Context(budget_tokens=40, model="gpt-4o")
    ctx.add(Block(messages=turns, priority=5, evict="truncate"))
    ctx.assemble()
    assert ctx.report().used <= 40
    assert ctx.report().decisions[0].tokens_after < ctx.report().decisions[0].tokens_before


def test_pinned_history_overflow_raises():
    turns = [{"role": "user", "content": "w" * 800}]
    ctx = Context(budget_tokens=20, model="gpt-4o")
    ctx.add(Block(messages=turns, priority=10, pin=True))
    with pytest.raises(BudgetError):
        ctx.assemble()


def test_block_requires_exactly_one_of_content_or_messages():
    with pytest.raises(ValueError):
        Block()  # neither
    with pytest.raises(ValueError):
        Block("text", messages=[{"role": "user", "content": "x"}])  # both
    with pytest.raises(ValueError):
        Block(messages=[{"oops": "no role/content"}])  # malformed turn


# --------------------------------------------------------------------- truncate options


def test_truncate_keep_tail_keeps_the_end():
    ctx = Context(budget_tokens=60, model="gpt-4o")
    text = "HEAD " + "x " * 200 + "TAIL"
    ctx.add(Block(text, priority=1, role="user", evict="truncate", keep="tail"))
    kept = ctx.assemble()[0]["content"]
    assert "TAIL" in kept and "HEAD" not in kept


def test_truncate_keep_head_keeps_the_start():
    ctx = Context(budget_tokens=60, model="gpt-4o")
    ctx.add(Block("HEAD " + "x " * 200 + "TAIL", priority=1, role="user", evict="truncate"))
    kept = ctx.assemble()[0]["content"]
    assert "HEAD" in kept and "TAIL" not in kept


def test_truncate_leaves_a_marker():
    ctx = Context(budget_tokens=80, model="gpt-4o")
    ctx.add(Block("y" * 800, priority=1, role="user", evict="truncate"))
    kept = ctx.assemble()[0]["content"]
    assert "[truncated]" in kept


# --------------------------------------------------------------------- pluggable eviction strategy


def test_custom_eviction_strategy_object():
    class KeepFirstWord:  # satisfies core.protocols.EvictionStrategy by shape
        def evict(self, content, remaining_tokens, model):
            return content.split()[0], "evicted"

    ctx = Context(budget_tokens=40, model="gpt-4o")
    ctx.add(Block("supercalifragilistic " * 50, priority=1, role="user", evict=KeepFirstWord()))
    msgs = ctx.assemble()
    assert msgs[0]["content"] == "supercalifragilistic"
    assert ctx.report().decisions[0].action == "evicted"


# --------------------------------------------------------------------- multimodal adapters


def test_for_gemini_multimodal_parts_well_formed():
    ctx = Context(budget_tokens=1000, model="gemini-1.5-pro", image_tokens=85)
    ctx.add(
        Block(
            [{"type": "text", "text": "look"}, {"type": "image", "image_url": "x"}],
            priority=9,
            pin=True,
            role="user",
        )
    )
    _system, contents = ctx.for_gemini()
    parts = contents[0]["parts"]
    assert {"text": "look"} in parts  # text part is a {"text": ...}, not a wrapped list
    assert {"type": "image", "image_url": "x"} in parts  # image part passes through


def test_for_anthropic_system_multimodal_does_not_crash():
    ctx = Context(budget_tokens=1000, model="claude-opus-4-8")
    ctx.add(Block([{"type": "text", "text": "sys"}], priority=10, pin=True, role="system"))
    system, _rest = ctx.for_anthropic()
    assert system == "sys"


def test_image_tokens_callable():
    ctx = Context(budget_tokens=1000, model="gpt-4o", image_tokens=lambda part: 130)
    ctx.add(Block([{"type": "image", "image_url": "x"}], priority=9, pin=True, role="user"))
    ctx.assemble()
    assert ctx.report().decisions[0].tokens_before == 130

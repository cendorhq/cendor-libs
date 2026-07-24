"""Complements test_contextkit.py: shape-locks the documented cross-adapter role coercion (a
non-user/assistant role like ``tool`` is remapped per provider) and the conservation invariants of
the three ``order`` modes and the three provider adapters. Deterministic, offline, no network.
"""

from cendor.contextkit import Block, Context


def _ctx_with_tool_role() -> Context:
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block("be helpful", priority=10, pin=True, role="system"))
    ctx.add(Block("prior tool output", priority=5, role="tool"))  # neither user nor assistant
    ctx.add(Block("the question", priority=9, pin=True, role="user"))
    return ctx


# ----------------------------------------------------- documented role coercion per adapter
def test_for_anthropic_coerces_tool_role_to_user():
    system, messages = _ctx_with_tool_role().for_anthropic()
    assert system == "be helpful"  # system split out
    roles = [m["role"] for m in messages]
    assert set(roles) <= {"user", "assistant"}  # Messages API only accepts these two
    assert "system" not in roles
    tool_msg = next(m for m in messages if m["content"] == "prior tool output")
    assert tool_msg["role"] == "user"  # tool -> user


def test_for_gemini_coerces_tool_role_to_user_with_parts():
    system, contents = _ctx_with_tool_role().for_gemini()
    assert system == "be helpful"
    assert all(set(c) == {"role", "parts"} for c in contents)
    assert all(c["role"] in ("user", "model") for c in contents)  # gemini uses `model`
    tool_content = next(c for c in contents if c["parts"] == [{"text": "prior tool output"}])
    assert tool_content["role"] == "user"  # tool -> user


def test_for_bedrock_coerces_tool_role_to_assistant():
    system, messages = _ctx_with_tool_role().for_bedrock()
    assert system == [{"text": "be helpful"}]
    assert all(m["role"] in ("user", "assistant") for m in messages)
    assert all(isinstance(m["content"], list) for m in messages)  # content blocks, not a bare str
    tool_msg = next(m for m in messages if m["content"] == [{"text": "prior tool output"}])
    assert tool_msg["role"] == "assistant"  # bedrock maps every non-user block to assistant


# --------------------------------------------------------------- conservation invariants
def _texts(ctx: Context) -> list[str]:
    return [m["content"] for m in ctx.assemble()]


def test_order_modes_conserve_the_kept_set():
    # attention/cache/default reorder the SAME kept blocks — none is gained or lost by ordering.
    def build(order: str) -> Context:
        ctx = Context(budget_tokens=1000, model="gpt-4o", order=order)
        ctx.add(Block("SYS", priority=100, pin=True, role="system"))
        ctx.add(Block("a", priority=9, role="assistant"))
        ctx.add(Block("b", priority=5, role="assistant"))
        ctx.add(Block("c", priority=1, role="assistant"))
        ctx.add(Block("USER", priority=10, pin=True, role="user"))
        return ctx

    default_set = sorted(_texts(build("default")))
    assert sorted(_texts(build("attention"))) == default_set
    assert sorted(_texts(build("cache"))) == default_set
    assert len(default_set) == 5  # everything fit → all kept, only the arrangement differs


def test_adapters_preserve_non_system_messages_of_one_assemble():
    ctx = Context(budget_tokens=1000, model="gpt-4o")
    ctx.add(Block("sys", priority=10, pin=True, role="system"))
    ctx.add(Block("u1", priority=9, pin=True, role="user"))
    ctx.add(Block("a1", priority=5, role="assistant"))
    non_system = [m for m in ctx.assemble() if m["role"] != "system"]

    _sys_a, anthropic_msgs = ctx.for_anthropic()
    _sys_g, gemini_contents = ctx.for_gemini()
    _sys_b, bedrock_msgs = ctx.for_bedrock()
    # Each adapter emits exactly the non-system messages, just reshaped per provider.
    assert len(anthropic_msgs) == len(gemini_contents) == len(bedrock_msgs) == len(non_system)

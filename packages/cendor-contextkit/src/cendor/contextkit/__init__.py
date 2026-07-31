"""cendor.contextkit — assemble context within a token budget, with a receipt.

Treat the context window like a packed suitcase: declare ``Block``s with priority, pin, and a
per-block eviction rule; :meth:`Context.assemble` packs them to a token budget (deterministically)
and :meth:`Context.report` returns the receipt — what was kept, shrunk, or dropped, with the token
math. Depends only on ``cendor-core`` (``tokens`` + the ``Compressor``/``EvictionStrategy``
protocols). Tools never import each other; ``squeeze`` plugs in by shape via the
``contextkit[squeeze]`` extra.

The receipt is honest at the **message** level: budgeting charges the per-message framing overhead
that providers add around every turn (self-calibrated from ``core.tokens``), so ``report().used``
equals ``core.tokens.count(assemble(), model)`` for text content — what the model actually sees.
"""

from __future__ import annotations

import inspect
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from cendor.core import bus, protocols, tokens

__all__ = [
    "AssemblyReport",
    "Block",
    "BlockDecision",
    "BudgetError",
    "Context",
    "MissingCompressorError",
    "MissingCompressorWarning",
    "use_compressor",
]

# Built-in string strategies; ``Block.evict`` also accepts any core EvictionStrategy object.
EvictStrategy = Literal["drop_oldest", "truncate", "summarize", "compress"]

#: What ``Context`` does when a block asks for ``evict="compress"`` and no compressor is available.
#: The block is TRUNCATED in that case — lossy and not reversible, unlike a compression — so this
#: chooses how visible that substitution is. ``"note"`` is the historical behaviour (recorded on the
#: block's ``BlockDecision``, and nothing else); ``"warn"`` also emits a
#: :class:`MissingCompressorWarning`; ``"error"`` raises :class:`MissingCompressorError` instead of
#: quietly truncating. Default ``"note"`` — this is additive, and no existing assembly changes.
MissingCompressorMode = Literal["note", "warn", "error"]
_MISSING_COMPRESSOR_MODES = ("note", "warn", "error")


class MissingCompressorWarning(UserWarning):
    """Emitted (only under ``on_missing_compressor="warn"``) when a ``compress`` block is truncated
    because no compressor is available."""


class MissingCompressorError(RuntimeError):
    """Raised (only under ``on_missing_compressor="error"``) instead of truncating a ``compress``
    block for which no compressor is available."""


# Optional process-wide default compressor for evict="compress" blocks. contextkit doesn't care
# *who* compresses — only that it matches core's Compressor protocol by shape. By default it
# auto-discovers cendor.squeeze (the deterministic, zero-dep backend) via contextkit[squeeze];
# set this to swap in any other backend (e.g. an ML-based compressor) globally.
_default_compressor: Any = None

# Per-model (priming, per_message) framing overhead, derived once from core.tokens' public API:
# count([one empty msg]) = priming + per_message; the delta to two empty msgs isolates per_message.
# This stays correct for any registered tokenizer without importing core internals.
_framing_cache: dict[str, tuple[int, int]] = {}


def _framing(model: str) -> tuple[int, int]:
    """Return ``(priming, per_message)`` token overhead for ``model``, per ``core.tokens``."""
    cached = _framing_cache.get(model)
    if cached is not None:
        return cached
    one = tokens.count([{"role": "user", "content": ""}], model)
    two = tokens.count([{"role": "user", "content": ""}, {"role": "user", "content": ""}], model)
    per_message = max(0, two - one)
    priming = max(0, one - per_message)
    _framing_cache[model] = (priming, per_message)
    return priming, per_message


def use_compressor(compressor: Any) -> Any:
    """Set the default compressor for ``evict="compress"`` blocks; returns the previous one.

    Accepts anything matching ``core.protocols.Compressor`` — a ``compress(content, *,
    target_tokens, model, ...)`` object (e.g. ``squeeze.SqueezeCompressor()``) or a
    ``compress(text, target_tokens=)`` callable — so you can plug in an alternative backend without
    touching call sites. Pass ``None`` to clear (falls back to auto-discovering ``squeeze``). A
    per-``Context`` ``compressor=`` argument still overrides this default.

    ```python
    from cendor.contextkit import use_compressor
    from cendor.squeeze import SqueezeCompressor

    use_compressor(SqueezeCompressor())   # process-wide default for evict="compress" blocks
    ```
    """
    global _default_compressor
    previous, _default_compressor = _default_compressor, compressor
    return previous


class BudgetError(Exception):
    """Raised when pinned blocks alone exceed the budget (they are never evicted)."""


@dataclass
class Block:
    """A unit of context with packing intent. See docs/contextkit.md §6.

    Provide **exactly one** of ``content`` (a single message, text or multimodal parts) or
    ``messages`` (a conversation segment — a list of ``{"role", "content"}`` turns that
    ``evict="drop_oldest"`` shrinks by peeling the *oldest* turns until it fits).

    ```python
    from cendor.contextkit import Block

    Block("system prompt", priority=10, pin=True, role="system")
    ```

    ``evict="compress"`` needs the ``contextkit[squeeze]`` extra installed; without it a
    ``compress`` block falls back to truncation.

    Attributes:
        content: The block's content for a single-message block — text, or a list of multimodal
            parts. Leave ``None`` when using ``messages``.
        priority: Higher is admitted first; ties break by insertion order (deterministic).
        pin: Pinned blocks are never evicted (assembly raises if pinned blocks alone overflow).
        evict: Strategy when this block overflows the remaining budget — a built-in name
            (:data:`EvictStrategy`) or any ``core.protocols.EvictionStrategy`` object.
        role: Provider message role for a single-message block: ``system`` | ``user`` |
            ``assistant`` | ``tool``. Ignored for ``messages`` blocks (each turn carries its own).
        summarizer: Callback ``(content, target_tokens) -> str`` used when ``evict="summarize"``.
        keep: For ``evict="truncate"``, which end to keep — ``"head"`` (default) or ``"tail"``.
        messages: A conversation segment as ``[{"role", "content"}, ...]``; mutually exclusive
            with ``content``. Eviction peels the oldest turns (a sliding window of recent context).
    """

    content: str | list | None = None
    priority: int = 0
    pin: bool = False
    evict: EvictStrategy | protocols.EvictionStrategy = "drop_oldest"
    role: str = "user"
    summarizer: Callable[[str, int], Any] | None = None
    keep: Literal["head", "tail"] = "head"
    messages: list[dict] | None = None

    def __post_init__(self) -> None:
        if (self.content is None) == (self.messages is None):
            raise ValueError("Block requires exactly one of content= or messages=")
        if self.keep not in ("head", "tail"):
            raise ValueError(f"keep must be 'head' or 'tail', got {self.keep!r}")
        if self.messages is not None and not all(
            isinstance(t, dict) and "role" in t and "content" in t for t in self.messages
        ):
            raise ValueError("each item in messages= must be a {'role', 'content'} dict")


@dataclass
class BlockDecision:
    """What happened to one block during assembly (a line on the receipt).

    ``tokens_before``/``tokens_after`` are *content* tokens (framing-exclusive); the report's
    ``used`` additionally accounts for per-message framing.
    """

    role: str
    action: str  # "kept" | "truncated" | "summarized" | "compressed" | "dropped"
    tokens_before: int
    tokens_after: int
    note: str = ""
    #: For a ``"compressed"`` block, the reversible squeeze ``Handle`` — call ``.expand()`` to get
    #: the original content back (squeeze's USP). ``None`` for every other action.
    handle: Any = None


@dataclass
class AssemblyReport:
    """The receipt: budget math + per-block decisions. See docs/contextkit.md §6.

    ``used`` is the message-level token count of the assembled prompt (content + framing), so it
    equals ``core.tokens.count(messages, model)`` for text content; multimodal image budget is also
    charged into ``used`` even though ``core.tokens`` can't see image parts.
    """

    budget: int
    used: int
    reserved_output: int
    model: str
    decisions: list[BlockDecision] = field(default_factory=list)
    order: str = "default"

    def __str__(self) -> str:
        lines = [
            f"AssemblyReport(model={self.model}, order={self.order}) "
            f"budget={self.budget} reserved_output={self.reserved_output} "
            f"used={self.used}/{self.budget - self.reserved_output}",
        ]
        for d in self.decisions:
            arrow = f"{d.tokens_before}->{d.tokens_after}tok"
            note = f"  # {d.note}" if d.note else ""
            lines.append(f"  [{d.action:<10}] {d.role:<9} {arrow}{note}")
        return "\n".join(lines)


# Default render order: system first, history/context middle, the user turn last.
_ROLE_RANK = {"system": 0, "history": 1, "tool": 1, "assistant": 2, "user": 3}
_ORDERS = ("default", "attention", "cache")

#: The three ``order`` strategies as a type (mirrors :data:`_ORDERS`) so an editor autocompletes
#: them and a typo is a type error. A bad string still raises ``ValueError`` at runtime.
OrderMode = Literal["default", "attention", "cache"]


def _ord_role(block: Block) -> str:
    """The role a block is ordered by — ``"history"`` for a multi-turn (``messages``) block."""
    return "history" if block.messages is not None else block.role


@dataclass
class _PackState:
    """Running state threaded through one packing pass (shared by sync/async assembly)."""

    used: int = 0
    has_msgs: bool = False
    decisions: list = field(default_factory=list)
    kept: list = field(default_factory=list)


@dataclass
class _BlockPlan:
    """What to do with one single-message block, decided *before* any (a)sync eviction runs.

    ``status`` is ``"kept"`` | ``"dropped"`` | ``"evict"``. For ``"evict"`` the plan carries the
    content budget so the caller runs the sync or async evictor — the one line that differs between
    :meth:`Context._pack` and :meth:`Context._apack`.
    """

    status: str
    used: int = 0
    message: dict | None = None
    decision: BlockDecision | None = None
    content_budget: int = 0
    prim: int = 0
    content_tokens: int = 0
    text: str = ""  # the str-narrowed content to evict (only set when status == "evict")


class Context:
    """A token-budgeted, declarative context assembler. See docs/contextkit.md §3, §5.

    ``order`` controls how kept blocks are arranged in the final messages (docs/contextkit.md §2):

    - ``"default"`` — role-grouped: system → history/context → the user turn.
    - ``"attention"`` — "lost-in-the-middle": highest-priority context blocks ride the edges
      (just after system / just before the user turn), weakest in the dead center.
    - ``"cache"`` — stable prefix first (pinned, high-priority blocks lead) to maximize provider
      prompt-cache / KV-cache hits across calls.

    Create it with a budget and model, add :class:`Block`s, then call the **synchronous**
    :meth:`assemble` (the async form is the separate :meth:`aassemble`):

    ```python
    from cendor.contextkit import Context

    ctx = Context(budget_tokens=8000, model="gpt-4o", reserve_output=1000)
    ```
    """

    def __init__(
        self,
        budget_tokens: int,
        model: str,
        reserve_output: int = 0,
        compressor: Any = None,
        order: OrderMode = "default",
        image_tokens: int | Callable[[dict], int] = 0,
        on_missing_compressor: MissingCompressorMode = "note",
    ) -> None:
        if order not in _ORDERS:
            raise ValueError(f"order must be one of {_ORDERS}, got {order!r}")
        if on_missing_compressor not in _MISSING_COMPRESSOR_MODES:
            raise ValueError(
                f"on_missing_compressor must be one of {_MISSING_COMPRESSOR_MODES}, "
                f"got {on_missing_compressor!r}"
            )
        self.budget_tokens = budget_tokens
        self.model = model
        self.reserve_output = reserve_output
        self._compressor = compressor
        self.order = order
        self.on_missing_compressor = on_missing_compressor
        # Token cost per image part in multimodal blocks: a flat int, or a callable
        # (part_dict -> tokens) for resolution-aware estimates.
        self.image_tokens = image_tokens
        self._blocks: list[Block] = []
        self._report: AssemblyReport | None = None
        self._messages: list[dict] = []

    def add(self, block: Block) -> Context:
        """Add a block. Returns ``self`` for chaining."""
        self._blocks.append(block)
        return self

    def assemble(self) -> list[dict]:
        """Pack blocks within the budget; return provider-ready messages (OpenAI/Foundry shape).

        Deterministic: stable sort by ``(pinned, priority, insertion order)``. Emits the
        :class:`AssemblyReport` onto core's bus so ``acttrace`` records what the model saw.

        ```python
        messages = ctx.assemble()   # sync; the async variant is ctx.aassemble()
        ```

        Python's ``assemble()`` is **synchronous** — the async form is the separate
        :meth:`aassemble` method (this differs from TypeScript, where ``assemble()`` is async).
        """
        messages, report = self._pack(self.budget_tokens, emit=True)
        self._messages = messages
        self._report = report
        return messages

    def report(self) -> AssemblyReport:
        """Return the receipt for the most recent :meth:`assemble`. Raises before the first one.

        ```python
        print(ctx.report())   # budget math + per-block decisions
        ```
        """
        if self._report is None:
            raise RuntimeError("call assemble() before report()")
        return self._report

    def whatif(self, budget_tokens: int) -> AssemblyReport:
        """Preview the assembly at a different budget without committing (no bus emit)."""
        _, report = self._pack(budget_tokens, emit=False)
        return report

    async def aassemble(self) -> list[dict]:
        """Async assemble — like :meth:`assemble` but awaits ``async`` summarize callbacks.

        Use this when a block's ``summarizer`` is a coroutine (e.g. an LLM summarizer). The sync
        :meth:`assemble` falls back to truncation for async summarizers.
        """
        messages, report = await self._apack(self.budget_tokens, emit=True)
        self._messages = messages
        self._report = report
        return messages

    def for_anthropic(self) -> tuple[str, list[dict]]:
        """Anthropic adapter: split system blocks out (the Messages API takes ``system`` apart).

        Returns ``(system_text, messages)`` from the most recent :meth:`assemble`. The Messages API
        accepts only ``user``/``assistant`` roles, so — like :meth:`for_gemini`/:meth:`for_bedrock`
        — any other role (e.g. ``tool``) is coerced to ``user`` (a raw ``role="tool"`` would be
        rejected). Multimodal content is passed through unchanged (Anthropic accepts content-block
        lists).
        """
        if not self._messages:
            self.assemble()
        rest = [
            {"role": "assistant" if m["role"] == "assistant" else "user", "content": m["content"]}
            for m in self._messages
            if m["role"] != "system"
        ]
        return self._system_text(), rest

    def for_gemini(self) -> tuple[str, list[dict]]:
        """Gemini adapter: returns ``(system_instruction, contents)``.

        ``contents`` are ``{"role": "user"|"model", "parts": [...]}`` (Gemini uses ``model``, not
        ``assistant``); system blocks become the separate ``system_instruction``. Multimodal parts
        are mapped to Gemini parts (``{"text": ...}`` for text; non-text parts pass through).
        """
        if not self._messages:
            self.assemble()
        contents = [
            {
                "role": "model" if m["role"] == "assistant" else "user",
                "parts": _parts_of(m["content"]),
            }
            for m in self._messages
            if m["role"] != "system"
        ]
        return self._system_text(), contents

    def for_bedrock(self) -> tuple[list[dict], list[dict]]:
        """Bedrock Converse adapter: returns ``(system, messages)``.

        ``system`` is ``[{"text": ...}]`` (or empty); ``messages`` are
        ``{"role": "user"|"assistant", "content": [...]}`` — Bedrock allows only those two roles, so
        non-user blocks map to ``assistant`` and content becomes a list of content blocks.
        """
        if not self._messages:
            self.assemble()
        system_text = self._system_text()
        system = [{"text": system_text}] if system_text else []
        messages = [
            {
                "role": "user" if m["role"] == "user" else "assistant",
                "content": _parts_of(m["content"]),
            }
            for m in self._messages
            if m["role"] != "system"
        ]
        return system, messages

    # ------------------------------------------------------------------ internals

    def _system_text(self) -> str:
        """Join the text of all assembled system messages (adapters split ``system`` out)."""
        return "\n\n".join(_text_of(m["content"]) for m in self._messages if m["role"] == "system")

    def _ordered_blocks(self) -> list[tuple[int, Block]]:
        # (not pin) -> pinned (False) sorts first; then priority desc; then insertion order.
        return sorted(
            enumerate(self._blocks), key=lambda iv: (not iv[1].pin, -iv[1].priority, iv[0])
        )

    def _image_cost(self, part: dict) -> int:
        it = self.image_tokens
        return it(part) if callable(it) else it

    def _content_tokens(self, content: Any) -> int:
        """Token cost of content, charging ``image_tokens`` per image part in multimodal lists."""
        if isinstance(content, list):
            text = "".join(
                p.get("text", "") for p in content if isinstance(p, dict) and "text" in p
            )
            n = tokens.count(text, self.model) if text else 0
            for p in content:
                if isinstance(p, dict) and p.get("type") in ("image", "image_url"):
                    n += self._image_cost(p)
            return n
        return tokens.count(str(content), self.model)

    def _finish(
        self, budget_tokens: int, used: int, decisions: list, kept: list, *, emit: bool
    ) -> tuple[list[dict], AssemblyReport]:
        ordered = _order_blocks(kept, self.order)
        messages: list[dict] = []
        for _idx, _block, block_messages in ordered:
            messages.extend(block_messages)
        report = AssemblyReport(
            budget=budget_tokens,
            used=used,
            reserved_output=self.reserve_output,
            model=self.model,
            decisions=decisions,
            order=self.order,
        )
        if emit:
            bus.emit(report)
        return messages, report

    def _pack(self, budget_tokens: int, *, emit: bool) -> tuple[list[dict], AssemblyReport]:
        """Pack blocks within the budget (sync). Identical to :meth:`_apack` but for the evictor."""
        priming, per_message = _framing(self.model)
        effective = max(0, budget_tokens - self.reserve_output)
        state = _PackState()
        for idx, block in self._ordered_blocks():
            if block.messages is not None:
                self._pack_history_into(block, idx, effective, priming, per_message, state)
                continue
            plan = self._plan_block(block, effective, state, priming, per_message)
            if plan.status == "evict":
                new_text, action, note, handle = self._evict(block, plan.text, plan.content_budget)
                self._apply_evicted(
                    idx, block, plan, per_message, new_text, action, note, state, handle
                )
            else:
                self._apply_plan(idx, block, plan, state)
        return self._finish(budget_tokens, state.used, state.decisions, state.kept, emit=emit)

    async def _apack(self, budget_tokens: int, *, emit: bool) -> tuple[list[dict], AssemblyReport]:
        """Async packing — like :meth:`_pack`, but awaits the async evictor (async summarizers)."""
        priming, per_message = _framing(self.model)
        effective = max(0, budget_tokens - self.reserve_output)
        state = _PackState()
        for idx, block in self._ordered_blocks():
            if block.messages is not None:
                self._pack_history_into(block, idx, effective, priming, per_message, state)
                continue
            plan = self._plan_block(block, effective, state, priming, per_message)
            if plan.status == "evict":
                new_text, action, note, handle = await self._aevict(
                    block, plan.text, plan.content_budget
                )
                self._apply_evicted(
                    idx, block, plan, per_message, new_text, action, note, state, handle
                )
            else:
                self._apply_plan(idx, block, plan, state)
        return self._finish(budget_tokens, state.used, state.decisions, state.kept, emit=emit)

    def _plan_block(
        self, block: Block, effective: int, state: _PackState, priming: int, per_message: int
    ) -> _BlockPlan:
        """Decide a single-message block's fate up to (not performing) eviction.

        Raises :class:`BudgetError` on pinned overflow. Shared by :meth:`_pack`/:meth:`_apack`, so
        the only difference between them is which evictor runs for an ``"evict"`` plan.
        """
        content_tokens = self._content_tokens(block.content)
        prim = 0 if state.has_msgs else priming
        if state.used + prim + per_message + content_tokens <= effective:
            return _BlockPlan(
                "kept",
                used=state.used + prim + per_message + content_tokens,
                message={"role": block.role, "content": block.content},
                decision=BlockDecision(block.role, "kept", content_tokens, content_tokens),
            )
        if block.pin:
            raise BudgetError(
                f"pinned block(s) exceed budget: need {prim + per_message + content_tokens} "
                f"tokens ({content_tokens} content + {prim + per_message} framing), "
                f"{effective - state.used} of {effective} remaining "
                f"(reserve_output={self.reserve_output})"
            )
        if not isinstance(block.content, str):  # can't shrink a multimodal/list block
            return _BlockPlan(
                "dropped",
                decision=BlockDecision(
                    block.role, "dropped", content_tokens, 0, "multimodal: too large"
                ),
            )
        content_budget = effective - state.used - prim - per_message
        if content_budget <= 0:
            return _BlockPlan(
                "dropped",
                decision=BlockDecision(
                    block.role, "dropped", content_tokens, 0, "no room (framing)"
                ),
            )
        return _BlockPlan(
            "evict",
            content_budget=content_budget,
            prim=prim,
            content_tokens=content_tokens,
            text=block.content,  # narrowed to str above
        )

    def _apply_plan(self, idx: int, block: Block, plan: _BlockPlan, state: _PackState) -> None:
        """Fold a non-evict plan (``"kept"`` / ``"dropped"``) into the running state."""
        if plan.status == "kept":
            state.used = plan.used
            state.has_msgs = True
            state.kept.append((idx, block, [plan.message]))
        state.decisions.append(plan.decision)

    def _apply_evicted(
        self,
        idx: int,
        block: Block,
        plan: _BlockPlan,
        per_message: int,
        new_text: str | None,
        action: str,
        note: str,
        state: _PackState,
        handle: Any = None,
    ) -> None:
        """Fold an evictor's result back into the running state (shared sync/async).

        ``handle`` (the reversible squeeze Handle from a ``compress`` eviction) is carried onto the
        ``BlockDecision`` so ``report()`` exposes it and a caller can ``expand()`` the original."""
        if new_text is None:
            state.decisions.append(
                BlockDecision(block.role, "dropped", plan.content_tokens, 0, note)
            )
            return
        after = self._content_tokens(new_text)
        state.used += plan.prim + per_message + after
        state.has_msgs = True
        state.kept.append((idx, block, [{"role": block.role, "content": new_text}]))
        state.decisions.append(
            BlockDecision(block.role, action, plan.content_tokens, after, note, handle=handle)
        )

    def _pack_history_into(
        self,
        block: Block,
        idx: int,
        effective: int,
        priming: int,
        per_message: int,
        state: _PackState,
    ) -> None:
        """Pack a multi-turn block into ``state`` (the messages-block branch, shared sync/async)."""
        turns, dec, state.used, state.has_msgs = self._pack_history(
            block, effective, state.used, state.has_msgs, priming, per_message
        )
        if turns:
            state.kept.append((idx, block, turns))
        state.decisions.append(dec)

    def _pack_history(
        self,
        block: Block,
        effective: int,
        used: int,
        has_msgs: bool,
        priming: int,
        per_message: int,
    ) -> tuple[list[dict], BlockDecision, int, bool]:
        """Pack a multi-turn block: keep the newest turns that fit, peeling the oldest.

        ``evict="truncate"`` additionally tail-trims the surviving newest turn when even it
        overflows. Other strategies fall back to peeling (with a note). Returns
        ``(kept_turns, decision, used, has_msgs)``.
        """
        turns = block.messages or []
        turn_tokens = [self._content_tokens(t.get("content", "")) for t in turns]
        total_before = sum(turn_tokens)
        is_truncate = block.evict == "truncate"

        if block.pin:
            full = (0 if has_msgs else priming) + per_message * len(turns) + total_before
            if used + full > effective:
                raise BudgetError(
                    f"pinned history block exceeds budget: needs {full} tokens, "
                    f"{effective - used} of {effective} remaining "
                    f"(reserve_output={self.reserve_output})"
                )

        kept: list[dict] = []  # built newest-first, reversed at the end
        running = used
        local_has = has_msgs
        for i in range(len(turns) - 1, -1, -1):
            prim = 0 if local_has else priming
            if running + prim + per_message + turn_tokens[i] <= effective:
                running += prim + per_message + turn_tokens[i]
                local_has = True
                kept.append(turns[i])
                continue
            if is_truncate and not kept:  # newest turn alone overflows -> tail-trim it
                budget_ct = effective - running - prim - per_message
                if budget_ct > 0:
                    trimmed = _truncate_to_tokens(
                        str(turns[i].get("content", "")), budget_ct, self.model, keep="tail"
                    )
                    running += prim + per_message + self._content_tokens(trimmed)
                    local_has = True
                    kept.append({**turns[i], "content": trimmed})
            break  # older turns are dropped (we keep a contiguous suffix of recent turns)
        kept.reverse()

        n, k = len(turns), len(kept)
        after = sum(self._content_tokens(t.get("content", "")) for t in kept)
        if n == 0:
            action, note = "kept", ""  # empty history: nothing to place, nothing dropped
        elif k == 0:
            action, note = "dropped", f"history: dropped all {n} turns (no room)"
        elif k < n:
            action, note = "truncated", f"history: kept {k} of {n} turns"
            if block.evict not in ("drop_oldest", "truncate"):
                note += f"; '{block.evict}' n/a for message blocks, peeled oldest"
        else:
            action, note = "kept", ""
        decision = BlockDecision("history", action, total_before, after, note)
        return kept, decision, running, local_has

    async def _aevict(
        self, block: Block, text: str, content_budget: int
    ) -> tuple[str | None, str, str, Any]:
        """Async eviction: await an async summarizer; delegate everything else to ``_evict``.

        Returns ``(content_or_None, action, note, handle)`` — ``handle`` is the reversible squeeze
        Handle for a ``compress`` eviction (else ``None``)."""
        if (
            isinstance(block.evict, str)
            and block.evict == "summarize"
            and block.summarizer is not None
            and inspect.iscoroutinefunction(block.summarizer)
        ):
            summary = await block.summarizer(text, content_budget)
            if tokens.count(summary, self.model) > content_budget:
                summary = _truncate_to_tokens(summary, content_budget, self.model, keep=block.keep)
            return summary, "summarized", "", None
        return self._evict(block, text, content_budget)

    def _evict(
        self, block: Block, text: str, content_budget: int
    ) -> tuple[str | None, str, str, Any]:
        """Apply a block's eviction strategy. Returns ``(content_or_None, action, note, handle)``.

        ``handle`` is the reversible squeeze :class:`~cendor.core.protocols.Handle` for a
        ``compress`` eviction (``None`` otherwise), surfaced on the block's ``BlockDecision`` so a
        caller can ``expand()`` the original back. ``content_budget`` is the room for this block's
        *content* (framing already reserved) and is always > 0 here.
        """
        strategy = block.evict

        if not isinstance(strategy, str):  # a core.protocols.EvictionStrategy object
            try:
                new, action = strategy.evict(text, content_budget, self.model)
            except Exception as exc:  # noqa: BLE001 - a custom strategy must not break assembly
                return None, "dropped", f"custom strategy raised: {exc!r}", None
            if new is None:
                return None, "dropped", action or "", None
            if tokens.count(new, self.model) > content_budget:
                new = _truncate_to_tokens(new, content_budget, self.model, keep=block.keep)
            return new, action or "evicted", "", None

        if strategy == "drop_oldest":
            note = "block dropped whole (use messages= for turn-level eviction)"
            return None, "dropped", note, None

        if strategy == "truncate":
            return (
                _truncate_to_tokens(text, content_budget, self.model, keep=block.keep),
                "truncated",
                "",
                None,
            )

        if strategy == "summarize":
            if block.summarizer is not None and not inspect.iscoroutinefunction(block.summarizer):
                summary = block.summarizer(text, content_budget)
                if tokens.count(summary, self.model) > content_budget:
                    summary = _truncate_to_tokens(
                        summary, content_budget, self.model, keep=block.keep
                    )
                return summary, "summarized", "", None
            note = (
                "async summarizer needs aassemble()"
                if block.summarizer is not None
                else "no summarizer"
            )
            return (
                _truncate_to_tokens(text, content_budget, self.model, keep=block.keep),
                "truncated",
                f"{note}; truncated",
                None,
            )

        if strategy == "compress":
            compressor = self._get_compressor()
            if compressor is not None:
                # Keep the squeeze Handle (reversibility is squeeze's USP) so report() exposes it.
                small, handle = _call_compressor(compressor, text, content_budget, self.model)
                if tokens.count(small, self.model) > content_budget:
                    small = _truncate_to_tokens(small, content_budget, self.model, keep=block.keep)
                return small, "compressed", "", handle
            # No compressor: the block is TRUNCATED instead — content is discarded, not compressed,
            # and the difference is not reversible. The note has always been recorded here, but a
            # note lives in the AssemblyReport and nothing obliges a caller to read one, so a
            # forgotten `contextkit[squeeze]` extra silently degraded every compress block in
            # production. `on_missing_compressor` lets a caller pick how loud that is; the default
            # is the historical "note", so nothing changes unless you ask for it.
            note = "squeeze not installed; fell back to truncate"
            if self.on_missing_compressor == "error":
                raise MissingCompressorError(
                    f"a {block.role!r} block asked for evict='compress' but no compressor is "
                    "available, so its content would be TRUNCATED (lossy, and not reversible the "
                    "way a compression is). Install the contextkit[squeeze] extra, pass "
                    "Context(compressor=...), call use_compressor(...), or set "
                    "on_missing_compressor='note' to accept truncation."
                )
            if self.on_missing_compressor == "warn":
                warnings.warn(
                    f"contextkit: a {block.role!r} block asked for evict='compress' but no "
                    "compressor is available; its content was TRUNCATED instead. Install the "
                    "contextkit[squeeze] extra or pass compressor=.",
                    MissingCompressorWarning,
                    stacklevel=2,
                )
            return (
                _truncate_to_tokens(text, content_budget, self.model, keep=block.keep),
                "truncated",
                note,
                None,
            )

        return None, "dropped", f"unknown evict strategy {strategy!r}", None

    def _get_compressor(self) -> Any:
        if self._compressor is not None:  # per-Context override wins
            return self._compressor
        if _default_compressor is not None:  # process-wide default via use_compressor()
            return _default_compressor
        # Otherwise auto-discover squeeze at runtime (the contextkit[squeeze] extra).
        import importlib

        try:
            return importlib.import_module("cendor.squeeze").compress
        except ModuleNotFoundError:
            return None


def _order_blocks(
    kept: list[tuple[int, Block, list[dict]]], mode: str
) -> list[tuple[int, Block, list[dict]]]:
    """Arrange kept blocks for rendering per the chosen strategy. Deterministic."""
    if mode == "cache":
        # Stable prefix: pinned, high-priority blocks lead so the prompt prefix is reused.
        return sorted(kept, key=lambda k: (not k[1].pin, -k[1].priority, k[0]))
    if mode == "attention":
        systems = sorted(
            (k for k in kept if _ord_role(k[1]) == "system"), key=lambda k: (-k[1].priority, k[0])
        )
        finals = sorted(
            (k for k in kept if _ord_role(k[1]) == "user"), key=lambda k: (k[1].priority, k[0])
        )  # ascending -> highest-priority user turn ends up last (strongest end position)
        middles = sorted(
            (k for k in kept if _ord_role(k[1]) not in ("system", "user")),
            key=lambda k: (-k[1].priority, k[0]),
        )
        return [*systems, *_edge_load(middles), *finals]
    # default: role-grouped, insertion order within a role.
    return sorted(kept, key=lambda k: (_ROLE_RANK.get(_ord_role(k[1]), 1), k[0]))


def _edge_load(items: list) -> list:
    """Edge-load a priority-descending list: highest at both edges, lowest in the center."""
    left: list = []
    right: list = []
    for i, item in enumerate(items):
        (left if i % 2 == 0 else right).append(item)
    return left + right[::-1]


def _text_of(content: Any) -> str:
    """Plain text of message content — a string, or the text parts of a multimodal list."""
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict) and "text" in p)
    return str(content)


def _parts_of(content: Any) -> list[dict]:
    """Normalize content to a list of content-block parts (text parts as ``{"text": ...}``)."""
    if isinstance(content, list):
        return [{"text": p["text"]} if isinstance(p, dict) and "text" in p else p for p in content]
    return [{"text": str(content)}]


def _accepts_model(fn: Any) -> bool:
    """Whether ``fn`` accepts a ``model`` keyword (explicitly or via ``**kwargs``)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "model" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def _call_compressor(compressor: Any, text: str, target: int, model: str) -> tuple[str, Any]:
    """Call a Compressor-protocol object or a ``squeeze.compress``-style callable.

    Returns ``(compressed_text, handle)`` — the handle reverses the compression (squeeze's USP),
    surfaced on the block's ``BlockDecision``. Forwards ``model`` so the compressor sizes against
    the *context's* model (not squeeze's gpt-4o default); a legacy ``(text, target_tokens)``
    callable that doesn't accept ``model`` still works."""
    if hasattr(compressor, "compress"):
        return compressor.compress(text, target_tokens=target, model=model)
    if _accepts_model(compressor):
        return compressor(text, target_tokens=target, model=model)
    return compressor(text, target_tokens=target)


# A short, honest marker appended (head) or prepended (tail) so a truncated block reads as cut.
_TRUNC_MARK = {"head": "\n…[truncated]", "tail": "[truncated]…\n"}


def _hard_cut(text: str, target: int, model: str, keep: str) -> str:
    """Binary-search the longest head/tail slice of ``text`` that fits ``target`` tokens."""
    if target <= 0:
        return ""
    if tokens.count(text, model) <= target:
        return text
    lo, hi, best = 0, len(text), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid] if keep == "head" else text[len(text) - mid :]
        if tokens.count(cand, model) <= target:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best


def _truncate_to_tokens(text: str, target: int, model: str, keep: str = "head") -> str:
    """Trim ``text`` to at most ``target`` tokens, keeping the ``head`` or ``tail``, with a marker.

    The marker is counted against ``target`` so the result never exceeds the budget; if there's no
    room for both content and marker, the text is hard-cut without one.
    """
    if target <= 0:
        return ""
    if tokens.count(text, model) <= target:
        return text
    marker = _TRUNC_MARK[keep]
    body_budget = max(0, target - tokens.count(marker, model))
    if body_budget == 0:
        return _hard_cut(text, target, model, keep)
    body = _hard_cut(text, body_budget, model, keep)
    return body + marker if keep == "head" else marker + body

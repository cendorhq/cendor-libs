"""Canonical data types shared across the cendor stack. See docs/core.md §5."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Usage:
    """Token usage for a single LLM call.

    ``cached_tokens`` is a *subset of* ``input_tokens`` and ``reasoning_tokens`` is a *subset of*
    ``output_tokens`` — both are breakdowns, not extra tokens, so neither is added into
    :attr:`total_tokens`. This subset convention holds for **every** provider after extraction
    normalizes to it: OpenAI's ``prompt_tokens`` already includes cached tokens, but Anthropic
    reports ``input_tokens`` *excluding* cache reads, so ``instrument._extract_usage`` folds
    ``cache_read_input_tokens`` back into ``input_tokens``. Downstream pricing therefore bills the
    cached portion exactly once. ``reasoning_tokens`` is the portion of the output spent on a
    reasoning/thinking model's internal reasoning. Providers that report it separately (OpenAI's
    ``completion_tokens_details.reasoning_tokens``, Gemini's ``thoughts_token_count``) populate it;
    providers that fold thinking into ``output_tokens`` without a separate count (Anthropic,
    Bedrock, Ollama) leave it ``0`` even when the call did reason. Cost is unaffected — reasoning is
    already billed inside ``output_tokens`` at the output rate.
    """

    input_tokens: int
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    cache_write: int = 0
    """Tokens written to the provider's prompt cache on this call (Anthropic
    ``cache_creation_input_tokens``). Unlike ``cached_tokens`` (a *subset* of ``input_tokens``, read
    from cache), these are a **separate** billed category — priced at the model's ``cache_write``
    rate (~1.25× input for Anthropic) — so they are not part of ``input_tokens`` or
    ``total_tokens``. ``0`` for providers that don't report cache writes."""

    @property
    def total_tokens(self) -> int:
        """Input + output tokens (cached and reasoning are subsets; cache_write is billed
        separately, not added)."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Money:
    """A Decimal-backed monetary amount. Never use ``float`` for money.

    Accepts ``int``/``float``/``str``/``Decimal`` for ``amount`` and coerces to
    ``Decimal`` (floats via their string form, to avoid binary-float noise).
    Arithmetic and comparisons require a matching ``currency``.
    """

    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Decimal):
            object.__setattr__(self, "amount", Decimal(str(self.amount)))

    @classmethod
    def zero(cls, currency: str = "USD") -> Money:
        """A zero amount in the given currency."""
        return cls(Decimal("0"), currency)

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money | int) -> Money:
        if other == 0:  # supports sum([...]) which starts at 0
            return self
        if not isinstance(other, Money):
            return NotImplemented
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    __radd__ = __add__

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, scalar: int | Decimal) -> Money:
        return Money(self.amount * Decimal(str(scalar)), self.currency)

    __rmul__ = __mul__

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.amount >= other.amount

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


@dataclass
class LLMCall:
    """A normalized provider-agnostic record of one model call. Emitted on the bus."""

    id: str
    provider: str
    model: str
    messages: list[dict]
    usage: Usage | None = None
    cost: Money | None = None
    latency_ms: float | None = None
    trace_id: str = ""
    ts: datetime | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ToolCall:
    """A normalized record of one tool invocation. Emitted when the dispatcher is wrapped."""

    id: str
    name: str
    arguments: dict
    result: object | None = None
    latency_ms: float | None = None
    trace_id: str = ""
    ts: datetime | None = None
    metadata: dict = field(default_factory=dict)

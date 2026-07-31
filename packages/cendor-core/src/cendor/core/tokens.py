"""Provider-aware token counting. docs/core.md §4, §8.

Exact by default — ``tiktoken`` is a required dependency of ``cendor-core``, because truthful token
counts (and therefore truthful cost/budget numbers) are the whole point. Always offline-capable,
deterministic, and network-free. Three tiers, picked automatically (see :func:`method`):

* **exact** — OpenAI with a model-native ``tiktoken`` encoding (the default path). Fine-tuned
  OpenAI ids (``ft:gpt-4o:...``) map to their base model's native encoding, so they count exactly.
* **bpe-estimate** — Claude/Gemini **and every other non-OpenAI or unrecognized model**
  (llama/mistral/deepseek/qwen, new o-series ids, hosted/open weights on Together/Groq/Fireworks/
  OpenRouter/Ollama/Bedrock): tiktoken's ``o200k`` BPE is used as a close cross-tokenizer proxy
  (far better than a character heuristic; not the native tokenizer).
* **heuristic** — a character/subword fallback, rough by design. Only reached if ``tiktoken``
  somehow fails to import at runtime (a broken/partial install); never the default a normal
  install lands on.

:func:`register` plugs a precise counter in for any family, overriding all of the above.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

# Counts for chat messages add a small fixed overhead per message plus a one-off priming
# cost, mirroring the framing real chat tokenizers add around each turn.
_MESSAGE_OVERHEAD = 4
_PRIMING = 3

# Approximate characters-per-token by family — a defensive fallback reached ONLY if tiktoken (a
# required dependency) somehow fails to import at runtime. Rough by nature (modern BPE tokenizers
# vary 3-6 chars/token by content); a normal install counts exactly and never lands here.
_CHARS_PER_TOKEN: dict[str, float] = {
    "openai": 4.0,
    "default": 4.0,
}

# Cache for tiktoken's o200k_base encoding (the cross-tokenizer estimator for non-OpenAI families).
_UNSET: object = object()
_o200k_cache: object = _UNSET

# Subword-ish pieces: words/numbers, and each run-free punctuation mark on its own.
_PIECE_RE = re.compile(r"\w+|[^\w\s]")

Counter = Callable[["str | list[dict]", str], int]
_counters: dict[str, Counter] = {}

# OpenAI reasoning ("o-series") ids: an ``o`` followed by a digit — ``o1``/``o3``/``o4``/``o5``/…
# Anchored, so ``ollama`` (no digit after ``o``) and ``olmo`` don't match. Kept general so a future
# o-series id (``o5-mini``, ``o6``) is recognized as OpenAI, not silently routed to ``default``.
_OSERIES = re.compile(r"o\d")


def _base_model(model: str) -> str:
    """Normalize a fine-tuned OpenAI id to its base model: ``ft:<base>:<org>::<id>`` → ``<base>``.

    Fine-tunes use the base model's tokenizer, so they should count *exactly* under the base
    encoding rather than fall through to a proxy. Non-``ft:`` ids are returned unchanged.
    """
    if model.lower().startswith("ft:"):
        parts = model.split(":")
        if len(parts) >= 2 and parts[1]:
            return parts[1]
    return model


def family(model: str) -> str:
    """Tokenizer family: ``"openai"`` | ``"anthropic"`` | ``"google"`` | ``"default"``.

    Substring matches handle Bedrock-style prefixed ids too (e.g. ``anthropic.claude-...``); an
    ``ft:`` fine-tune wrapper is unwrapped to its base model first.
    """
    m = _base_model(model).lower()
    if m.startswith(("gpt", "chatgpt", "text-", "davinci")) or _OSERIES.match(m):
        return "openai"
    if "claude" in m:
        return "anthropic"
    if "gemini" in m:
        return "google"
    return "default"


def register(fam: str, counter: Counter) -> None:
    """Override the counter for a family (e.g. plug in a precise tokenizer). See docs/core.md §8.

    This registers a token *counter*, not a price — to register a model's **price** use
    ``cendor.core.prices.register_model_price(...)`` (per-1M rates) or
    ``cendor.core.prices.register(...)`` (per-token).

    ```python
    from cendor.core import tokens
    tokens.register("anthropic", lambda text_or_messages, model: my_counter(text_or_messages))
    ```
    """
    _counters[fam] = counter


def method(model: str) -> str:
    """How :func:`count` will measure ``model`` — so callers can surface their confidence.

    Returns ``"registered"`` (a custom counter via :func:`register`), ``"exact"`` (OpenAI with a
    **model-native** ``tiktoken`` encoding — including a fine-tune mapped to its base model),
    ``"bpe-estimate"`` (Claude/Gemini/any non-OpenAI or unrecognized id via tiktoken's ``o200k``
    proxy — *and* an OpenAI id tiktoken doesn't recognize, which silently falls back to ``o200k``
    rather than a model-native encoding), or ``"heuristic"`` (offline character fallback, only if
    tiktoken failed to import).
    """
    fam = family(model)
    if fam in _counters:
        return "registered"
    if fam == "openai":
        if _tiktoken_encoding(model) is None:
            return "heuristic"  # tiktoken not installed
        # "exact" only when tiktoken has a model-native encoding; an unknown OpenAI id falls back
        # to the o200k proxy inside _tiktoken_encoding, which is a bpe-estimate, not exact.
        return "exact" if _openai_encoding_is_native(model) else "bpe-estimate"
    # anthropic / google / default: the o200k BPE proxy when tiktoken is present, else heuristic.
    if _o200k() is not None:
        return "bpe-estimate"
    return "heuristic"


def _openai_encoding_is_native(model: str) -> bool:
    """``True`` when ``tiktoken`` has a model-native encoding for ``model`` (not the o200k
    fallback). Used to tell an exact count from a proxy estimate for an unknown OpenAI id."""
    try:
        import tiktoken
    except ImportError:
        return False
    try:
        tiktoken.encoding_for_model(_base_model(model))
        return True
    except KeyError:
        return False


def is_exact(model: str) -> bool:
    """``True`` when :func:`count` is exact for ``model`` (OpenAI with ``tiktoken``)."""
    return method(model) == "exact"


def count(text_or_messages: str | list[dict], model: str) -> int:
    """Count tokens for a string or a list of chat messages under ``model``.

    Args:
        text_or_messages: Raw text, or a list of ``{"role", "content"}`` message dicts.
            ``content`` may itself be a list of content blocks (multimodal); text parts are summed.
        model: The model id; selects the tokenizer family.

    Returns:
        The estimated token count.

    It's ``tokens.count`` (not ``count_tokens``); pass the model **positionally or by keyword**:

    ```python
    from cendor.core import tokens
    n = tokens.count([{"role": "user", "content": "hi"}], model="gpt-4o")
    ```
    """
    fam = family(model)
    if fam in _counters:
        return _counters[fam](text_or_messages, model)

    if isinstance(text_or_messages, str):
        return _count_text(text_or_messages, fam, model)

    total = _PRIMING
    for msg in text_or_messages:
        total += _MESSAGE_OVERHEAD
        total += _count_text(_message_text(msg), fam, model)
    return total


def _message_text(msg: Any) -> str:
    if not isinstance(msg, dict):
        # Gemini list-`contents` may hold bare strings or SDK objects (types.Content / types.Part),
        # not dicts — don't call .get() on them (that raised AttributeError). Use a `text` attribute
        # if present, else the string form.
        if isinstance(msg, str):
            return msg
        text = getattr(msg, "text", None)
        return text if isinstance(text, str) else str(msg)
    content = msg.get("content", "")
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict)]
        return "".join(parts)
    return str(content or "")


def _count_text(text: str, fam: str, model: str) -> int:
    if not text:
        return 0
    if fam == "openai":
        enc = _tiktoken_encoding(model)
        if enc is not None:
            return len(enc.encode(text))  # exact (or the o200k proxy for an unknown OpenAI id)
        # tiktoken failed to import — the defensive char heuristic (never a normal install)
        return math.ceil(len(text) / _CHARS_PER_TOKEN["openai"])
    # anthropic / google / default: the o200k BPE proxy — a real tokenizer beats a char heuristic
    # for the whole non-OpenAI class (Claude/Gemini + llama/mistral/deepseek/hosted open weights).
    enc = _o200k()
    if enc is not None:
        return len(enc.encode(text))
    return _subword_estimate(text)  # offline fallback when tiktoken isn't importable


def _subword_estimate(text: str) -> int:
    """Offline subword fallback for non-OpenAI families (Claude/Gemini/default) when ``tiktoken``
    isn't installed.

    Blends a character-rate estimate with a subword-piece count (words + standalone punctuation),
    which tracks BPE tokenizers better than a flat chars/N divisor — punctuation- and code-dense
    text counts higher, plain prose lower. A rough fallback only: install the ``[tiktoken]`` extra
    for a BPE-based estimate, or :func:`register` a precise counter. docs/core.md §8.
    """
    pieces = len(_PIECE_RE.findall(text))
    char_est = len(text) / 3.5
    piece_est = pieces * 1.2
    return max(1, math.ceil((char_est + piece_est) / 2))


def _tiktoken_encoding(model: str):  # noqa: ANN202 - third-party type is optional
    """Return a tiktoken encoding for ``model`` if tiktoken is installed, else ``None``."""
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        return tiktoken.encoding_for_model(_base_model(model))
    except KeyError:
        return _o200k()


def _o200k():  # noqa: ANN202 - third-party type is optional
    """tiktoken's ``o200k_base`` encoding if installed, else ``None`` (cached).

    Used as a cross-tokenizer BPE estimator for non-OpenAI families — a real tokenizer is a far
    better approximation than a character heuristic, though it isn't the model's native tokenizer.
    """
    global _o200k_cache
    if _o200k_cache is _UNSET:
        try:
            import tiktoken

            _o200k_cache = tiktoken.get_encoding("o200k_base")
        except Exception:
            _o200k_cache = None
    return _o200k_cache

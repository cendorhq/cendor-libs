"""Provider-aware token counting. docs/core.md §4, §8.

Accurate when a real tokenizer is available, best-effort otherwise — always offline-capable,
deterministic, and network-free. Three tiers, picked automatically (see :func:`method`):

* **exact** — OpenAI with ``tiktoken`` installed (``pip install cendor-core[tiktoken]``).
* **bpe-estimate** — Claude/Gemini with ``tiktoken`` installed: tiktoken's ``o200k`` BPE is used as
  a close cross-tokenizer proxy (far better than a character heuristic; not the native tokenizer).
* **heuristic** — no tokenizer installed: a character/subword fallback, rough by design.

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

# Approximate characters-per-token by family — the offline fallback used ONLY when no real
# tokenizer (tiktoken) is installed. Rough by nature (modern BPE tokenizers vary 3-6 chars/token
# by content): install the [tiktoken] extra for accuracy, or register() a precise counter.
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


def family(model: str) -> str:
    """Tokenizer family: ``"openai"`` | ``"anthropic"`` | ``"google"`` | ``"default"``.

    Substring matches handle Bedrock-style prefixed ids too (e.g. ``anthropic.claude-...``).
    """
    m = model.lower()
    if m.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-", "davinci")):
        return "openai"
    if "claude" in m:
        return "anthropic"
    if "gemini" in m:
        return "google"
    return "default"


def register(fam: str, counter: Counter) -> None:
    """Override the counter for a family (e.g. plug in a precise tokenizer). See docs/core.md §8."""
    _counters[fam] = counter


def method(model: str) -> str:
    """How :func:`count` will measure ``model`` — so callers can surface their confidence.

    Returns ``"registered"`` (a custom counter via :func:`register`), ``"exact"`` (OpenAI with a
    **model-native** ``tiktoken`` encoding), ``"bpe-estimate"`` (Claude/Gemini via tiktoken's
    ``o200k`` proxy — *and* an OpenAI id tiktoken doesn't recognize, which silently falls back to
    ``o200k`` rather than a model-native encoding), or ``"heuristic"`` (offline character/subword
    fallback, no tiktoken).
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
    if fam in ("anthropic", "google") and _o200k() is not None:
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
        tiktoken.encoding_for_model(model)
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
            return len(enc.encode(text))  # exact
    elif fam in ("anthropic", "google"):
        enc = _o200k()
        if enc is not None:
            return len(enc.encode(text))  # BPE-based estimate — far closer than a char heuristic
        return _subword_estimate(text)  # offline fallback
    cpt = _CHARS_PER_TOKEN.get(fam, _CHARS_PER_TOKEN["default"])
    return math.ceil(len(text) / cpt)


def _subword_estimate(text: str) -> int:
    """Offline subword fallback for Claude/Gemini when ``tiktoken`` isn't installed.

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
        return tiktoken.encoding_for_model(model)
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

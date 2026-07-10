"""cendor.squeeze — content-aware, reversible context compression.

Shrink verbose context without throwing anything away: :func:`compress` returns ``(small, handle)``
and ``handle.expand()`` restores the original on demand. Content is routed by type — JSON, logs,
and prose each get a purpose-built, deterministic compressor (no LLM). Reversibility is guaranteed
by a **content-addressed store** (CCR): every original is kept keyed by its hash, deduped across
calls, so ``expand()`` is always exact no matter how hard we squeeze.

Satisfies ``cendor.core.protocols.Compressor`` by shape, so ``contextkit`` uses it for
``Block(evict="compress")`` via the ``contextkit[squeeze]`` extra — without importing this package.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

from cendor.core import tokens

from .store import MemoryStore

__all__ = ["compress", "decompress", "detect", "use_store", "Handle", "SqueezeCompressor"]

# Active content-addressed store (CCR): sha256(original) -> original. Deduped; the basis of
# reversibility. Default in-process; swap via use_store() for a persistent backend.
_backend: Any = MemoryStore()


def use_store(store: Any) -> Any:
    """Swap the CCR backend (e.g. ``store.SQLiteStore``); returns the previous one. (docs §5)

    A backend is any object with ``get(key) -> str`` and ``put(key, value) -> None``. Handles
    expand against whichever backend is active at expand time.
    """
    global _backend
    previous, _backend = _backend, store
    return previous


@dataclass
class Handle:
    """Restore handle for a compression. ``expand()`` returns the exact original. (docs §5)"""

    id: str
    kind: str
    original_ref: str  # CCR key into the content store
    restore_map: dict = field(default_factory=dict)

    def expand(self) -> str:
        """Return the original content, byte-for-byte (from the active CCR backend)."""
        return _backend.get(self.original_ref)

    @property
    def technique(self) -> str:
        """The compression technique recorded for this handle (e.g. ``"minify+dropnulls"``)."""
        return str(self.restore_map.get("technique", ""))

    def to_dict(self) -> dict:
        """Serialize the handle (not the original). Persist it alongside a durable store
        (e.g. :class:`store.SQLiteStore`) to :meth:`expand` after the process restarts."""
        return {
            "id": self.id,
            "kind": self.kind,
            "original_ref": self.original_ref,
            "restore_map": dict(self.restore_map),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Handle:
        """Rebuild a handle from :meth:`to_dict`; ``expand()`` resolves via the active store."""
        return cls(
            id=data["id"],
            kind=data["kind"],
            original_ref=data["original_ref"],
            restore_map=dict(data.get("restore_map", {})),
        )


def _store(original: str) -> str:
    key = hashlib.sha256(original.encode("utf-8")).hexdigest()
    _backend.put(key, original)
    return key


# --------------------------------------------------------------------------- detection

_TS = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
_IP = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")  # IPv4 (before the integer rule)
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-fA-F]{8,}\b")  # request ids / hashes / addresses
_INT = re.compile(r"\b\d+\b")  # standalone integers (line counts, pids, offsets, …)


def _normalize_log_line(line: str) -> str:
    """Blank out volatile fields so near-duplicate lines collapse to one pattern for dedup.

    Order matters: timestamps and UUIDs first (they contain digits/hex the later rules would
    otherwise clobber), then IPs (before integers eat their octets), then long hex runs, then any
    remaining standalone integers. The placeholders (``<ts>``/``<uuid>``/…) contain no digits, so a
    later rule never rewrites an earlier substitution."""
    line = _TS.sub("<ts>", line)
    line = _UUID.sub("<uuid>", line)
    line = _IP.sub("<ip>", line)
    line = _HEX.sub("<hex>", line)
    return _INT.sub("<n>", line)


_LEVEL = re.compile(r"\b(?:DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|TRACE|FATAL)\b")

_FIDELITY = ("lossless", "balanced", "aggressive")
_CODE_MARKERS = (
    "def ",
    "class ",
    "function ",
    "func ",
    "import ",
    "from ",
    "return ",
    "const ",
    "let ",
    "var ",
    "public ",
    "private ",
    "#include",
    "=>",
    "</",
)
# Preprocessor/import directives that begin with `#` but must NOT be stripped as comments.
_PREPROC = re.compile(
    r"#\s*(?:include|define|undef|ifdef|ifndef|endif|else|elif|if|pragma|error|line|import)\b"
)


def _strip_comments(code: str) -> str:
    """Remove ``//``, ``/* */`` and ``#`` comments while leaving string/char literals intact.

    A single-pass, string-aware scanner (handles single/double/backtick quotes with escapes),
    so a ``//`` or ``#`` *inside* a literal — a URL, a color, a path — is preserved instead of
    being cut. ``#`` preprocessor directives (``#include``, ``#define``, …) and shebang (``#!``)
    lines are kept; otherwise ``#`` begins a line comment (Python/shell style). Not a parser — a
    deterministic heuristic; reversibility is unaffected (the original is always in the CCR store).
    """
    out: list[str] = []
    i, n = 0, len(code)
    quote: str | None = None
    in_block = False
    at_line_start = True  # no non-space char seen yet on the current line
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if in_block:
            if ch == "*" and nxt == "/":
                in_block = False
                i += 2
            else:
                if ch == "\n":
                    out.append(ch)
                i += 1
            continue
        if quote is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(nxt)
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'`":
            quote = ch
            out.append(ch)
            at_line_start = False
            i += 1
            continue
        if ch == "/" and nxt == "/":
            while i < n and code[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            i += 2
            continue
        if ch == "#" and not ((at_line_start and nxt == "!") or _PREPROC.match(code, i)):
            while i < n and code[i] != "\n":
                i += 1
            continue
        if ch == "\n":
            at_line_start = True
        elif not ch.isspace():
            at_line_start = False
        out.append(ch)
        i += 1
    return "".join(out)


def detect(content: str) -> str:
    """Detect the content kind: ``"json"`` | ``"logs"`` | ``"code"`` | ``"prose"``. (docs §4)"""
    s = content.strip()
    if not s:
        return "prose"
    if s[0] in "{[":
        try:
            json.loads(s)
            return "json"
        except (ValueError, TypeError):
            pass
    if _looks_like_logs(s):
        return "logs"
    if _looks_like_code(s):
        return "code"
    return "prose"


def _looks_like_logs(s: str) -> bool:
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    hits = sum(1 for ln in lines if _TS.search(ln) or _LEVEL.search(ln))
    return hits >= len(lines) * 0.5


def _looks_like_code(s: str) -> bool:
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if not lines:
        return False
    hits = sum(
        1
        for ln in lines
        if any(m in ln for m in _CODE_MARKERS) or ln.strip().endswith(("{", "}", ";", ":"))
    )
    return hits >= max(1, len(lines) * 0.3)


# --------------------------------------------------------------------------- public API


def compress(
    content: Any,
    kind: str = "auto",
    target_tokens: int | None = None,
    model: str = "gpt-4o",
    fidelity: str = "balanced",
) -> tuple[str, Handle]:
    """Compress ``content`` and return ``(small, handle)``. ``handle.expand()`` restores it.

    Args:
        content: A string, or a JSON-serializable object (dict/list).
        kind: ``"auto"`` (detect) or one of ``"json"`` | ``"logs"`` | ``"code"`` | ``"prose"``.
        target_tokens: If given, compress *to* this budget (best effort, never exceeds it).
        model: Model id used for token counting.
        fidelity: How hard to squeeze — ``"lossless"`` (structural only), ``"balanced"`` (default),
            or ``"aggressive"``. Reversibility is unaffected; the original is always in the handle.
    """
    if fidelity not in _FIDELITY:
        raise ValueError(f"fidelity must be one of {_FIDELITY}, got {fidelity!r}")
    if isinstance(content, str):
        original = content
    else:
        try:
            original = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
        except TypeError as exc:
            raise TypeError(
                f"squeeze.compress() takes a string or a JSON-serializable object (dict/list/int/"
                f"float/bool/None); got {type(content).__name__}, which cannot be encoded as JSON. "
                f"Convert it to one of those first."
            ) from exc
        if kind == "auto":
            kind = "json"

    if kind == "auto":
        kind = detect(original)

    if kind == "json":
        small, restore_map = _compress_json(original, target_tokens, model, fidelity)
    elif kind == "logs":
        small, restore_map = _compress_logs(original, target_tokens, model)
    elif kind == "code":
        small, restore_map = _compress_code(original, target_tokens, model, fidelity)
    else:
        small, restore_map = _compress_prose(original, target_tokens, model, fidelity)

    ref = _store(original)
    # Deterministic id (squeeze is deterministic — a random uuid4 contradicted that): derived from
    # the content-addressed ref + technique, so identical (content, technique) yields the same id.
    technique = str(restore_map.get("technique", ""))
    handle = Handle(
        id=hashlib.sha256(f"{ref}:{technique}".encode()).hexdigest()[:32],
        kind=kind,
        original_ref=ref,
        restore_map=restore_map,
    )
    return small, handle


def decompress(handle: Handle) -> str:
    """Restore the original content for a handle (same as ``handle.expand()``)."""
    return handle.expand()


class SqueezeCompressor:
    """Object form satisfying ``core.protocols.Compressor`` (delegates to :func:`compress`)."""

    def compress(
        self,
        content: Any,
        *,
        target_tokens: int | None = None,
        model: str | None = None,
        kind: str = "auto",
        fidelity: str = "balanced",
    ) -> tuple[str, Handle]:
        return compress(
            content,
            kind=kind,
            target_tokens=target_tokens,
            model=model or "gpt-4o",
            fidelity=fidelity,
        )


# --------------------------------------------------------------------------- compressors


def _strip_nulls(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(v) for v in obj]
    return obj


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _peel_one(obj: Any) -> bool:
    """Remove one structural unit from ``obj`` **in place**, returning ``True`` if anything was
    removed (``False`` for an un-peelable scalar / empty container).

    Descends through single-child wrappers, so a payload nested under one key — the dominant real
    shape, ``{"data":[…]}`` / ``{"results":{…}}`` — is peeled element-by-element instead of being
    deleted wholesale (which used to collapse the whole thing to ``{}``). Dicts drop the
    largest-valued key; lists drop the trailing element (keeping a valid chronological prefix)."""
    if isinstance(obj, dict) and obj:
        if len(obj) == 1:
            (key,) = obj.keys()
            val = obj[key]
            if isinstance(val, (dict, list)) and val and _peel_one(val):
                return True
            del obj[key]  # sole key wraps a scalar / now-empty container — drop it (→ {})
            return True
        biggest = max(obj, key=lambda k: len(_dumps(obj[k])))  # first max on ties (insertion order)
        del obj[biggest]
        return True
    if isinstance(obj, list) and obj:
        tail = obj[-1]
        if len(obj) == 1 and isinstance(tail, (dict, list)) and tail and _peel_one(tail):
            return True
        obj.pop()
        return True
    return False


def _fit_json(obj: Any, target_tokens: int, model: str) -> tuple[str, bool]:
    """Shrink a JSON value to ``target_tokens`` by dropping keys/elements **structurally**, so the
    result stays valid JSON (prefix-cutting a JSON string yields a parse error).

    Peels one unit at a time — the largest dict key, or a trailing list element — recursing into a
    payload nested under a single wrapper key so ``{"data":[…]}`` keeps some elements instead of
    collapsing to ``{}``. Returns ``(json_text, dropped)``. Only if even the emptied container
    overflows (a single giant scalar) does it fall back to a raw prefix cut — the one case where the
    output may not parse (see the module docs)."""
    small = _dumps(obj)
    if tokens.count(small, model) <= target_tokens:
        return small, False
    kept = copy.deepcopy(obj)  # never mutate the caller's value
    while tokens.count(_dumps(kept), model) > target_tokens and _peel_one(kept):
        pass
    small = _dumps(kept)
    if tokens.count(small, model) <= target_tokens:
        return small, True
    # last resort: a single giant scalar/leaf value — prefix-cut (may not parse; documented)
    return _truncate_to_tokens(small, target_tokens, model), True


def _compress_json(
    text: str, target_tokens: int | None, model: str, fidelity: str = "balanced"
) -> tuple[str, dict]:
    """Minify whitespace; drop null-valued keys unless ``fidelity="lossless"``. Original in CCR.

    Under a ``target_tokens`` budget, keys/elements are dropped **structurally** so the output stays
    valid JSON (the original is always restorable via the handle)."""
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return _compress_prose(text, target_tokens, model)
    shaped = obj if fidelity == "lossless" else _strip_nulls(obj)
    technique = "minify" if fidelity == "lossless" else "minify+dropnulls"
    if target_tokens is None:
        return _dumps(shaped), {"technique": technique}
    small, dropped = _fit_json(shaped, target_tokens, model)
    if dropped:
        technique += "+drop"
    return small, {"technique": technique}


def _compress_code(
    text: str, target_tokens: int | None, model: str, fidelity: str = "balanced"
) -> tuple[str, dict]:
    """Strip comments + blank lines (and, when aggressive, internal whitespace); keep structure."""
    code = text
    if fidelity != "lossless":
        code = _strip_comments(code)
    out: list[str] = []
    for raw in code.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if fidelity == "aggressive":
            stripped = line.lstrip()
            indent = line[: len(line) - len(stripped)]
            line = indent + re.sub(r"[ \t]{2,}", " ", stripped)
        out.append(line)
    small = "\n".join(out)
    if target_tokens is not None and tokens.count(small, model) > target_tokens:
        small = _truncate_to_tokens(small, target_tokens, model)
    return small, {"technique": f"code:{fidelity}"}


def _compress_logs(text: str, target_tokens: int | None, model: str) -> tuple[str, dict]:
    """Normalize volatile fields (timestamps, UUIDs, IPs, long hex runs, standalone integers) then
    dedup repeated lines into ``(×N)`` — so near-duplicate lines that differ only in a request id,
    address, or counter collapse to one pattern on real logs.

    Under a ``target_tokens`` budget, the noisiest patterns are kept (most repeats first) but they
    are rendered back in original **chronological** order, so the timeline stays readable.
    """
    counts: dict[str, int] = {}
    order: list[str] = []
    for line in text.splitlines():
        norm = _normalize_log_line(line)
        if norm not in counts:
            order.append(norm)
        counts[norm] = counts.get(norm, 0) + 1

    rendered = {norm: f"{norm} (×{counts[norm]})" if counts[norm] > 1 else norm for norm in order}
    if target_tokens is None:
        kept = order
    else:
        # Select which patterns survive by frequency (cheap additive estimate), then restore
        # chronological order; the final truncate enforces the hard token cap.
        chosen: set[str] = set()
        running = 0
        for norm in sorted(order, key=lambda ln: counts[ln], reverse=True):
            cost = tokens.count(rendered[norm], model) + 1  # +1 for the line separator
            if running + cost > target_tokens and chosen:
                break
            chosen.add(norm)
            running += cost
        kept = [norm for norm in order if norm in chosen]

    small = "\n".join(rendered[norm] for norm in kept)
    if target_tokens is not None and tokens.count(small, model) > target_tokens:
        small = _truncate_to_tokens(small, target_tokens, model)
    return small, {"technique": "normalize+dedup", "patterns": len(order)}


_SENT = re.compile(r"(?<=[.!?])\s+")
_STOP = frozenset(
    "the a an and or but of to in on for with is are was were be been it this that as at by".split()
)
#: Abbreviations that end in a period but don't end a sentence — so "Dr. Smith" / "e.g. foo" don't
#: get split. Compared case-insensitively with trailing dots stripped.
_ABBREV = frozenset(
    "dr mr mrs ms prof sr jr st vs etc no fig eq al inc ltd co e.g i.e cf approx dept vol pp".split()  # noqa: E501
)
_LAST_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)\.?\s*$")


def _split_sentences(text: str) -> list[str]:
    """Split prose into sentences, but don't break after a common abbreviation or a decimal.

    The naive ``(?<=[.!?])\\s+`` split fires inside "Dr. Smith", "e.g. foo", and "3.14 m" (when a
    space follows). This re-joins a fragment onto the previous one when the previous fragment ends
    in a known abbreviation or a bare number, keeping real sentences intact."""
    out: list[str] = []
    for part in _SENT.split(text.strip()):
        if not part:
            continue
        if out:
            m = _LAST_WORD.search(out[-1])
            tail = m.group(1).rstrip(".").lower() if m else ""
            ends_number = out[-1].rstrip().rstrip(".")[-1:].isdigit()
            if tail in _ABBREV or ends_number:
                out[-1] = f"{out[-1]} {part}"
                continue
        out.append(part)
    return [s for s in out if s.strip()]


def _compress_prose(
    text: str, target_tokens: int | None, model: str, fidelity: str = "balanced"
) -> tuple[str, dict]:
    """Extractive: rank sentences by keyword density, keep the top ones in original order."""
    sentences = [s for s in _split_sentences(text) if s.strip()]
    if len(sentences) <= 1 or fidelity == "lossless":
        # Nothing to rank, but still honor the budget (e.g. one long sentence, or lossless).
        small = text
        if target_tokens is not None and tokens.count(small, model) > target_tokens:
            small = _truncate_to_tokens(small, target_tokens, model)
        return small, {"technique": "extractive", "kept": len(sentences), "of": len(sentences)}

    freq: dict[str, int] = {}
    for word in re.findall(r"[a-zA-Z']+", text.lower()):
        if word not in _STOP:
            freq[word] = freq.get(word, 0) + 1

    def score(sentence: str) -> float:
        # Length-normalized keyword mass (sum / sqrt(len)), NOT the mean. The mean
        # (sum / len) over-rewards short sentences built from a couple of common words and drops
        # the long, information-dense sentence that actually carries the point; dividing by
        # sqrt(len) rewards total keyword content while still damping raw length.
        words = re.findall(r"[a-zA-Z']+", sentence.lower())
        if not words:
            return 0.0
        return sum(freq.get(w, 0) for w in words) / math.sqrt(len(words))

    ranked = sorted(range(len(sentences)), key=lambda i: score(sentences[i]), reverse=True)

    if target_tokens is not None:
        keep: set[int] = set()
        for i in ranked:
            trial = " ".join(sentences[j] for j in sorted(keep | {i}))
            if tokens.count(trial, model) > target_tokens and keep:
                break
            keep.add(i)
    else:
        divisor = 3 if fidelity == "aggressive" else 2  # aggressive: top third, balanced: top half
        keep = set(ranked[: max(1, len(sentences) // divisor)])

    small = " ".join(sentences[i] for i in sorted(keep))
    # The top-ranked sentence is always kept; truncate so target_tokens is never exceeded.
    if target_tokens is not None and tokens.count(small, model) > target_tokens:
        small = _truncate_to_tokens(small, target_tokens, model)
    return small, {"technique": "extractive", "kept": len(keep), "of": len(sentences)}


def _truncate_to_tokens(text: str, target: int, model: str) -> str:
    """Binary-search the longest prefix of ``text`` that fits ``target`` tokens (deterministic)."""
    if target <= 0:
        return ""
    if tokens.count(text, model) <= target:
        return text
    lo, hi, best = 0, len(text), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid]
        if tokens.count(cand, model) <= target:
            best, lo = cand, mid + 1
        else:
            hi = mid - 1
    return best
